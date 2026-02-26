// Package features manages runtime-toggleable performance enhancement flags.
//
// Flags are stored in memory only — they reset on restart, which is intentional:
// this lets you A/B test by restarting containers with different defaults.
//
// Available features:
//
//	flash_attn       — enable Flash Attention in Ollama (O(1) memory, faster long-context)
//	mmap_weights     — load model weights via mmap (OS page cache, instant cold start)
//	mlock_weights    — lock model weights into RAM (no swap, eliminates page-fault stalls)
//	lean_context     — cap num_ctx at 512 to shrink KV cache ~8x (footprint reduction)
//	low_vram         — disable Ollama scratch buffers; streaming attention (footprint reduction)
//	aggressive_quant — pull one quant tier lower than recommended to fit larger models in RAM
//	batching         — queue concurrent requests and drain them serially to Ollama
//	prefix_cache     — reuse in-memory KV context when prompt prefix matches a recent request
//	quant_advisor    — surface quant recommendations based on available RAM
//	thread_hint      — pass num_thread=PCores to Ollama, skipping efficiency cores
package features

import (
	"sync"
)

// FeatureID is a unique key for a feature flag.
type FeatureID string

const (
	// Performance flags
	FlashAttn    FeatureID = "flash_attn"
	MmapWeights  FeatureID = "mmap_weights"
	MLockWeights FeatureID = "mlock_weights"

	// Footprint reduction flags
	LeanContext     FeatureID = "lean_context"
	LowVRAM         FeatureID = "low_vram"
	AggressiveQuant FeatureID = "aggressive_quant"

	// Utility flags
	Batching     FeatureID = "batching"
	PrefixCache  FeatureID = "prefix_cache"
	QuantAdvisor FeatureID = "quant_advisor"
	ThreadHint   FeatureID = "thread_hint"
)

// Info describes a feature flag for display in the UI.
type Info struct {
	ID          FeatureID `json:"id"`
	Name        string    `json:"name"`
	Description string    `json:"description"`
	Enabled     bool      `json:"enabled"`
}

// Store holds the current enabled/disabled state of all feature flags.
type Store struct {
	mu    sync.RWMutex
	flags map[FeatureID]bool
}

// NewStore creates a Store with all features disabled by default.
func NewStore() *Store {
	return &Store{
		flags: map[FeatureID]bool{
			FlashAttn:       false,
			MmapWeights:     false,
			MLockWeights:    false,
			LeanContext:     false,
			LowVRAM:         false,
			AggressiveQuant: false,
			Batching:        false,
			PrefixCache:     false,
			QuantAdvisor:    false,
			ThreadHint:      false,
		},
	}
}

// IsEnabled returns true if the given feature is currently on.
func (s *Store) IsEnabled(id FeatureID) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.flags[id]
}

// Set enables or disables a feature.  Returns false if the id is unknown.
func (s *Store) Set(id FeatureID, enabled bool) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.flags[id]; !ok {
		return false
	}
	s.flags[id] = enabled
	return true
}

// All returns a slice of Info for every known feature, in display order.
func (s *Store) All() []Info {
	s.mu.RLock()
	defer s.mu.RUnlock()

	return []Info{
		{
			ID:          FlashAttn,
			Name:        "Flash Attention",
			Description: "Enable Flash Attention in Ollama. Computes attention in tiled blocks — O(1) memory instead of O(n²) — dramatically faster at long context lengths (2k+ tokens). Requires Ollama ≥ 0.1.33.",
			Enabled:     s.flags[FlashAttn],
		},
		{
			ID:          MmapWeights,
			Name:        "Memory-Map Weights (mmap)",
			Description: "Load model weights via OS mmap instead of reading them into a private buffer. The OS page-cache serves weights directly to Ollama, enabling instant cold-start and shared pages across processes. Pairs well with MLock.",
			Enabled:     s.flags[MmapWeights],
		},
		{
			ID:          MLockWeights,
			Name:        "Lock Weights in RAM (mlock)",
			Description: "Pin model weights into physical RAM so the OS can never swap them to disk. Eliminates page-fault stalls during inference — highest impact when system RAM is under pressure. May require elevated OS limits (ulimit -l unlimited).",
			Enabled:     s.flags[MLockWeights],
		},
		// ── Footprint Reduction ───────────────────────────────────────────────
		{
			ID:          LeanContext,
			Name:        "Lean Context Window",
			Description: "Caps the KV context to 512 tokens — shrinks KV cache memory ~8× compared to the default 4096. Lets you run a model that would otherwise OOM. Conversations longer than 512 tokens will be truncated. Best combined with Low VRAM mode.",
			Enabled:     s.flags[LeanContext],
		},
		{
			ID:          LowVRAM,
			Name:        "Low VRAM Mode",
			Description: "Tells Ollama to skip pre-allocated scratch buffers and use streaming attention instead. Reduces peak RAM by 15–25% at a small speed cost. Essential when running near the edge of available memory.",
			Enabled:     s.flags[LowVRAM],
		},
		{
			ID:          AggressiveQuant,
			Name:        "Aggressive Quantization",
			Description: "When pulling a model, drops one full quant tier below what the advisor recommends (e.g. Q4_K_M → Q2_K). Lets you fit a larger model at the cost of noticeably lower quality. Requires Quantization Advisor to also be enabled.",
			Enabled:     s.flags[AggressiveQuant],
		},
		// ── Utility ───────────────────────────────────────────────────────────
		{
			ID:          ThreadHint,
			Name:        "Thread Affinity Hint",
			Description: "⚠ Only useful on Intel hybrid CPUs (12th-gen+) with P+E cores. Sends num_thread=P-cores to Ollama. On Apple Silicon or any uniform-core CPU this forces a model reload and HURTS performance — leave it OFF unless you have a hybrid Intel CPU.",
			Enabled:     s.flags[ThreadHint],
		},
		{
			ID:          PrefixCache,
			Name:        "Prompt Prefix Cache",
			Description: "When a new request shares the same system prompt + conversation prefix as a recent one, hint Ollama to reuse its in-memory KV context instead of re-evaluating tokens.",
			Enabled:     s.flags[PrefixCache],
		},
		{
			ID:          Batching,
			Name:        "Request Batching",
			Description: "⚠ Serialises ALL requests through a single queue. Only useful when running as a shared multi-user server. For single-user use this will make every message wait for the previous one to finish — leave it OFF.",
			Enabled:     s.flags[Batching],
		},
		{
			ID:          QuantAdvisor,
			Name:        "Quantization Advisor",
			Description: "Analyse available system RAM and recommend the best quantization tier when pulling models. Displayed as a badge next to each model in search results.",
			Enabled:     s.flags[QuantAdvisor],
		},
	}
}