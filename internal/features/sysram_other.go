//go:build !darwin

package features

// detectSysRAMGB is a no-op on non-Darwin platforms.
// Linux uses /proc/meminfo instead (readProcMeminfo in quant_advisor.go).
func detectSysRAMGB() float64 {
	return 0
}