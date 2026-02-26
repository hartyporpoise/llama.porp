// Linux specific CPU topology detection.

//go:build linux

package cpu

import (
	"os"
	"strconv"
	"strings"
)

// detectPlatformTopology is the Linux implementation.
// On a container the cgroup CPU quota takes priority over /proc/cpuinfo so
// that we report the number of cores the container actually has access to,
// not the host machine's full core count.
func detectPlatformTopology(t *Topology) {
	parseLinuxCPUInfo(t)
	detectLinuxNUMA(t)

	// Override logical core count with cgroup quota if present.
	// This gives the correct value when running inside a Docker container
	// with --cpus set.
	if n := cgroupCPULimit(); n > 0 && n < t.LogicalCores {
		t.LogicalCores  = n
		t.PhysicalCores = n
		t.PCores        = n
		t.ECores        = 0
	}
}

// cgroupCPULimit returns the effective CPU count from the cgroup CPU quota,
// or 0 if no limit is set or if the limit cannot be determined.
// Supports both cgroup v1 (/sys/fs/cgroup/cpu/) and cgroup v2 (/sys/fs/cgroup/).
func cgroupCPULimit() int {
	// --- cgroup v2 ---
	// /sys/fs/cgroup/cpu.max: "<quota> <period>" or "max <period>"
	if data, err := os.ReadFile("/sys/fs/cgroup/cpu.max"); err == nil {
		fields := strings.Fields(strings.TrimSpace(string(data)))
		if len(fields) >= 2 && fields[0] != "max" {
			quota, e1  := strconv.ParseFloat(fields[0], 64)
			period, e2 := strconv.ParseFloat(fields[1], 64)
			if e1 == nil && e2 == nil && period > 0 && quota > 0 {
				n := int(quota / period)
				if n < 1 {
					n = 1
				}
				return n
			}
		}
	}

	// --- cgroup v1 ---
	quotaData,  e1 := os.ReadFile("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
	periodData, e2 := os.ReadFile("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
	if e1 == nil && e2 == nil {
		quota,  pe1 := strconv.ParseFloat(strings.TrimSpace(string(quotaData)), 64)
		period, pe2 := strconv.ParseFloat(strings.TrimSpace(string(periodData)), 64)
		if pe1 == nil && pe2 == nil && quota > 0 && period > 0 {
			n := int(quota / period)
			if n < 1 {
				n = 1
			}
			return n
		}
	}

	return 0 // no limit
}
