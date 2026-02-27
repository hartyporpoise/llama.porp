// Package features — semantic_cache.go implements an in-memory response cache
// keyed by embedding similarity. When a user asks a question semantically
// similar to a previously answered one, the cached response is returned
// instantly instead of running inference again.
package features

import (
	"math"
	"sync"
	"time"
)

// SemanticCacheEntry holds a cached response and its embedding.
type SemanticCacheEntry struct {
	Embedding []float64
	Response  string
	Model     string // model that generated the response
	StoredAt  time.Time
}

// SemanticCacheStore is a thread-safe, LRU-evicting cache of responses keyed by
// cosine similarity of their query embeddings.
type SemanticCacheStore struct {
	mu        sync.RWMutex
	entries   []SemanticCacheEntry
	maxSize   int
	threshold float64 // cosine similarity threshold for a cache hit (e.g. 0.92)
}

// NewSemanticCache creates a SemanticCacheStore.
// maxSize is the maximum number of entries (LRU eviction when full).
// threshold is the minimum cosine similarity for a cache hit (0.0–1.0).
func NewSemanticCache(maxSize int, threshold float64) *SemanticCacheStore {
	return &SemanticCacheStore{
		entries:   make([]SemanticCacheEntry, 0, maxSize),
		maxSize:   maxSize,
		threshold: threshold,
	}
}

// Lookup finds the most similar cached response. Returns the response and true
// on a hit, or ("", false) on a miss.
func (sc *SemanticCacheStore) Lookup(embedding []float64, model string) (string, bool) {
	sc.mu.RLock()
	defer sc.mu.RUnlock()

	bestSim := -1.0
	bestIdx := -1
	for i, e := range sc.entries {
		// Only match against responses from the same model.
		if e.Model != model {
			continue
		}
		sim := cosineSimilarity(embedding, e.Embedding)
		if sim > bestSim {
			bestSim = sim
			bestIdx = i
		}
	}
	if bestIdx >= 0 && bestSim >= sc.threshold {
		return sc.entries[bestIdx].Response, true
	}
	return "", false
}

// Store adds a new entry to the cache. If the cache is full, the oldest entry
// is evicted.
func (sc *SemanticCacheStore) Store(embedding []float64, response, model string) {
	sc.mu.Lock()
	defer sc.mu.Unlock()

	// Evict oldest if at capacity.
	if len(sc.entries) >= sc.maxSize {
		sc.entries = sc.entries[1:]
	}
	sc.entries = append(sc.entries, SemanticCacheEntry{
		Embedding: embedding,
		Response:  response,
		Model:     model,
		StoredAt:  time.Now(),
	})
}

// Len returns the current number of cached entries.
func (sc *SemanticCacheStore) Len() int {
	sc.mu.RLock()
	defer sc.mu.RUnlock()
	return len(sc.entries)
}

// cosineSimilarity computes the cosine similarity between two vectors.
// Returns 0 if either vector is zero-length or they have different dimensions.
func cosineSimilarity(a, b []float64) float64 {
	if len(a) != len(b) || len(a) == 0 {
		return 0
	}
	var dot, normA, normB float64
	for i := range a {
		dot += a[i] * b[i]
		normA += a[i] * a[i]
		normB += b[i] * b[i]
	}
	denom := math.Sqrt(normA) * math.Sqrt(normB)
	if denom == 0 {
		return 0
	}
	return dot / denom
}
