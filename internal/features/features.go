// Package features manages runtime-toggleable performance enhancement flags.
//
// Flags are stored in memory only — they reset on restart, which is intentional:
// this lets you A/B test by restarting containers with different defaults.
//
// User-visible flags (returned by All()):
//
//	semantic_cache       — cache responses by embedding similarity
//	smart_context        — compress conversation history to reduce prefill
//	speculative_decoding — draft model predicts tokens for batch verification
//	auto_compaction      — LLM-powered conversation compaction when context fills up
package features

import (
	"sync"
)

// FeatureID is a unique key for a feature flag.
type FeatureID string

const (
	// SemanticCacheFlag caches responses keyed by embedding similarity.
	// Similar questions get instant cached answers.
	SemanticCacheFlag FeatureID = "semantic_cache"

	// SmartContext compresses long conversation history before sending to
	// Ollama, keeping only the most relevant messages to reduce prefill time.
	SmartContext FeatureID = "smart_context"

	// SpeculativeDecoding uses a small draft model to predict tokens, then
	// sends them as a prefix to the target model for batch verification.
	// The draft model is user-selectable via the settings UI.
	SpeculativeDecoding FeatureID = "speculative_decoding"

	// AutoCompaction asks the LLM to summarize the conversation when context
	// usage exceeds 80%. The summary replaces the chat history so the
	// conversation can continue without hitting the context limit.
	AutoCompaction FeatureID = "auto_compaction"
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
	mu         sync.RWMutex
	flags      map[FeatureID]bool
	draftModel string // user-selected draft model for speculative decoding
}

// NewStore creates a Store with all features disabled by default.
func NewStore() *Store {
	return &Store{
		flags: map[FeatureID]bool{
			SemanticCacheFlag:   false,
			SmartContext:        false,
			SpeculativeDecoding: false,
			AutoCompaction:      false,
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

// DraftModel returns the currently configured draft model for speculative decoding.
// Returns empty string if none is set.
func (s *Store) DraftModel() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.draftModel
}

// SetDraftModel sets the draft model used for speculative decoding.
func (s *Store) SetDraftModel(model string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.draftModel = model
}

// All returns the user-visible feature flags in display order.
func (s *Store) All() []Info {
	s.mu.RLock()
	defer s.mu.RUnlock()

	return []Info{
		{
			ID:   SemanticCacheFlag,
			Name: "Semantic Cache",
			Description: "Cache responses by semantic similarity. Similar questions get instant " +
				"cached answers instead of running inference. Auto-pulls nomic-embed-text.",
			Enabled: s.flags[SemanticCacheFlag],
		},
		{
			ID:   SmartContext,
			Name: "Smart Context",
			Description: "Compress long conversation history before sending to Ollama. Keeps the " +
				"first and last messages, drops the middle. Dramatically reduces prefill time.",
			Enabled: s.flags[SmartContext],
		},
		{
			ID:   SpeculativeDecoding,
			Name: "Speculative Decode",
			Description: "Use a small draft model to predict tokens, then send them " +
				"as a prefix to the target model for batch verification.",
			Enabled: s.flags[SpeculativeDecoding],
		},
		{
			ID:   AutoCompaction,
			Name: "Auto-Compaction",
			Description: "When context usage exceeds 80%, ask the LLM to summarize the " +
				"conversation. The summary replaces chat history so you never hit the context limit.",
			Enabled: s.flags[AutoCompaction],
		},
	}
}
