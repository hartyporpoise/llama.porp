// Fallback feature detection for unsupported architectures.
// All flags remain false — porpulsion will still start, just without SIMD hints.

//go:build !amd64 && !arm64

package cpu

func detectFeatures(t *Topology) {
	// No SIMD features detected; llama.cpp will use its scalar fallback path.
}
