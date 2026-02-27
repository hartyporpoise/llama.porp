// Package api provides the HTTP server for porpulsion.
//
// Routes:
//
//	GET  /                        -> Web UI dashboard
//	GET  /static/*                -> Static assets (CSS, JS, images)
//	GET  /health                  -> Health check (also pings Ollama)
//	GET  /api/info                -> Server info (CPU, Ollama version, models)
//	GET  /api/metrics             -> JSON metrics snapshot (or SSE stream)
//	GET  /api/features            -> List all feature flags with enabled state
//	POST /api/features            -> Toggle a feature flag {"feature":"...","enabled":true}
//	GET  /api/draft-model         -> Get currently selected draft model
//	POST /api/draft-model         -> Set draft model {"model":"smollm2:135m"}
//	GET  /api/models              -> List Ollama models (JSON)
//	POST /api/pull                -> Pull a model from Ollama registry
//	DELETE /api/models/{name}     -> Delete a model from Ollama
//	GET  /v1/models               -> OpenAI-compatible model list
//	POST /v1/chat/completions     -> OpenAI-compatible chat (streaming + non-streaming)
//	POST /v1/completions          -> OpenAI-compatible text completion
package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/hartyporpoise/porpulsion/internal/config"
	"github.com/hartyporpoise/porpulsion/internal/cpu"
	"github.com/hartyporpoise/porpulsion/internal/features"
	"github.com/hartyporpoise/porpulsion/internal/metrics"
	"github.com/hartyporpoise/porpulsion/internal/ollama"
)

const (
	// maxRequestBodyBytes caps incoming JSON request bodies at 10 MB.
	maxRequestBodyBytes = 10 * 1024 * 1024
)

// validModelName matches Ollama model identifiers: name or name:tag.
var validModelName = regexp.MustCompile(`^[a-zA-Z0-9._/-]+(:[a-zA-Z0-9._-]+)?$`)

// Server is the porpulsion HTTP server.
type Server struct {
	cfg           *config.Config
	topo          *cpu.Topology
	ollama        *ollama.Client
	metrics       *metrics.Collector
	featureStore  *features.Store
	semanticCache *features.SemanticCacheStore
	mux           *http.ServeMux
	started       time.Time
}

// NewServer creates a Server with all routes registered.
func NewServer(cfg *config.Config, topo *cpu.Topology, oc *ollama.Client, mc *metrics.Collector) *Server {
	s := &Server{
		cfg:           cfg,
		topo:          topo,
		ollama:        oc,
		metrics:       mc,
		featureStore:  features.NewStore(),
		semanticCache: features.NewSemanticCache(100, 0.92),
		mux:           http.NewServeMux(),
		started:       time.Now(),
	}
	s.registerRoutes()
	return s
}

// Run starts the HTTP server on addr (e.g. "0.0.0.0:8080").
func (s *Server) Run(addr string) error {
	fmt.Printf("\n  Porpulsion is running at http://%s\n\n", addr)
	srv := &http.Server{
		Addr:    addr,
		Handler: s.mux,
		// ReadHeaderTimeout prevents slow-loris.
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
		// ReadTimeout / WriteTimeout intentionally omitted — streaming
		// SSE responses can legitimately run for minutes.
	}
	return srv.ListenAndServe()
}

func (s *Server) registerRoutes() {
	s.mux.HandleFunc("/", s.handleUI)
	s.mux.Handle("/static/", http.StripPrefix("/static/", http.FileServer(http.FS(staticFiles))))

	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/api/info", s.handleInfo)
	s.mux.HandleFunc("/api/metrics", s.handleMetrics)
	s.mux.HandleFunc("/api/features", s.handleFeatures)
	s.mux.HandleFunc("/api/draft-model", s.handleDraftModel)
	s.mux.HandleFunc("/api/compact", s.handleCompact)
	s.mux.HandleFunc("/api/models", s.handleAPIModels)
	s.mux.HandleFunc("/api/pull", s.handlePull)
	s.mux.HandleFunc("/api/models/", s.handleDeleteModel)
	s.mux.HandleFunc("/v1/models", s.handleV1Models)
	s.mux.HandleFunc("/v1/chat/completions", s.handleChat)
	s.mux.HandleFunc("/v1/completions", s.handleCompletion)
}

