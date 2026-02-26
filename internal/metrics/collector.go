// Package metrics collects and exposes real-time inference statistics.
package metrics

import (
	"sync"
	"sync/atomic"
	"time"
)

// Snapshot is a point-in-time view of server metrics — safe to marshal to JSON.
type Snapshot struct {
	TotalRequests   int64   `json:"total_requests"`
	ActiveRequests  int64   `json:"active_requests"`
	TokensGenerated int64   `json:"tokens_generated"`
	TokensPerSecond float64 `json:"tokens_per_second"` // rolling 10-second window
	AvgTTFT         float64 `json:"avg_ttft_ms"`       // avg time-to-first-token (ms)
	AvgTPOT         float64 `json:"avg_tpot_ms"`       // avg time-per-output-token (ms)
	UptimeSeconds   float64 `json:"uptime_seconds"`
}

// Collector is a thread-safe metrics store.
type Collector struct {
	startTime time.Time

	totalRequests  atomic.Int64
	activeRequests atomic.Int64
	tokensTotal    atomic.Int64

	mu          sync.Mutex
	tokenEvents []tokenEvent // ring buffer for rolling TPS
	ttftSamples []float64
	tpotSamples []float64
}

type tokenEvent struct {
	at    time.Time
	count int64
}

// NewCollector creates and starts a Collector.
func NewCollector() *Collector {
	return &Collector{
		startTime: time.Now(),
	}
}

// RecordRequest increments the total request counter.
func (c *Collector) RecordRequest() {
	c.totalRequests.Add(1)
}

// RequestStart marks a request as active and returns a done function
// that should be deferred by the handler.
func (c *Collector) RequestStart() func() {
	c.activeRequests.Add(1)
	return func() {
		c.activeRequests.Add(-1)
	}
}

// RecordTokens records N tokens generated in the current window.
func (c *Collector) RecordTokens(n int64, ttftMs, tpotMs float64) {
	c.tokensTotal.Add(n)

	c.mu.Lock()
	defer c.mu.Unlock()

	c.tokenEvents = append(c.tokenEvents, tokenEvent{at: time.Now(), count: n})
	if ttftMs > 0 {
		c.ttftSamples = append(c.ttftSamples, ttftMs)
	}
	if tpotMs > 0 {
		c.tpotSamples = append(c.tpotSamples, tpotMs)
	}

	// Keep last 10 seconds of token events.
	cutoff := time.Now().Add(-10 * time.Second)
	for len(c.tokenEvents) > 0 && c.tokenEvents[0].at.Before(cutoff) {
		c.tokenEvents = c.tokenEvents[1:]
	}
	// Cap samples at 1000 entries.
	if len(c.ttftSamples) > 1000 {
		c.ttftSamples = c.ttftSamples[len(c.ttftSamples)-1000:]
	}
	if len(c.tpotSamples) > 1000 {
		c.tpotSamples = c.tpotSamples[len(c.tpotSamples)-1000:]
	}
}

// Snapshot returns current metrics as an immutable value.
func (c *Collector) Snapshot() Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Prune events older than 10 seconds on every read too, so TPS
	// decays to zero once generation stops.
	cutoff := time.Now().Add(-10 * time.Second)
	for len(c.tokenEvents) > 0 && c.tokenEvents[0].at.Before(cutoff) {
		c.tokenEvents = c.tokenEvents[1:]
	}

	// Rolling tokens-per-second over the last 10 seconds.
	var windowTokens int64
	for _, ev := range c.tokenEvents {
		windowTokens += ev.count
	}
	tps := float64(0)
	if len(c.tokenEvents) > 1 {
		window := c.tokenEvents[len(c.tokenEvents)-1].at.Sub(c.tokenEvents[0].at).Seconds()
		if window > 0 {
			tps = float64(windowTokens) / window
		}
	}

	return Snapshot{
		TotalRequests:   c.totalRequests.Load(),
		ActiveRequests:  c.activeRequests.Load(),
		TokensGenerated: c.tokensTotal.Load(),
		TokensPerSecond: tps,
		AvgTTFT:         average(c.ttftSamples),
		AvgTPOT:         average(c.tpotSamples),
		UptimeSeconds:   time.Since(c.startTime).Seconds(),
	}
}

func average(vals []float64) float64 {
	if len(vals) == 0 {
		return 0
	}
	var sum float64
	for _, v := range vals {
		sum += v
	}
	return sum / float64(len(vals))
}
