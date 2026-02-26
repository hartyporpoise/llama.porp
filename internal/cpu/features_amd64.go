// CPU feature detection for x86-64 using the CPUID instruction.
// Each feature is probed individually — we never assume AVX-512 or AMX
// simply because AVX2 is present.  Older CPUs that lack a feature will
// just have the corresponding flag set to false.
package cpu

import "golang.org/x/sys/cpu"

// detectFeatures fills in x86-64 SIMD capability flags.
func detectFeatures(t *Topology) {
	// AVX — Sandy Bridge (2011) and later.
	t.HasAVX = cpu.X86.HasAVX

	// AVX2 — Haswell (2013) and later; the most common "fast path" on x86.
	t.HasAVX2 = cpu.X86.HasAVX2

	// F16C — half-precision ↔ single-precision conversion.
	// golang.org/x/sys/cpu does not expose HasF16C; leave false (safe default).
	t.HasF16C = false

	// FMA — fused multiply-add; required for full AVX2 throughput.
	// golang.org/x/sys/cpu exposes this as HasFMA (covers FMA3 on x86-64).
	t.HasFMA = cpu.X86.HasFMA

	// AVX-512 — Skylake-SP (2017) server CPUs, select desktop/laptop chips.
	// NOT present on most consumer hardware, including many modern Intel/AMD CPUs.
	t.HasAVX512 = cpu.X86.HasAVX512F

	// Intel AMX — Advanced Matrix Extensions, Sapphire Rapids (2023) and later.
	// Delivers massive INT8/BF16 throughput via 2-D tile registers.
	// Check for AMX-BF16 specifically (the variant llama.cpp uses).
	t.HasAMX = cpu.X86.HasAMXBF16

	// ARM NEON / SVE are not present on x86-64.
	t.HasNEON = false
	t.HasSVE = false
}