// -------------------------------------------------------------------------
// UI
// -------------------------------------------------------------------------

func (s *Server) handleUI(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	f, err := staticFiles.Open("index.html")
	if err != nil {
		http.Error(w, "UI not found", http.StatusNotFound)
		return
	}
	defer f.Close()
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	io.Copy(w, f)
}

// -------------------------------------------------------------------------
// Health
// -------------------------------------------------------------------------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	ollamaOK := true
	ollamaVersion := ""
	if v, err := s.ollama.Version(r.Context()); err == nil {
		ollamaVersion = v
	} else {
		ollamaOK = false
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":         "ok",
		"ollama_ok":      ollamaOK,
		"ollama_version": ollamaVersion,
	})
}

// -------------------------------------------------------------------------
// Info
// -------------------------------------------------------------------------

func (s *Server) handleInfo(w http.ResponseWriter, r *http.Request) {
	ollamaVersion := ""
	if v, err := s.ollama.Version(r.Context()); err == nil {
		ollamaVersion = v
	}

	models, _ := s.ollama.ListModels(r.Context())
	modelNames := make([]string, 0, len(models))
	for _, m := range models {
		modelNames = append(modelNames, m.Name)
	}

	ramGB := features.AvailableRAMGB()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"version":        "0.4.0",
		"ollama_url":     s.cfg.OllamaURL,
		"ollama_version": ollamaVersion,
		"default_model":  s.cfg.DefaultModel,
		"models":         modelNames,
		"uptime_seconds": int(time.Since(s.started).Seconds()),
		"ram_gb":         ramGB,
		"cpu": map[string]interface{}{
			"model":          s.topo.ModelName,
			"logical_cores":  s.topo.LogicalCores,
			"physical_cores": s.topo.PhysicalCores,
			"p_cores":        s.topo.PCores,
			"e_cores":        s.topo.ECores,
			"numa_nodes":     s.topo.NUMANodes,
			"l3_cache_mb":    s.topo.L3CacheBytes / (1024 * 1024),
			"features":       cpu.FeatureSummary(s.topo),
			"has_avx":        s.topo.HasAVX,
			"has_avx2":       s.topo.HasAVX2,
			"has_avx512":     s.topo.HasAVX512,
			"has_amx":        s.topo.HasAMX,
			"has_f16c":       s.topo.HasF16C,
			"has_fma":        s.topo.HasFMA,
			"has_neon":       s.topo.HasNEON,
			"has_sve":        s.topo.HasSVE,
		},
	})
}

// -------------------------------------------------------------------------
// Metrics
// -------------------------------------------------------------------------

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Accept") == "text/event-stream" {
		s.streamMetrics(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(s.metrics.Snapshot())
}

func (s *Server) streamMetrics(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
			data, _ := json.Marshal(s.metrics.Snapshot())
			fmt.Fprintf(w, "data: %s\n\n", data)
			flusher.Flush()
		}
	}
}

// -------------------------------------------------------------------------
// Feature flags
// -------------------------------------------------------------------------

