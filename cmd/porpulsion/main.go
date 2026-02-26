// Porpulsion — Intelligent UI & scheduler layer for Ollama
//
// Usage:
//
//	porpulsion serve
//	porpulsion serve --ollama-url http://localhost:11434 --port 8080
package main

import (
	"fmt"
	"os"

	"context"

	"github.com/hartyporpoise/porpulsion/internal/api"
	"github.com/hartyporpoise/porpulsion/internal/config"
	"github.com/hartyporpoise/porpulsion/internal/cpu"
	"github.com/hartyporpoise/porpulsion/internal/metrics"
	"github.com/hartyporpoise/porpulsion/internal/ollama"
	"github.com/spf13/cobra"
)

const banner = `
██████╗  ██████╗ ██████╗ ██████╗ ██╗   ██╗██╗     ███████╗██╗ ██████╗ ███╗   ██╗
██╔══██╗██╔═══██╗██╔══██╗██╔══██╗██║   ██║██║     ██╔════╝██║██╔═══██╗████╗  ██║
██████╔╝██║   ██║██████╔╝██████╔╝██║   ██║██║     ███████╗██║██║   ██║██╔██╗ ██║
██╔═══╝ ██║   ██║██╔══██╗██╔═══╝ ██║   ██║██║     ╚════██║██║██║   ██║██║╚██╗██║
██║     ╚██████╔╝██║  ██║██║     ╚██████╔╝███████╗███████║██║╚██████╔╝██║ ╚████║
╚═╝      ╚═════╝ ╚═╝  ╚═╝╚═╝      ╚═════╝ ╚══════╝╚══════╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝

  Powered by Ollama  ·  github.com/hartyporpoise/porpulsion
`

func main() {
	var cfg config.Config

	root := &cobra.Command{
		Use:   "porpulsion",
		Short: "Porpulsion — smart UI and proxy for Ollama",
		Long:  banner,
	}

	serve := &cobra.Command{
		Use:   "serve",
		Short: "Start the porpulsion server",
		RunE: func(cmd *cobra.Command, args []string) error {
			return runServe(&cfg)
		},
	}

	f := serve.Flags()
	f.StringVar(&cfg.OllamaURL, "ollama-url", envOrDefault("OLLAMA_URL", "http://ollama:11434"),
		"Ollama API base URL")
	f.StringVarP(&cfg.DefaultModel, "model", "m", "",
		"Default model name (e.g. llama3.2)")
	f.IntVarP(&cfg.Port, "port", "p", 8080, "HTTP port")
	f.StringVar(&cfg.Host, "host", "0.0.0.0", "Bind address")
	f.StringVar(&cfg.OllamaEnvDir, "ollama-env-dir", envOrDefault("OLLAMA_ENV_DIR", ""),
		"Shared volume path for Ollama env file + restart sentinel (empty = disabled)")

	root.AddCommand(serve)
	if err := root.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func runServe(cfg *config.Config) error {
	fmt.Print(banner)

	// ── 1. CPU detection (for dashboard display) ──────────────────────────
	topo, err := cpu.Detect()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: CPU detection error: %v\n", err)
		topo = &cpu.Topology{LogicalCores: 4, PhysicalCores: 4, PCores: 4, NUMANodes: 1}
	}
	fmt.Printf("CPU:   %s\n", topo.ModelName)
	fmt.Printf("Cores: %d physical / %d logical\n", topo.PhysicalCores, topo.LogicalCores)
	fmt.Printf("SIMD:  %s\n\n", cpu.FeatureSummary(topo))

	// ── 2. Connect to Ollama ───────────────────────────────────────────────
	fmt.Printf("Ollama: %s\n", cfg.OllamaURL)
	oc := ollama.NewClient(cfg.OllamaURL)

	// Non-fatal: Ollama may not be up yet (docker-compose startup ordering).
	if v, err := oc.Version(context.Background()); err == nil {
		fmt.Printf("Ollama version: %s\n", v)
	} else {
		fmt.Fprintf(os.Stderr, "Warning: cannot reach Ollama at %s (%v) — retries will happen per-request\n",
			cfg.OllamaURL, err)
	}

	// ── 3. Start HTTP server ───────────────────────────────────────────────
	mc := metrics.NewCollector()
	srv := api.NewServer(cfg, topo, oc, mc)
	return srv.Run(fmt.Sprintf("%s:%d", cfg.Host, cfg.Port))
}

// envOrDefault returns the value of an env var, or fallback if unset.
func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}