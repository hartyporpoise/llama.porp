package features

import (
	"crypto/sha256"
	"fmt"
	"sync"
	"time"
)

// PrefixEntry records the last time a given prompt prefix was seen.
// We use this to tell Ollama it can keep its context alive and skip re-evaluation.
type PrefixEntry struct {
	Hash      string
	NumTokens int   // estimated token count of the prefix
	SeenAt    time.Time
}

// PrefixCacheStore stores a small window of recent prompt prefixes keyed by their SHA-256.
// Thread-safe; entries expire after ttl.
type PrefixCacheStore struct {
	mu      sync.Mutex
	entries map[string]*PrefixEntry
	ttl     time.Duration
	maxSize int
}

// NewPrefixCache creates a PrefixCacheStore with the given TTL and max number of entries.
func NewPrefixCache(ttl time.Duration, maxSize int) *PrefixCacheStore {
	return &PrefixCacheStore{
		entries: make(map[string]*PrefixEntry),
		ttl:     ttl,
		maxSize: maxSize,
	}
}

// HashPrefix returns a deterministic string key for a system prompt + messages prefix.
// We hash the first portion of the conversation (system + all messages except the last user turn)
// because that is the part Ollama would re-evaluate on every request.
func HashPrefix(systemPrompt string, prefixMessages []string) string {
	h := sha256.New()
	h.Write([]byte(systemPrompt))
	for _, m := range prefixMessages {
		h.Write([]byte(m))
	}
	return fmt.Sprintf("%x", h.Sum(nil))[:16] // first 16 hex chars is plenty
}

// Lookup checks whether a prefix hash is cached and still fresh.
// Returns (entry, true) on a hit.
func (pc *PrefixCacheStore) Lookup(hash string) (*PrefixEntry, bool) {
	pc.mu.Lock()
	defer pc.mu.Unlock()
	e, ok := pc.entries[hash]
	if !ok {
		return nil, false
	}
	if time.Since(e.SeenAt) > pc.ttl {
		delete(pc.entries, hash)
		return nil, false
	}
	return e, true
}

// Store upserts an entry.  Evicts oldest entries when the cache is full.
func (pc *PrefixCacheStore) Store(hash string, numTokens int) {
	pc.mu.Lock()
	defer pc.mu.Unlock()

	// Evict stale entries first.
	now := time.Now()
	for k, v := range pc.entries {
		if now.Sub(v.SeenAt) > pc.ttl {
			delete(pc.entries, k)
		}
	}

	// If still over capacity, remove the oldest live entry.
	for len(pc.entries) >= pc.maxSize {
		var oldest string
		var oldestTime time.Time
		for k, v := range pc.entries {
			if oldest == "" || v.SeenAt.Before(oldestTime) {
				oldest = k
				oldestTime = v.SeenAt
			}
		}
		delete(pc.entries, oldest)
	}

	pc.entries[hash] = &PrefixEntry{
		Hash:      hash,
		NumTokens: numTokens,
		SeenAt:    now,
	}
}