func (s *Server) handleFeatures(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	switch r.Method {
	case http.MethodGet:
		json.NewEncoder(w).Encode(s.featureStore.All())

	case http.MethodPost:
		var req struct {
			Feature string `json:"feature"`
			Enabled bool   `json:"enabled"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		id := features.FeatureID(req.Feature)
		if !s.featureStore.Set(id, req.Enabled) {
			http.Error(w, "unknown feature: "+req.Feature, http.StatusBadRequest)
			return
		}
		// Auto-pull the embedding model when Semantic Cache is enabled.
		if id == features.SemanticCacheFlag && req.Enabled {
			go s.ensureModel("nomic-embed-text")
		}
		json.NewEncoder(w).Encode(s.featureStore.All())

	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// handleDraftModel handles GET and POST /api/draft-model.
func (s *Server) handleDraftModel(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	switch r.Method {
	case http.MethodGet:
		json.NewEncoder(w).Encode(map[string]string{
			"model": s.featureStore.DraftModel(),
		})

	case http.MethodPost:
		var req struct {
			Model string `json:"model"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		s.featureStore.SetDraftModel(req.Model)
		json.NewEncoder(w).Encode(map[string]string{
			"model": req.Model,
		})

	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// handleCompact uses the LLM to summarize a conversation into a compact form.
// The client sends the full message history and model; the server asks the LLM
// to produce a concise summary that preserves all important context.
func (s *Server) handleCompact(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)

	var req struct {
		Model    string           `json:"model"`
		Messages []ollama.Message `json:"messages"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if req.Model == "" || len(req.Messages) < 4 {
		http.Error(w, "need model and at least 4 messages to compact", http.StatusBadRequest)
		return
	}

	// Build the compaction prompt: feed the conversation and ask for a summary.
	compactPrompt := []ollama.Message{
		{
			Role: "system",
			Content: "You are a conversation summarizer. The user will give you a conversation " +
				"between a human and an AI assistant. Produce a concise summary that preserves:\n" +
				"- Key facts, decisions, and conclusions\n" +
				"- Important names, numbers, code snippets, or technical details\n" +
				"- The overall topic and direction of the conversation\n" +
				"- Any open questions or unresolved items\n\n" +
				"Write the summary in second person (\"you asked about...\", \"you decided to...\"). " +
				"Be concise but don't lose important details. Output ONLY the summary, no preamble.",
		},
		{
			Role:    "user",
			Content: formatConversationForCompaction(req.Messages),
		},
	}

	summary, err := s.ollama.Chat(r.Context(), ollama.ChatRequest{
		Model:    req.Model,
		Messages: compactPrompt,
		Options:  &ollama.Options{NumPredict: 512},
	})
	if err != nil {
		http.Error(w, "compaction failed: "+err.Error(), http.StatusBadGateway)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{
		"summary": summary,
	})
}

// formatConversationForCompaction renders a message list as readable text
// for the compaction LLM prompt.
func formatConversationForCompaction(msgs []ollama.Message) string {
	var sb strings.Builder
	sb.WriteString("Here is the conversation to summarize:\n\n")
	for _, m := range msgs {
		switch m.Role {
		case "system":
			sb.WriteString("[System]: ")
		case "user":
			sb.WriteString("[Human]: ")
		case "assistant":
			sb.WriteString("[Assistant]: ")
		default:
			sb.WriteString("[" + m.Role + "]: ")
		}
		sb.WriteString(m.Content)
		sb.WriteString("\n\n")
	}
	return sb.String()
}

// ensureModel checks if a model is locally available and pulls it if not.
// Designed to be called in a goroutine. Silent on success, logs only errors.
func (s *Server) ensureModel(name string) {
	ctx := context.Background()
	models, err := s.ollama.ListModels(ctx)
	if err != nil {
		return
	}
	for _, m := range models {
		if m.Name == name || strings.HasPrefix(m.Name, name+":") {
			return // already installed
		}
	}
	ch, errCh := s.ollama.PullStream(ctx, name)
	for range ch {
		// drain progress events silently
	}
	if err := <-errCh; err != nil {
		fmt.Printf("[porpulsion] auto-pull %s failed: %v\n", name, err)
	}
}

// -------------------------------------------------------------------------
// Model management
// -------------------------------------------------------------------------

func (s *Server) handleAPIModels(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	models, err := s.ollama.ListModels(r.Context())
	if err != nil {
		http.Error(w, "ollama error: "+err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(models)
}

func (s *Server) handlePull(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req struct {
		Name string `json:"name"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Name == "" {
		http.Error(w, "missing model name", http.StatusBadRequest)
		return
	}
	if !validModelName.MatchString(req.Name) {
		http.Error(w, "invalid model name", http.StatusBadRequest)
		return
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	ch, errCh := s.ollama.PullStream(r.Context(), req.Name)
	for status := range ch {
		data, _ := json.Marshal(status)
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}
	if err := <-errCh; err != nil {
		data, _ := json.Marshal(map[string]string{"error": err.Error()})
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}
}

func (s *Server) handleDeleteModel(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	name := strings.TrimPrefix(r.URL.Path, "/api/models/")
	if name == "" {
		http.Error(w, "missing model name", http.StatusBadRequest)
		return
	}
	if !validModelName.MatchString(name) {
		http.Error(w, "invalid model name", http.StatusBadRequest)
		return
	}
	if err := s.ollama.DeleteModel(r.Context(), name); err != nil {
		http.Error(w, "ollama error: "+err.Error(), http.StatusBadGateway)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// -------------------------------------------------------------------------
// OpenAI-compatible endpoints
// -------------------------------------------------------------------------

func (s *Server) handleV1Models(w http.ResponseWriter, r *http.Request) {
	models, err := s.ollama.ListModels(r.Context())
	if err != nil {
		http.Error(w, "ollama error: "+err.Error(), http.StatusBadGateway)
		return
	}
	items := make([]map[string]interface{}, 0, len(models))
	for _, m := range models {
		items = append(items, map[string]interface{}{
			"id":       m.Name,
			"object":   "model",
			"created":  m.ModifiedAt.Unix(),
			"owned_by": "ollama",
		})
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"object": "list", "data": items})
}

type openAIChatRequest struct {
	Model       string                   `json:"model"`
	Messages    []map[string]interface{} `json:"messages"`
	Stream      bool                     `json:"stream"`
	MaxTokens   int                      `json:"max_tokens"`
	Temperature *float64                 `json:"temperature"`
	TopP        float64                  `json:"top_p"`
	TopK        int                      `json:"top_k"`
	CtxSize     int                      `json:"ctx_size"`
}

func (s *Server) handleChat(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req openAIChatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}

	model := req.Model
	if model == "" {
		model = s.cfg.DefaultModel
	}

	// Convert OpenAI messages to Ollama messages.
	msgs := make([]ollama.Message, 0, len(req.Messages))
	for _, m := range req.Messages {
		role, _ := m["role"].(string)
		content, _ := m["content"].(string)
		msgs = append(msgs, ollama.Message{Role: role, Content: content})
	}

	// -- Feature: Smart Context -- compress long history --------------------
	if s.featureStore.IsEnabled(features.SmartContext) {
		before := len(msgs)
		msgs = features.CompressHistory(msgs)
		if len(msgs) < before {
			fmt.Printf("[porpulsion] smart_context: %d -> %d messages\n", before, len(msgs))
		}
	}

	// -- Feature: Semantic Cache -- check for cached response ---------------
	var lastUserMsg string
	for i := len(msgs) - 1; i >= 0; i-- {
		if msgs[i].Role == "user" {
			lastUserMsg = msgs[i].Content
			break
		}
	}
	var queryEmbedding []float64
	if s.featureStore.IsEnabled(features.SemanticCacheFlag) && lastUserMsg != "" {
		// nomic-embed-text is an embedding-only model — use Embed(), not Chat().
		if emb, err := s.ollama.Embed(r.Context(), "nomic-embed-text", lastUserMsg); err == nil {
			queryEmbedding = emb
			if cached, hit := s.semanticCache.Lookup(emb, model); hit {
				s.streamCachedResponse(w, r, model, cached)
				return
			}
		}
	}

	// Build Ollama options from OpenAI params.
	opts := s.buildOptions(req.Temperature, req.TopP, req.TopK, req.MaxTokens, req.CtxSize)

	// -- Feature: Speculative Decoding -- draft model provides a hint -------
	// A small draft model generates a quick response to the same prompt.
	// We inject it as a system hint so the target model can use it as a
	// starting point but is free to override if the draft is off.
	draftModel := s.featureStore.DraftModel()
	if s.featureStore.IsEnabled(features.SpeculativeDecoding) && draftModel != "" && model != draftModel {
		draftReq := ollama.ChatRequest{
			Model:    draftModel,
			Messages: msgs,
			Options:  &ollama.Options{NumPredict: 20},
		}
		if draft, err := s.ollama.Chat(r.Context(), draftReq); err == nil && draft != "" {
			// Insert as a system hint right before the last user message.
			// The target model treats this as guidance, not committed output.
			hint := ollama.Message{
				Role:    "system",
				Content: "A fast draft model suggested this response — use it as a starting point but improve and correct as needed: " + draft,
			}
			// Insert before the last message (which is the user's question).
			if len(msgs) > 1 {
				msgs = append(msgs[:len(msgs)-1], hint, msgs[len(msgs)-1])
			}
		}
	}

	ollamaReq := ollama.ChatRequest{
		Model:    model,
		Messages: msgs,
		Options:  opts,
	}

	done := s.metrics.RequestStart()
	defer done()

	if req.Stream {
		if s.featureStore.IsEnabled(features.SemanticCacheFlag) && queryEmbedding != nil {
			s.streamChatAndCache(w, r, ollamaReq, queryEmbedding)
		} else {
			s.streamChat(w, r, ollamaReq)
		}
	} else {
		s.collectChat(w, r, ollamaReq)
	}
}

// streamCachedResponse streams a cached response as OpenAI SSE events.
func (s *Server) streamCachedResponse(w http.ResponseWriter, r *http.Request, model, cached string) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Porpulsion-Cached", "true")

	id := fmt.Sprintf("chatcmpl-%d", time.Now().UnixNano())

	oaiChunk := map[string]interface{}{
		"id":      id,
		"object":  "chat.completion.chunk",
		"created": time.Now().Unix(),
		"model":   model,
		"choices": []map[string]interface{}{
			{
				"index":         0,
				"delta":         map[string]string{"role": "assistant", "content": cached},
				"finish_reason": nil,
			},
		},
	}
	data, _ := json.Marshal(oaiChunk)
	fmt.Fprintf(w, "data: %s\n\n", data)
	flusher.Flush()

	doneChunk := map[string]interface{}{
		"id":      id,
		"object":  "chat.completion.chunk",
		"created": time.Now().Unix(),
		"model":   model,
		"choices": []map[string]interface{}{
			{
				"index":         0,
				"delta":         map[string]string{"role": "assistant", "content": ""},
				"finish_reason": "stop",
			},
		},
	}
	data, _ = json.Marshal(doneChunk)
	fmt.Fprintf(w, "data: %s\n\n", data)
	fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()
}

// streamChatAndCache wraps streamChat to capture the full response for the
// semantic cache.
func (s *Server) streamChatAndCache(w http.ResponseWriter, r *http.Request, req ollama.ChatRequest, embedding []float64) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	id := fmt.Sprintf("chatcmpl-%d", time.Now().UnixNano())
	ch, errCh := s.ollama.ChatStream(r.Context(), req)

	start := time.Now()
	var firstTokenAt time.Time
	var tokenCount int64
	var prevTokenAt time.Time
	var fullResponse strings.Builder

	for chunk := range ch {
		now := time.Now()
		tokenCount++

		ttftMs := 0.0
		tpotMs := 0.0
		if tokenCount == 1 {
			firstTokenAt = now
			ttftMs = float64(now.Sub(start).Milliseconds())
		} else if !prevTokenAt.IsZero() {
			tpotMs = float64(now.Sub(prevTokenAt).Milliseconds())
		}
		prevTokenAt = now
		s.metrics.RecordTokens(1, ttftMs, tpotMs)
		_ = firstTokenAt

		fullResponse.WriteString(chunk.Message.Content)

		var finishReason interface{}
		oaiChunk := map[string]interface{}{
			"id":      id,
			"object":  "chat.completion.chunk",
			"created": time.Now().Unix(),
			"model":   req.Model,
			"choices": []map[string]interface{}{
				{
					"index":         0,
					"delta":         map[string]string{"role": "assistant", "content": chunk.Message.Content},
					"finish_reason": finishReason,
				},
			},
		}
		if chunk.Done {
			finishReason = "stop"
			oaiChunk["choices"] = []map[string]interface{}{
				{
					"index":         0,
					"delta":         map[string]string{"role": "assistant", "content": chunk.Message.Content},
					"finish_reason": finishReason,
				},
			}
			oaiChunk["usage"] = map[string]int{
				"prompt_tokens":     chunk.PromptEvalCount,
				"completion_tokens": chunk.EvalCount,
				"total_tokens":      chunk.PromptEvalCount + chunk.EvalCount,
			}
		}
		data, _ := json.Marshal(oaiChunk)
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}

	if err := <-errCh; err != nil && r.Context().Err() == nil {
		data, _ := json.Marshal(map[string]string{"error": err.Error()})
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}

	fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()

	// Store in semantic cache if we got a meaningful response.
	if resp := fullResponse.String(); len(resp) > 0 {
		s.semanticCache.Store(embedding, resp, req.Model)
	}
}

// buildOptions assembles Ollama Options from OpenAI request params.
func (s *Server) buildOptions(temp *float64, topP float64, topK int, maxTokens int, ctxSize int) *ollama.Options {
	opts := &ollama.Options{}
	if temp != nil {
		opts.Temperature = *temp
	}
	if topP > 0 {
		opts.TopP = topP
	}
	if topK > 0 {
		opts.TopK = topK
	}
	if maxTokens > 0 {
		opts.NumPredict = maxTokens
	}
	if ctxSize > 0 {
		opts.NumCtx = ctxSize
	}
	return opts
}

// streamChat forwards a streaming chat request to Ollama and re-emits as OpenAI SSE.
func (s *Server) streamChat(w http.ResponseWriter, r *http.Request, req ollama.ChatRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	id := fmt.Sprintf("chatcmpl-%d", time.Now().UnixNano())
	ch, errCh := s.ollama.ChatStream(r.Context(), req)

	start := time.Now()
	var firstTokenAt time.Time
	var tokenCount int64
	var prevTokenAt time.Time

	for chunk := range ch {
		now := time.Now()
		tokenCount++

		ttftMs := 0.0
		tpotMs := 0.0
		if tokenCount == 1 {
			firstTokenAt = now
			ttftMs = float64(now.Sub(start).Milliseconds())
		} else if !prevTokenAt.IsZero() {
			tpotMs = float64(now.Sub(prevTokenAt).Milliseconds())
		}
		prevTokenAt = now
		s.metrics.RecordTokens(1, ttftMs, tpotMs)
		_ = firstTokenAt

		var finishReason interface{}
		oaiChunk := map[string]interface{}{
			"id":      id,
			"object":  "chat.completion.chunk",
			"created": time.Now().Unix(),
			"model":   req.Model,
			"choices": []map[string]interface{}{
				{
					"index":         0,
					"delta":         map[string]string{"role": "assistant", "content": chunk.Message.Content},
					"finish_reason": finishReason,
				},
			},
		}
		if chunk.Done {
			finishReason = "stop"
			oaiChunk["choices"] = []map[string]interface{}{
				{
					"index":         0,
					"delta":         map[string]string{"role": "assistant", "content": chunk.Message.Content},
					"finish_reason": finishReason,
				},
			}
			// Include token usage on the final chunk so the UI can show
			// context utilisation and auto-compress when needed.
			oaiChunk["usage"] = map[string]int{
				"prompt_tokens":     chunk.PromptEvalCount,
				"completion_tokens": chunk.EvalCount,
				"total_tokens":      chunk.PromptEvalCount + chunk.EvalCount,
			}
		}
		data, _ := json.Marshal(oaiChunk)
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}

	if err := <-errCh; err != nil && r.Context().Err() == nil {
		data, _ := json.Marshal(map[string]string{"error": err.Error()})
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}

	fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func (s *Server) collectChat(w http.ResponseWriter, r *http.Request, req ollama.ChatRequest) {
	ch, errCh := s.ollama.ChatStream(r.Context(), req)
	var sb strings.Builder
	start := time.Now()
	var tokenCount int64
	for chunk := range ch {
		sb.WriteString(chunk.Message.Content)
		tokenCount++
		ttftMs := 0.0
		if tokenCount == 1 {
			ttftMs = float64(time.Since(start).Milliseconds())
		}
		s.metrics.RecordTokens(1, ttftMs, 0)
	}
	if err := <-errCh; err != nil {
		http.Error(w, "ollama error: "+err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"id":      fmt.Sprintf("chatcmpl-%d", time.Now().UnixNano()),
		"object":  "chat.completion",
		"created": time.Now().Unix(),
		"model":   req.Model,
		"choices": []map[string]interface{}{
			{"index": 0, "message": map[string]string{"role": "assistant", "content": sb.String()}, "finish_reason": "stop"},
		},
	})
}

type openAICompletionRequest struct {
	Model       string   `json:"model"`
	Prompt      string   `json:"prompt"`
	Stream      bool     `json:"stream"`
	MaxTokens   int      `json:"max_tokens"`
	Temperature *float64 `json:"temperature"`
}

func (s *Server) handleCompletion(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
	var req openAICompletionRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}

	model := req.Model
	if model == "" {
		model = s.cfg.DefaultModel
	}

	opts := s.buildOptions(req.Temperature, 0, 0, req.MaxTokens, 0)

	ollamaReq := ollama.GenerateRequest{
		Model:   model,
		Prompt:  req.Prompt,
		Options: opts,
	}

	done := s.metrics.RequestStart()
	defer done()

	if req.Stream {
		s.streamGenerate(w, r, ollamaReq)
	} else {
		s.collectGenerate(w, r, ollamaReq)
	}
}

func (s *Server) streamGenerate(w http.ResponseWriter, r *http.Request, req ollama.GenerateRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	id := fmt.Sprintf("cmpl-%d", time.Now().UnixNano())
	ch, errCh := s.ollama.GenerateStream(r.Context(), req)

	start := time.Now()
	var tokenCount int64
	var prevAt time.Time

	for chunk := range ch {
		now := time.Now()
		tokenCount++
		ttftMs, tpotMs := 0.0, 0.0
		if tokenCount == 1 {
			ttftMs = float64(now.Sub(start).Milliseconds())
		} else if !prevAt.IsZero() {
			tpotMs = float64(now.Sub(prevAt).Milliseconds())
		}
		prevAt = now
		s.metrics.RecordTokens(1, ttftMs, tpotMs)

		var finishReason interface{}
		if chunk.Done {
			finishReason = "stop"
		}
		oaiChunk := map[string]interface{}{
			"id":      id,
			"object":  "text_completion",
			"created": time.Now().Unix(),
			"model":   req.Model,
			"choices": []map[string]interface{}{
				{"text": chunk.Response, "index": 0, "finish_reason": finishReason},
			},
		}
		data, _ := json.Marshal(oaiChunk)
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}

	if err := <-errCh; err != nil && r.Context().Err() == nil {
		data, _ := json.Marshal(map[string]string{"error": err.Error()})
		fmt.Fprintf(w, "data: %s\n\n", data)
		flusher.Flush()
	}

	fmt.Fprintf(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func (s *Server) collectGenerate(w http.ResponseWriter, r *http.Request, req ollama.GenerateRequest) {
	ch, errCh := s.ollama.GenerateStream(r.Context(), req)
	var sb strings.Builder
	start := time.Now()
	var tokenCount int64
	for chunk := range ch {
		tokenCount++
		ttftMs := 0.0
		if tokenCount == 1 {
			ttftMs = float64(time.Since(start).Milliseconds())
		}
		s.metrics.RecordTokens(1, ttftMs, 0)
		sb.WriteString(chunk.Response)
	}
	if err := <-errCh; err != nil {
		http.Error(w, "ollama error: "+err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"id":      fmt.Sprintf("cmpl-%d", time.Now().UnixNano()),
		"object":  "text_completion",
		"created": time.Now().Unix(),
		"model":   req.Model,
		"choices": []map[string]interface{}{
			{"text": sb.String(), "index": 0, "finish_reason": "stop"},
		},
	})
}
