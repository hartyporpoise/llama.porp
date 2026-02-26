// Generic CPU topology detection fallback for unsupported platforms.

//go:build !darwin && !linux

package cpu

// detectPlatformTopology is a no-op on unsupported platforms.
// The Topology will have sensible defaults from runtime.NumCPU().
func detectPlatformTopology(t *Topology) {}
