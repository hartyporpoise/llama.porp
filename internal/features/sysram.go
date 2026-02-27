package features

import (
	"os"
	"runtime"
	"strconv"
	"strings"
)

// AvailableRAMGB returns the RAM available to the current process in gigabytes.
//
// Detection priority:
//  1. cgroup v2 memory.max (containerised Linux)
//  2. cgroup v1 memory.limit_in_bytes (older Docker / K8s)
//  3. /proc/meminfo MemTotal (bare-metal Linux)
//  4. Platform sysctl (macOS hw.memsize)
//  5. Go runtime Sys bytes (last resort)
func AvailableRAMGB() float64 {
	// cgroup v2
	if b, err := os.ReadFile("/sys/fs/cgroup/memory.max"); err == nil {
		s := strings.TrimSpace(string(b))
		if s != "max" {
			if v, err := strconv.ParseInt(s, 10, 64); err == nil && v > 0 {
				return float64(v) / 1e9
			}
		}
	}
	// cgroup v1
	if b, err := os.ReadFile("/sys/fs/cgroup/memory/memory.limit_in_bytes"); err == nil {
		if v, err := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64); err == nil && v > 0 && v < 1e15 {
			return float64(v) / 1e9
		}
	}
	// /proc/meminfo
	if b, err := os.ReadFile("/proc/meminfo"); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "MemTotal:") {
				fields := strings.Fields(line)
				if len(fields) >= 2 {
					if kb, err := strconv.ParseInt(fields[1], 10, 64); err == nil {
						return float64(kb) / 1e6
					}
				}
			}
		}
	}
	// Platform-specific (macOS sysctl)
	if gb := detectSysRAMGB(); gb > 0 {
		return gb
	}
	// Go runtime fallback
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	if m.Sys > 0 {
		return float64(m.Sys) / 1e9
	}
	return 8.0 // safe default
}
