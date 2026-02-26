// Package api provides the HTTP server for porpulsion.
//
// Routes:
//
//	GET  /                        → Web UI dashboard
//	GET  /static/*                → Static assets (CSS, JS, images)
//	GET  /health                  → Health check (also pings Ollama)
//	GET  /api/info                → Server info (CPU, Ollama version, models)
//	GET  /api/metrics             → JSON metrics snapshot (or SSE stream)
//	GET  /api/features            → List all feature flags with enabled state
//	POST /api/features            → Toggle a feature flag {"feature":"...","enabled":true}
//	GET  /api/quant-advice        → Quantization recommendation for this machine
//	GET  /api/models              → List Ollama models (JSON)
//	POST /api/pull                → Pull a model from Ollama registry
//	DELETE /api/models/{name}     → Delete a model from Ollama
//	GET  /v1/models               → OpenAI-compatible model list
//	POST /v1/chat/completions     → OpenAI-compatible chat (streaming + non-streaming)
//	POST /v1/completions          → OpenAI-compatible text completion
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
	// This prevents memory exhaustion from oversized payloads.
	maxRequestBodyBytes = 10 * 1024 * 1024
)

// validModelName matches Ollama model identifiers: name or name:tag.
// Examples: "llama3.2", "llama3.2:latest", "qwen2.5-coder:7b-instruct-q4_K_M"
var validModelName = regexp.MustCompile(`^[a-zA-Z0-9._/-]+(:[a-zA-Z0-9._-]+)?$`)

// Server is the porpulsion HTTP server.
type Server struct {
	cfg          *config.Config
	topo         *cpu.Topology
	ollama       *ollama.Client
	metrics      *metrics.Collector
	featureStore *features.Store
	batcher      *features.Batcher
	prefixCache  *features.PrefixCacheStore
	mux          *http.ServeMux
	started      time.Time
}

// NewServer creates a Server with all routes registered.
func NewServer(cfg *config.Config, topo *cpu.Topology, oc *ollama.Client, mc *metrics.Collector) *Server {
	s := &Server{
		cfg:          cfg,
		topo:         topo,
		ollama:       oc,
		metrics:      mc,
		featureStore: features.NewStore(),
		batcher:      features.NewBatcher(),
		// Prefix cache: 5-minute TTL, max 64 entries per instance.
		prefixCache: features.NewPrefixCache(5*time.Minute, 64),
		mux:         http.NewServeMux(),
		started:     time.Now(),
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
		// ReadHeaderTimeout prevents slow-loris: clients that send headers very
		// slowly would otherwise hold a goroutine open indefinitely.
		ReadHeaderTimeout: 10 * time.Second,
		// IdleTimeout closes keep-alive connections that sit idle too long.
		IdleTimeout: 120 * time.Second,
		// Note: ReadTimeout / WriteTimeout intentionally omitted — streaming
		// SSE responses (chat, model pull) can legitimately run for minutes.
	}
	return srv.ListenAndServe()
}

func (s *Server) registerRoutes() {
	s.mux.HandleFunc("/", s.handleUI)
	s.mux.Handle("/static/", http.StripPrefix("/static/", http.FileServer(http.FS(staticFiles))))

	// Utility
	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/api/info", s.handleInfo)
	s.mux.HandleFunc("/api/metrics", s.handleMetrics)

	// Feature flags
	s.mux.HandleFunc("/api/features", s.handleFeatures)
	s.mux.HandleFunc("/api/quant-advice", s.handleQuantAdvice)

	// Model management (used by UI)
	s.mux.HandleFunc("/api/models", s.handleAPIModels)
	s.mux.HandleFunc("/api/pull", s.handlePull)
	s.mux.HandleFunc("/api/models/", s.handleDeleteModel) // DELETE /api/models/{name}

	// OpenAI-compatible endpoints
	s.mux.HandleFunc("/v1/models", s.handleV1Models)
	s.mux.HandleFunc("/v1/chat/completions", s.handleChat)
	s.mux.HandleFunc("/v1/completions", s.handleCompletion)
}

// ─────────────────────────────────────────────────────────────────────────
// UI
// ─────────────────────────────────────────────────────────────────────────

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

// ─────────────────────────────────────────────────────────────────────────
// Health
// ─────────────────────────────────────────────────────────────────────────

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

// ─────────────────────────────────────────────────────────────────────────
// Info
// ─────────────────────────────────────────────────────────────────────────

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
		"version":        "0.3.0",
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
			// Individual boolean flags so the UI can render per-feature badges.
			"has_avx":    s.topo.HasAVX,
			"has_avx2":   s.topo.HasAVX2,
			"has_avx512": s.topo.HasAVX512,
			"has_amx":    s.topo.HasAMX,
			"has_f16c":   s.topo.HasF16C,
			"has_fma":    s.topo.HasFMA,
			"has_neon":   s.topo.HasNEON,
			"has_sve":    s.topo.HasSVE,
		},
	})
}

// ─────────────────────────────────────────────────────────────────────────
// Metrics
// ─────────────────────────────────────────────────────────────────────────

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

// ─────────────────────────────────────────────────────────────────────────
// Feature flags
// ─────────────────────────────────────────────────────────────────────────

// handleFeatures handles GET and POST /api/features.
//
//	GET  → returns all feature flags as JSON array
//	POST → toggles a flag; body: {"feature":"batching","enabled":true}
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
		// Side-effects when specific features are toggled.
		if id == features.Batching {
			s.batcher.SetEnabled(req.Enabled)
		}
		// Return updated list.
		json.NewEncoder(w).Encode(s.featureStore.All())

	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

