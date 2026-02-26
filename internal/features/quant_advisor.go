package features

import (
	"bufio"
	"os"
	"runtime"
	"strconv"
	"strings"
)

// QuantTier describes a recommended quantization level.
type QuantTier struct {
	Tag         string  `json:"tag"`
	Label       string  `json:"label"`
	Description string  `json:"description"`
	MinRAMGB    float64 `json:"min_ram_gb"`
}

// quantTiers lists known Ollama quantization suffixes in descending quality order.
// Each tier requires progressively more RAM.
var quantTiers = []QuantTier{
	{"q8_0", "Q8_0", "Near-lossless, ~8 bits/weight — best quality", 32},
	{"q6_K", "Q6_K", "6-bit k-quant — excellent quality/size balance", 24},
	{"q5_K_M", "Q5_K_M", "5-bit medium — great quality, moderate RAM", 20},
	{"q4_K_M", "Q4_K_M", "4-bit medium — sweet spot for most machines", 16},
	{"q4_K_S", "Q4_K_S", "4-bit small — slightly lower quality", 12},
	{"q3_K_M", "Q3_K_M", "3-bit medium — low RAM but noticeable quality loss", 8},
	{"q2_K", "Q2_K", "2-bit — emergency option, significant quality loss", 4},
}

// AvailableRAMGB returns the RAM available to the current process in gigabytes.
//
// Priority order (highest to lowest):
//  1. cgroup v2 memory limit  (/sys/fs/cgroup/memory.max)          — Docker/k8s containers
//  2. cgroup v1 memory limit  (/sys/fs/cgroup/memory/memory.limit_in_bytes)
//  3. /proc/meminfo MemTotal                                        — Linux host RAM
//  4. Platform sysctl (macOS hw.memsize)
//  5. Go runtime Sys bytes or 8 GB default
//
// Reading the cgroup limit before /proc/meminfo means a container with
// --memory=1g correctly reports 1 GB instead of the host's 64 GB.
func AvailableRAMGB() float64 {
	// 1. cgroup v2 — "max" means unlimited, skip.
	if gb := readCgroupV2MemLimit(); gb > 0 {
		return gb
	}
	// 2. cgroup v1
	if gb := readCgroupV1MemLimit(); gb > 0 {
		return gb
	}
	// 3. Linux /proc/meminfo (host or unconstrained container)
	if gb := readProcMeminfo(); gb > 0 {
		return gb
	}
	// 4. Platform-specific (macOS sysctl, etc.)
	if gb := detectSysRAMGB(); gb > 0 {
		return gb
	}
	// 5. Last resort
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	gb := float64(ms.Sys) / 1e9
	if gb < 1 {
		return 8
	}
	return gb
}

// readCgroupV2MemLimit reads the memory limit from cgroup v2.
// Returns 0 if the file is absent, "max" (unlimited), or cannot be parsed.
func readCgroupV2MemLimit() float64 {
	data, err := os.ReadFile("/sys/fs/cgroup/memory.max")
	if err != nil {
		return 0
	}
	s := strings.TrimSpace(string(data))
	if s == "max" || s == "" {
		return 0 // unlimited
	}
	bytes, err := strconv.ParseInt(s, 10, 64)
	if err != nil || bytes <= 0 {
		return 0
	}
	return float64(bytes) / 1e9
}

// readCgroupV1MemLimit reads the memory limit from cgroup v1.
// Returns 0 if absent, at the OS maximum sentinel value, or unparseable.
func readCgroupV1MemLimit() float64 {
	data, err := os.ReadFile("/sys/fs/cgroup/memory/memory.limit_in_bytes")
	if err != nil {
		return 0
	}
	bytes, err := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
	if err != nil || bytes <= 0 {
		return 0
	}
	// The kernel uses a very large sentinel (PAGE_COUNTER_MAX) for "no limit".
	// Anything above 4 PB is effectively unlimited.
	const maxSentinel = 4 * 1024 * 1024 * 1024 * 1024 * 1024 // 4 PiB
	if bytes >= maxSentinel {
		return 0
	}
	return float64(bytes) / 1e9
}

// readProcMeminfo reads MemTotal from /proc/meminfo (Linux / Docker).
func readProcMeminfo() float64 {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "MemTotal:") {
			continue
		}
		// Format: "MemTotal:       16384000 kB"
		fields := strings.Fields(line)
		if len(fields) < 2 {
			return 0
		}
		kb, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil {
			return 0
		}
		return float64(kb) / (1024 * 1024) // kB → GB
	}
	return 0
}

// RecommendTier returns the highest-quality quant tier that fits in ramGB.
func RecommendTier(ramGB float64) QuantTier {
	for _, t := range quantTiers {
		if ramGB >= t.MinRAMGB {
			return t
		}
	}
	return quantTiers[len(quantTiers)-1] // q2_K always fits
}

// BestPullName returns the Ollama model name with the recommended quantization
// suffix appended (e.g. "llama3.2:q4_K_M").  If the name already contains a
// colon tag the original name is returned unchanged so we never override an
// explicit user selection.
func BestPullName(baseName string, ramGB float64) string {
	if strings.Contains(baseName, ":") {
		return baseName // user specified a tag already
	}
	tier := RecommendTier(ramGB)
	return baseName + ":" + tier.Tag
}

// AllTiers returns the full list of quantization tiers for display in the UI.
func AllTiers() []QuantTier {
	return quantTiers
}

// ShrinkRAMForAggressiveQuant returns an artificially reduced RAM value that
// causes RecommendTier to select one tier lower than it normally would.
// For example, if the machine has 16 GB (→ Q4_K_M), this returns a value
// that lands in the Q3_K_M band instead, fitting a larger model at lower quality.
func ShrinkRAMForAggressiveQuant(ramGB float64) float64 {
	recommended := RecommendTier(ramGB)
	for i, t := range quantTiers {
		if t.Tag == recommended.Tag && i+1 < len(quantTiers) {
			// Return a value just below the current tier's minimum so the next
			// (lower quality) tier is selected instead.
			return quantTiers[i+1].MinRAMGB - 0.1
		}
	}
	// Already at the lowest tier — can't go lower.
	return ramGB
}

// AnnotateSearchResults returns the recommended quant label for this machine
// (e.g. "Q4_K_M") to display as a badge in model search results.
func AnnotateSearchResults(ramGB float64) string {
	return RecommendTier(ramGB).Label
}