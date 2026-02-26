package features

import (
	"context"
	"sync"
)

// WorkFn is a unit of work submitted to the Batcher.
// It is called with a context that is cancelled if the batcher is stopped.
type WorkFn func(ctx context.Context)

// Batcher serialises concurrent requests through a single worker goroutine.
// When batching is disabled, work is executed directly (zero overhead).
//
// Purpose: when multiple users hit Ollama at the same time, each request
// competes for memory bandwidth.  Draining them one-at-a-time trades latency
// for throughput: total tokens/sec is higher because the CPU caches stay warm.
type Batcher struct {
	mu      sync.Mutex
	enabled bool
	queue   chan workItem
	once    sync.Once
	stop    chan struct{}
}

type workItem struct {
	fn   WorkFn
	ctx  context.Context
	done chan struct{}
}

// NewBatcher creates a Batcher.  The internal goroutine starts lazily on
// the first queued request.
func NewBatcher() *Batcher {
	return &Batcher{
		queue: make(chan workItem, 256),
		stop:  make(chan struct{}),
	}
}

// SetEnabled toggles batching mode.  Safe to call at runtime.
func (b *Batcher) SetEnabled(on bool) {
	b.mu.Lock()
	b.enabled = on
	b.mu.Unlock()

	if on {
		// Start the drain goroutine once.
		b.once.Do(func() {
			go b.drain()
		})
	}
}

// IsEnabled returns the current batching mode.
func (b *Batcher) IsEnabled() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.enabled
}

// Do executes fn either immediately (batching off) or via the serial queue.
// Blocks until fn completes.
func (b *Batcher) Do(ctx context.Context, fn WorkFn) {
	b.mu.Lock()
	enabled := b.enabled
	b.mu.Unlock()

	if !enabled {
		fn(ctx)
		return
	}

	item := workItem{fn: fn, ctx: ctx, done: make(chan struct{})}
	select {
	case b.queue <- item:
		// Queued — wait for drain goroutine to execute it.
		select {
		case <-item.done:
		case <-ctx.Done():
			// Caller gave up; the item may still be drained but the caller
			// has already returned — no harm done since workFn checks its ctx.
		}
	case <-ctx.Done():
		// Queue full and caller cancelled.
	}
}

// drain is the single worker goroutine.  Runs forever once started.
func (b *Batcher) drain() {
	for {
		select {
		case item := <-b.queue:
			item.fn(item.ctx)
			close(item.done)
		case <-b.stop:
			return
		}
	}
}

// Stop shuts down the drain goroutine (for clean shutdown).
func (b *Batcher) Stop() {
	close(b.stop)
}