// handleQuantAdvice returns the quant recommendation for this machine's RAM.
func (s *Server) handleQuantAdvice(w http.ResponseWriter, r *http.Request) {
	ramGB := features.AvailableRAMGB()
	tier := features.RecommendTier(ramGB)
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"ram_gb":      ramGB,
		"recommended": tier,
		"all_tiers":   features.AllTiers(),
	})
}

// ─────────────────────────────────────────────────────────────────────────
// Model management
// ─────────────────────────────────────────────────────────────────────────

// handleAPIModels returns the list of locally available Ollama models as JSON.
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

// handlePull initiates a model pull from Ollama and streams progress as SSE.
// When quant_advisor is enabled and no tag is given, the recommended quant
// suffix is appended automatically.
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

	// Quant advisor: if enabled and no explicit tag, pick the best quant for this machine.
	// Aggressive quant: drop one tier lower than recommended to fit larger models in RAM.
	if s.featureStore.IsEnabled(features.QuantAdvisor) {
		ramGB := features.AvailableRAMGB()
		if s.featureStore.IsEnabled(features.AggressiveQuant) {
			ramGB = features.ShrinkRAMForAggressiveQuant(ramGB)
		}
		req.Name = features.BestPullName(req.Name, ramGB)
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

// handleDeleteModel handles DELETE /api/models/{name}.
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

// ─────────────────────────────────────────────────────────────────────────
// OpenAI-compatible endpoints
// ─────────────────────────────────────────────────────────────────────────

// handleV1Models returns the model list in OpenAI format.
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

// openAIChatRequest is the OpenAI /v1/chat/completions request body.
type openAIChatRequest struct {
	Model       string                   `json:"model"`
	Messages    []map[string]interface{} `json:"messages"`
	Stream      bool                     `json:"stream"`
	MaxTokens   int                      `json:"max_tokens"`
	Temperature *float64                 `json:"temperature"`
	TopP        float64                  `json:"top_p"`
	TopK        int                      `json:"top_k"`
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

	// Resolve model: use request model → default model.
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

	// Build Ollama options from OpenAI params.
	opts := s.buildOptions(req.Temperature, req.TopP, req.TopK, req.MaxTokens, 0)

	// ── Feature: prefix cache ──────────────────────────────────────────────
	// Hash everything except the last user turn.  On a hit, tell Ollama to
	// keep the model loaded so it can reuse its in-memory KV context.
	keepAlive := ""
	if s.featureStore.IsEnabled(features.PrefixCache) && len(msgs) > 1 {
		var sysPrompt string
		prefixParts := make([]string, 0, len(msgs))
		for i, m := range msgs {
			if m.Role == "system" {
				sysPrompt = m.Content
			} else if i < len(msgs)-1 {
				prefixParts = append(prefixParts, m.Role+":"+m.Content)
			}
		}
		hash := features.HashPrefix(sysPrompt, prefixParts)
		if _, hit := s.prefixCache.Lookup(hash); hit {
			keepAlive = "10m"
		}
		s.prefixCache.Store(hash, len(prefixParts)*50)
	}

	ollamaReq := ollama.ChatRequest{
		Model:     model,
		Messages:  msgs,
		Options:   opts,
		KeepAlive: keepAlive,
	}

	done := s.metrics.RequestStart()
	defer done()

	// ── Feature: request batching ──────────────────────────────────────────
	s.batcher.Do(r.Context(), func(ctx context.Context) {
		if req.Stream {
			s.streamChat(w, r.WithContext(ctx), ollamaReq)
		} else {
			s.collectChat(w, r.WithContext(ctx), ollamaReq)
		}
	})
}

// boolPtr returns a pointer to b, used to send explicit bool options to Ollama.
func boolPtr(b bool) *bool { return &b }

// buildOptions assembles Ollama Options, applying active feature flags.
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

	// Thread affinity: use only P-cores to avoid E-core thrashing.
	if s.featureStore.IsEnabled(features.ThreadHint) && s.topo.PCores > 0 {
		opts.NumThread = s.topo.PCores
	}

	// Flash Attention: O(1) memory attention kernel, faster at long context.
	if s.featureStore.IsEnabled(features.FlashAttn) {
		opts.FlashAttn = boolPtr(true)
	}

	// mmap: load weights via OS page cache — instant cold-start, shared pages.
	if s.featureStore.IsEnabled(features.MmapWeights) {
		opts.UseMmap = boolPtr(true)
	}

	// mlock: pin weights in physical RAM, prevent swap eviction.
	if s.featureStore.IsEnabled(features.MLockWeights) {
		opts.UseMlock = boolPtr(true)
	}

	// ── Footprint reduction ────────────────────────────────────────────────

	// Lean context: cap KV cache to 512 tokens — ~8× smaller than default 4096.
	if s.featureStore.IsEnabled(features.LeanContext) {
		if opts.NumCtx == 0 || opts.NumCtx > 512 {
			opts.NumCtx = 512
		}
		opts.NumBatch = 128 // smaller prefill chunks to match reduced context
	}

	// Low VRAM: skip scratch buffers, use streaming attention.
	if s.featureStore.IsEnabled(features.LowVRAM) {
		opts.LowVRAM = boolPtr(true)
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
		if chunk.Done {
			finishReason = "stop"
		}
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

// collectChat waits for the full response then writes a single JSON object.
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

// openAICompletionRequest is the OpenAI /v1/completions request body.
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

	s.batcher.Do(r.Context(), func(ctx context.Context) {
		if req.Stream {
			s.streamGenerate(w, r.WithContext(ctx), ollamaReq)
		} else {
			s.collectGenerate(w, r.WithContext(ctx), ollamaReq)
		}
	})
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