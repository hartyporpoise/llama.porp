// CPU feature detection for ARM64 (Apple Silicon, AWS Graviton, etc.).
package cpu

import "golang.org/x/sys/cpu"

// detectFeatures fills in ARM64 SIMD capability flags.
func detectFeatures(t *Topology) {
	// NEON is mandatory on ARMv8-A — every arm64 CPU has it.
	t.HasNEON = true

	// SVE — Scalable Vector Extension; Graviton 3, Cortex-X series, etc.
	t.HasSVE = cpu.ARM64.HasSVE

	// x86-specific flags are absent on ARM.
	t.HasAVX = false
	t.HasAVX2 = false
	t.HasAVX512 = false
	t.HasAMX = false
	t.HasF16C = false
	t.HasFMA = false
}
