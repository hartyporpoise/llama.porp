// Package cpu detects CPU topology and feature flags at runtime.
// Detection is best-effort: if a feature cannot be confirmed it is
// reported as absent, ensuring porpulsion degrades gracefully on
// older hardware (no AVX-512, no AMX, etc.).
package cpu

import (
	"bufio"
	"os"
	"runtime"
	"strconv"
	"strings"
)

// Topology describes the CPU landscape of the current machine.
type Topology struct {
	// ModelName is the human-readable CPU model string (e.g. "Intel Core i9-13900K").
	ModelName string

	// PhysicalCores is the number of physical (not hyper-threaded) cores.
	PhysicalCores int

	// LogicalCores is the total number of logical CPUs (hyper-threading included).
	LogicalCores int

	// PCores is the count of Performance cores on Intel hybrid chips.
	// Equals LogicalCores on non-hybrid CPUs.
	PCores int

	// ECores is the count of Efficiency cores on Intel hybrid chips (0 on others).
	ECores int

	// NUMANodes is the number of NUMA memory nodes detected.
	NUMANodes int

	// L3CacheBytes is the total L3 cache in bytes (0 if not detected).
	L3CacheBytes int64

	// ---- Feature flags ----
	// Each flag is only true when confirmed present; false = absent or unknown.

	// HasAVX indicates SSE/AVX support — virtually universal on x86-64.
	HasAVX bool

	// HasAVX2 indicates AVX2 support (Intel Haswell 2013+, AMD Ryzen 1st gen+).
	HasAVX2 bool

	// HasAVX512 indicates AVX-512 support (Intel Skylake-SP+, some Ryzen 9000+).
	// NOT present on most consumer CPUs — porpulsion will fall back to AVX2.
	HasAVX512 bool

	// HasAMX indicates Intel AMX tile-based matrix extension (Sapphire Rapids 2023+).
	// Extremely fast for INT8/BF16 — far from universal.
	HasAMX bool

	// HasF16C indicates hardware FP16↔FP32 conversion.
	HasF16C bool

	// HasFMA indicates fused multiply-add (required for good FP32 throughput).
	HasFMA bool

	// HasNEON indicates ARM NEON SIMD (Apple Silicon, AWS Graviton, etc.).
	HasNEON bool

	// HasSVE indicates ARM Scalable Vector Extension (Graviton 3+, Cortex-X series).
	HasSVE bool
}

// Detect reads the CPU topology and feature flags of the current machine.
// It never returns a hard error; on any read failure it falls back to
// conservative defaults so the server always starts.
func Detect() (*Topology, error) {
	t := &Topology{
		LogicalCores:  runtime.NumCPU(),
		PhysicalCores: runtime.NumCPU(), // refined below
		PCores:        runtime.NumCPU(),
		NUMANodes:     1,
	}

	detectPlatformTopology(t)

	// Feature detection is always CPUID-based (cross-platform).
	detectFeatures(t)

	// Clamp PCores to logical count in case of parse errors.
	if t.PCores > t.LogicalCores {
		t.PCores = t.LogicalCores
	}
	if t.PCores == 0 {
		t.PCores = t.LogicalCores
	}

	return t, nil
}

// OptimalThreadCount returns the recommended number of inference threads for
// the given topology. It avoids Efficiency cores (they hurt token throughput)
// and leaves one logical thread free for OS bookkeeping.
func OptimalThreadCount(t *Topology) int {
	cores := t.PCores
	if cores <= 0 {
		cores = t.LogicalCores
	}
	if cores > 2 {
		return cores - 1
	}
	return cores
}

