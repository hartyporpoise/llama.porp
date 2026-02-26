// Package ollamaenv manages a shared env file that the Ollama sidecar sources
// on startup. When porpulsion writes a new env file it also touches a restart
// sentinel so the Ollama wrapper script re-execs the process with the new env.
//
// Directory layout (shared emptyDir volume, default /shared):
//
//	/shared/ollama.env   — KEY=VALUE pairs sourced by the Ollama wrapper
//	/shared/restart      — sentinel: recreated each time a restart is requested
//
// The Ollama container must run under scripts/ollama-wrapper.sh (not the
// default ollama entrypoint) so it picks up the env file and watches the sentinel.
package ollamaenv

import (
	"fmt"
	"os"
	"path/filepath"
)

const (
	envFile      = "ollama.env"
	sentinelFile = "restart"
)

// Env holds the set of Ollama environment variables porpulsion manages.
type Env struct {
	FlashAttention bool // OLLAMA_FLASH_ATTENTION
	UseMMap        bool // OLLAMA_NOPRUNE (inverted — true = keep mmap)
	UseMlock       bool // OLLAMA_KEEP_ALIVE trick via OLLAMA_MAX_LOADED_MODELS? no — see below
	LowVRAM        bool // OLLAMA_GPU_OVERHEAD / not a standard var — best effort via num_gpu=0
}

// Write serialises e to <dir>/ollama.env and touches <dir>/restart so the
// Ollama wrapper script knows to re-exec the process.
// If dir is empty the call is a no-op (env management disabled).
func Write(dir string, e Env) error {
	if dir == "" {
		return nil
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}

	content := buildEnvFile(e)
	envPath := filepath.Join(dir, envFile)
	if err := os.WriteFile(envPath, []byte(content), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", envPath, err)
	}

	// Touch the sentinel to signal the wrapper script to restart Ollama.
	sentinelPath := filepath.Join(dir, sentinelFile)
	f, err := os.Create(sentinelPath)
	if err != nil {
		return fmt.Errorf("touch %s: %w", sentinelPath, err)
	}
	f.Close()
	return nil
}

// buildEnvFile returns the KEY=VALUE content for ollama.env.
func buildEnvFile(e Env) string {
	out := "# Managed by porpulsion — do not edit by hand\n"

	// Flash Attention: speeds up long-context inference (O(1) memory).
	if e.FlashAttention {
		out += "OLLAMA_FLASH_ATTENTION=1\n"
	} else {
		out += "OLLAMA_FLASH_ATTENTION=0\n"
	}

	// OLLAMA_NOPRUNE controls whether Ollama prunes unused KV blocks from mmap.
	// Setting it to 1 keeps the memory-mapped weights resident (effectively use_mmap=keep).
	if e.UseMMap {
		out += "OLLAMA_NOPRUNE=1\n"
	} else {
		out += "OLLAMA_NOPRUNE=0\n"
	}

	// OLLAMA_MAX_LOADED_MODELS=1 + ulimit is the closest we can get to mlock
	// without root. Setting OLLAMA_KEEP_ALIVE to a long value keeps the model
	// warm so it isn't evicted (avoiding reload page-faults).
	if e.UseMlock {
		out += "OLLAMA_KEEP_ALIVE=24h\n"
	} else {
		out += "OLLAMA_KEEP_ALIVE=5m\n"
	}

	// LowVRAM: reduce GPU scratch buffers.
	if e.LowVRAM {
		out += "OLLAMA_NUM_PARALLEL=1\n"
	} else {
		out += "# OLLAMA_NUM_PARALLEL unset (default)\n"
	}

	return out
}
