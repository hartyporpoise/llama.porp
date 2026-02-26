// Darwin (macOS) specific CPU detection via sysctl.

//go:build darwin

package cpu

import (
	"os/exec"
	"strings"
)

// detectPlatformTopology is the Darwin implementation.
func detectPlatformTopology(t *Topology) {
	parseDarwinSysctl(t)
}

// parseDarwinSysctl populates topology fields using sysctl on macOS.
// Apple Silicon (ARM) is handled here too; x86 Macs use the same sysctls.
func parseDarwinSysctl(t *Topology) {
	t.ModelName = sysctlString("machdep.cpu.brand_string")

	// Physical / logical core counts
	if n := sysctlInt("hw.physicalcpu"); n > 0 {
		t.PhysicalCores = n
	}
	if n := sysctlInt("hw.logicalcpu"); n > 0 {
		t.LogicalCores = n
	}

	// Apple Silicon: performance + efficiency cores
	if p := sysctlInt("hw.perflevel0.physicalcpu"); p > 0 {
		t.PCores = p
	}
	if e := sysctlInt("hw.perflevel1.physicalcpu"); e > 0 {
		t.ECores = e
	}
	if t.PCores == 0 {
		t.PCores = t.PhysicalCores
	}

	// L3 cache (not all Apple Silicon chips have L3; M-series use system cache)
	if l3 := sysctlInt64("hw.l3cachesize"); l3 > 0 {
		t.L3CacheBytes = l3
	}
}

// sysctlString returns a sysctl string value, or "" on error.
func sysctlString(key string) string {
	out, err := exec.Command("sysctl", "-n", key).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// sysctlInt returns a sysctl integer value, or 0 on error.
func sysctlInt(key string) int {
	s := sysctlString(key)
	if s == "" {
		return 0
	}
	var n int
	for _, c := range s {
		if c < '0' || c > '9' {
			break
		}
		n = n*10 + int(c-'0')
	}
	return n
}

// sysctlInt64 returns a sysctl int64, or 0 on error.
func sysctlInt64(key string) int64 {
	s := sysctlString(key)
	if s == "" {
		return 0
	}
	var n int64
	for _, c := range s {
		if c < '0' || c > '9' {
			break
		}
		n = n*10 + int64(c-'0')
	}
	return n
}
