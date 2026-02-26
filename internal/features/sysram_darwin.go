package features

import "golang.org/x/sys/unix"

// detectSysRAMGB reads total physical RAM on macOS via sysctl hw.memsize.
func detectSysRAMGB() float64 {
	b, err := unix.SysctlRaw("hw.memsize")
	if err != nil || len(b) < 8 {
		return 0
	}
	// hw.memsize is a uint64 in native byte order.
	var bytes uint64
	for i := 0; i < 8; i++ {
		bytes |= uint64(b[i]) << (8 * i)
	}
	return float64(bytes) / 1e9
}