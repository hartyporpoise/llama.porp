// Package ollama provides a typed HTTP client for the Ollama API.
// Porpulsion uses this to proxy chat/completion requests and manage models.
package ollama

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Client wraps the Ollama HTTP API.
type Client struct {
	BaseURL    string
	httpClient *http.Client
}

// NewClient creates a new Ollama client pointing at baseURL (e.g. "http://ollama:11434").
func NewClient(baseURL string) *Client {
	return &Client{
		BaseURL: baseURL,
		httpClient: &http.Client{
			Timeout: 0, // no timeout — streaming responses can be long
		},
	}
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// Model is a single entry from GET /api/tags.
type Model struct {
	Name       string    `json:"name"`
	ModifiedAt time.Time `json:"modified_at"`
	Size       int64     `json:"size"`
	Digest     string    `json:"digest"`
	Details    struct {
		Format            string   `json:"format"`
		Family            string   `json:"family"`
		Families          []string `json:"families"`
		ParameterSize     string   `json:"parameter_size"`
		QuantizationLevel string   `json:"quantization_level"`
	} `json:"details"`
}

// Message is a single chat turn (role + content).
type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ChatRequest maps to POST /api/chat.
type ChatRequest struct {
	Model    string    `json:"model"`
	Messages []Message `json:"messages"`
	Stream   bool      `json:"stream"`
	Options  *Options  `json:"options,omitempty"`
	// KeepAlive overrides how long Ollama keeps the model in memory (e.g. "10m").
	// Set when a prefix-cache hit is detected so the model stays warm for the next turn.
	KeepAlive string `json:"keep_alive,omitempty"`
}

// GenerateRequest maps to POST /api/generate.
type GenerateRequest struct {
	Model     string   `json:"model"`
	Prompt    string   `json:"prompt"`
	Stream    bool     `json:"stream"`
	Options   *Options `json:"options,omitempty"`
	KeepAlive string   `json:"keep_alive,omitempty"`
}

// Options are sampling parameters forwarded to Ollama.
type Options struct {
	Temperature   float64 `json:"temperature,omitempty"`
	TopP          float64 `json:"top_p,omitempty"`
	TopK          int     `json:"top_k,omitempty"`
	NumCtx        int     `json:"num_ctx,omitempty"`
	RepeatPenalty float64 `json:"repeat_penalty,omitempty"`
	NumPredict    int     `json:"num_predict,omitempty"`
	// NumThread pins inference to this many OS threads (thread-affinity hint).
	// Set to PCores to avoid efficiency-core thrashing on Intel hybrid CPUs.
	NumThread int `json:"num_thread,omitempty"`

	// ── High-impact performance flags ────────────────────────────────────────

	// FlashAttn enables Flash Attention — O(1) memory, faster at long context.
	// Requires Ollama ≥ 0.1.33. Passed as "flash_attn" in the options object.
	FlashAttn *bool `json:"flash_attn,omitempty"`

	// UseMmap controls whether Ollama loads weights via mmap (OS page cache).
	// When true, model startup is instant and pages are shared across processes.
	UseMmap *bool `json:"use_mmap,omitempty"`

	// UseMlock pins model weights into physical RAM, preventing swap eviction.
	// Eliminates page-fault stalls at the cost of locking RAM permanently.
	UseMlock *bool `json:"use_mlock,omitempty"`

	// LowVRAM skips pre-allocated scratch buffers and uses streaming attention.
	// Reduces peak RAM 15-25% at a small speed cost — for memory-constrained machines.
	LowVRAM *bool `json:"low_vram,omitempty"`

	// NumBatch controls the prompt evaluation batch size.
	// Smaller values reduce peak RAM during prefill (lean context mode).
	NumBatch int `json:"num_batch,omitempty"`
}

// ChatChunk is one SSE event from POST /api/chat (stream=true).
type ChatChunk struct {
	Model     string  `json:"model"`
	CreatedAt string  `json:"created_at"`
	Message   Message `json:"message"`
	Done      bool    `json:"done"`
	// Fields present only when Done==true:
	TotalDuration      int64 `json:"total_duration,omitempty"`
	LoadDuration       int64 `json:"load_duration,omitempty"`
	PromptEvalCount    int   `json:"prompt_eval_count,omitempty"`
	EvalCount          int   `json:"eval_count,omitempty"`
	EvalDuration       int64 `json:"eval_duration,omitempty"`
}

// GenerateChunk is one SSE event from POST /api/generate (stream=true).
type GenerateChunk struct {
	Model     string `json:"model"`
	CreatedAt string `json:"created_at"`
	Response  string `json:"response"`
	Done      bool   `json:"done"`
}

// PullRequest maps to POST /api/pull.
type PullRequest struct {
	Name   string `json:"name"`
	Stream bool   `json:"stream"`
}

// PullStatus is one SSE event from POST /api/pull.
type PullStatus struct {
	Status    string `json:"status"`
	Digest    string `json:"digest,omitempty"`
	Total     int64  `json:"total,omitempty"`
	Completed int64  `json:"completed,omitempty"`
}

// DeleteRequest maps to DELETE /api/delete.
type DeleteRequest struct {
	Name string `json:"name"`
}

// VersionResponse maps to GET /api/version.
type VersionResponse struct {
	Version string `json:"version"`
}

// ---------------------------------------------------------------------------
// Methods
// ---------------------------------------------------------------------------

// Version fetches the Ollama server version (also serves as a health check).
func (c *Client) Version(ctx context.Context) (string, error) {
	var v VersionResponse
	if err := c.getJSON(ctx, "/api/version", &v); err != nil {
		return "", err
	}
	return v.Version, nil
}

// ListModels returns all locally available models.
func (c *Client) ListModels(ctx context.Context) ([]Model, error) {
	var resp struct {
		Models []Model `json:"models"`
	}
	if err := c.getJSON(ctx, "/api/tags", &resp); err != nil {
		return nil, err
	}
	return resp.Models, nil
}

// ChatStream sends a chat request and streams ChatChunk events to the returned channel.
// The caller must drain the channel. The channel is closed when the stream ends or ctx is cancelled.
func (c *Client) ChatStream(ctx context.Context, req ChatRequest) (<-chan ChatChunk, <-chan error) {
	ch := make(chan ChatChunk)
	errCh := make(chan error, 1)
	req.Stream = true

	go func() {
		defer close(ch)
		defer close(errCh)

		body, err := json.Marshal(req)
		if err != nil {
			errCh <- fmt.Errorf("marshal: %w", err)
			return
		}

		httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/api/chat", bytes.NewReader(body))
		if err != nil {
			errCh <- fmt.Errorf("request: %w", err)
			return
		}
		httpReq.Header.Set("Content-Type", "application/json")

		resp, err := c.httpClient.Do(httpReq)
		if err != nil {
			errCh <- fmt.Errorf("do: %w", err)
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			b, _ := io.ReadAll(resp.Body)
			errCh <- fmt.Errorf("ollama %d: %s", resp.StatusCode, string(b))
			return
		}

		scanner := bufio.NewScanner(resp.Body)
		for scanner.Scan() {
			line := scanner.Bytes()
			if len(line) == 0 {
				continue
			}
			var chunk ChatChunk
			if err := json.Unmarshal(line, &chunk); err != nil {
				errCh <- fmt.Errorf("decode chunk: %w", err)
				return
			}
			select {
			case ch <- chunk:
			case <-ctx.Done():
				return
			}
			if chunk.Done {
				return
			}
		}
		if err := scanner.Err(); err != nil && ctx.Err() == nil {
			errCh <- fmt.Errorf("scan: %w", err)
		}
	}()

	return ch, errCh
}

// GenerateStream sends a generate request and streams GenerateChunk events.
func (c *Client) GenerateStream(ctx context.Context, req GenerateRequest) (<-chan GenerateChunk, <-chan error) {
	ch := make(chan GenerateChunk)
	errCh := make(chan error, 1)
	req.Stream = true

	go func() {
		defer close(ch)
		defer close(errCh)

		body, err := json.Marshal(req)
		if err != nil {
			errCh <- fmt.Errorf("marshal: %w", err)
			return
		}

		httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/api/generate", bytes.NewReader(body))
		if err != nil {
			errCh <- fmt.Errorf("request: %w", err)
			return
		}
		httpReq.Header.Set("Content-Type", "application/json")

		resp, err := c.httpClient.Do(httpReq)
		if err != nil {
			errCh <- fmt.Errorf("do: %w", err)
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			b, _ := io.ReadAll(resp.Body)
			errCh <- fmt.Errorf("ollama %d: %s", resp.StatusCode, string(b))
			return
		}

		scanner := bufio.NewScanner(resp.Body)
		for scanner.Scan() {
			line := scanner.Bytes()
			if len(line) == 0 {
				continue
			}
			var chunk GenerateChunk
			if err := json.Unmarshal(line, &chunk); err != nil {
				errCh <- fmt.Errorf("decode chunk: %w", err)
				return
			}
			select {
			case ch <- chunk:
			case <-ctx.Done():
				return
			}
			if chunk.Done {
				return
			}
		}
		if err := scanner.Err(); err != nil && ctx.Err() == nil {
			errCh <- fmt.Errorf("scan: %w", err)
		}
	}()

	return ch, errCh
}

// PullStream pulls a model and streams progress events.
func (c *Client) PullStream(ctx context.Context, name string) (<-chan PullStatus, <-chan error) {
	ch := make(chan PullStatus)
	errCh := make(chan error, 1)

	go func() {
		defer close(ch)
		defer close(errCh)

		body, _ := json.Marshal(PullRequest{Name: name, Stream: true})
		httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/api/pull", bytes.NewReader(body))
		if err != nil {
			errCh <- err
			return
		}
		httpReq.Header.Set("Content-Type", "application/json")

		resp, err := c.httpClient.Do(httpReq)
		if err != nil {
			errCh <- err
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			b, _ := io.ReadAll(resp.Body)
			errCh <- fmt.Errorf("ollama %d: %s", resp.StatusCode, string(b))
			return
		}

		scanner := bufio.NewScanner(resp.Body)
		for scanner.Scan() {
			line := scanner.Bytes()
			if len(line) == 0 {
				continue
			}
			var status PullStatus
			if err := json.Unmarshal(line, &status); err != nil {
				continue
			}
			select {
			case ch <- status:
			case <-ctx.Done():
				return
			}
		}
	}()

	return ch, errCh
}

// DeleteModel removes a model from Ollama.
func (c *Client) DeleteModel(ctx context.Context, name string) error {
	body, _ := json.Marshal(DeleteRequest{Name: name})
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, c.BaseURL+"/api/delete", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("ollama %d: %s", resp.StatusCode, string(b))
	}
	return nil
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func (c *Client) getJSON(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+path, nil)
	if err != nil {
		return err
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("ollama %d: %s", resp.StatusCode, string(b))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}