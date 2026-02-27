// Package features — smart_context.go compresses conversation history before
// sending it to Ollama. Long conversations have a huge prefill cost; by keeping
// only the most important messages we dramatically reduce time-to-first-token.
package features

import (
	"fmt"

	"github.com/hartyporpoise/porpulsion/internal/ollama"
)

// CompressHistory trims a message slice to reduce prefill tokens.
//
// Strategy:
//   - Keep all system messages (they define behaviour)
//   - Keep the first non-system user message (establishes topic)
//   - Keep the last 4 non-system messages (2 exchanges — immediate context)
//   - Drop everything in between, inserting a synthetic system note
//
// If the conversation has 6 or fewer non-system messages, it is returned
// unmodified — compression would remove nothing useful.
func CompressHistory(msgs []ollama.Message) []ollama.Message {
	// Separate system messages from conversation messages.
	var system []ollama.Message
	var conv []ollama.Message
	for _, m := range msgs {
		if m.Role == "system" {
			system = append(system, m)
		} else {
			conv = append(conv, m)
		}
	}

	// Not enough messages to justify compression.
	if len(conv) <= 6 {
		return msgs
	}

	// Keep the first user message and the last 4 messages.
	// Everything in between is dropped.
	const tailKeep = 4
	dropped := len(conv) - 1 - tailKeep // first message + tail are kept
	if dropped <= 0 {
		return msgs
	}

	compressed := make([]ollama.Message, 0, len(system)+1+1+tailKeep)

	// 1. System messages
	compressed = append(compressed, system...)

	// 2. First conversation message (topic establishment)
	compressed = append(compressed, conv[0])

	// 3. Synthetic note about omitted messages
	compressed = append(compressed, ollama.Message{
		Role:    "system",
		Content: fmt.Sprintf("[%d earlier messages omitted for performance]", dropped),
	})

	// 4. Last 4 messages (immediate context)
	compressed = append(compressed, conv[len(conv)-tailKeep:]...)

	return compressed
}
