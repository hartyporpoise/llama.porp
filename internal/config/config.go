// Package config defines runtime configuration for porpulsion.
package config

// Config holds all settings passed in via CLI flags or environment variables.
type Config struct {
	// Host is the network interface to bind the HTTP server to.
	Host string

	// Port is the HTTP server port.
	Port int

	// OllamaURL is the base URL of the Ollama backend (e.g. "http://ollama:11434").
	OllamaURL string

	// DefaultModel is the model name to select when the request doesn't specify one.
	DefaultModel string
}