// FeatureSummary returns a short human-readable string of detected SIMD features.
func FeatureSummary(t *Topology) string {
	var parts []string
	if t.HasAVX {
		parts = append(parts, "AVX")
	}
	if t.HasAVX2 {
		parts = append(parts, "AVX2")
	}
	if t.HasF16C {
		parts = append(parts, "F16C")
	}
	if t.HasFMA {
		parts = append(parts, "FMA")
	}
	if t.HasAVX512 {
		parts = append(parts, "AVX-512")
	}
	if t.HasAMX {
		parts = append(parts, "AMX")
	}
	if t.HasNEON {
		parts = append(parts, "NEON")
	}
	if t.HasSVE {
		parts = append(parts, "SVE")
	}
	if len(parts) == 0 {
		return "none detected"
	}
	return strings.Join(parts, " ")
}

// ---------------------------------------------------------------------------
// Linux helpers
// ---------------------------------------------------------------------------

// parseLinuxCPUInfo reads /proc/cpuinfo for model name, core counts,
// and L3 cache info where available.
func parseLinuxCPUInfo(t *Topology) {
	f, err := os.Open("/proc/cpuinfo")
	if err != nil {
		return
	}
	defer f.Close()

	physicalIDs := map[string]struct{}{}
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		key, val, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		val = strings.TrimSpace(val)

		switch key {
		case "model name":
			if t.ModelName == "" {
				t.ModelName = val
			}
		case "physical id":
			physicalIDs[val] = struct{}{}
		}
	}

	if len(physicalIDs) > 0 {
		// rough: assume symmetric multi-socket
		t.PhysicalCores = t.LogicalCores / len(physicalIDs)
	}

	// Detect Intel hybrid (P/E cores) via /sys
	detectLinuxHybridCores(t)

	// L3 cache via sysfs
	detectLinuxL3Cache(t)
}

// detectLinuxHybridCores checks for Intel hybrid CPU topology via cpufreq.
// P-cores (Golden Cove / Raptor Cove) run at higher max frequency than
// E-cores (Gracemont), so we count unique max-frequency buckets.
func detectLinuxHybridCores(t *Topology) {
	pCores := 0
	eCores := 0

	for i := 0; i < t.LogicalCores; i++ {
		path := "/sys/devices/system/cpu/cpu" + strconv.Itoa(i) + "/cpu_capacity"
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		cap, err := strconv.Atoi(strings.TrimSpace(string(data)))
		if err != nil {
			continue
		}
		// Capacity values: P-cores ≈ 1024, E-cores ≈ 316 (exact values vary).
		if cap >= 700 {
			pCores++
		} else {
			eCores++
		}
	}

	if pCores > 0 {
		t.PCores = pCores
		t.ECores = eCores
	}
}

// detectLinuxNUMA counts NUMA nodes from /sys.
func detectLinuxNUMA(t *Topology) {
	entries, err := os.ReadDir("/sys/devices/system/node")
	if err != nil {
		return
	}
	count := 0
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "node") {
			count++
		}
	}
	if count > 0 {
		t.NUMANodes = count
	}
}

// detectLinuxL3Cache attempts to read L3 cache size from sysfs.
func detectLinuxL3Cache(t *Topology) {
	// Walk cache indices for cpu0 looking for a unified L3.
	for i := 0; i < 8; i++ {
		base := "/sys/devices/system/cpu/cpu0/cache/index" + strconv.Itoa(i)
		level, _ := os.ReadFile(base + "/level")
		ctype, _ := os.ReadFile(base + "/type")
		size, _ := os.ReadFile(base + "/size")

		if strings.TrimSpace(string(level)) == "3" &&
			strings.TrimSpace(string(ctype)) != "Instruction" {
			t.L3CacheBytes = parseKiloBytes(strings.TrimSpace(string(size)))
			return
		}
	}
}

// parseKiloBytes converts strings like "12288K" or "12M" to bytes.
func parseKiloBytes(s string) int64 {
	if strings.HasSuffix(s, "K") {
		v, _ := strconv.ParseInt(strings.TrimSuffix(s, "K"), 10, 64)
		return v * 1024
	}
	if strings.HasSuffix(s, "M") {
		v, _ := strconv.ParseInt(strings.TrimSuffix(s, "M"), 10, 64)
		return v * 1024 * 1024
	}
	v, _ := strconv.ParseInt(s, 10, 64)
	return v
}
