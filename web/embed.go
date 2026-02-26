// Package web embeds the compiled static web assets into the binary.
// Placing the embed here (next to the static/ directory) avoids the
// Go toolchain restriction that //go:embed paths cannot use "..".
package web

import (
	"embed"
	"io/fs"
)

//go:embed static
var embedded embed.FS

// StaticFiles is an fs.FS rooted at web/static/.
// Consumers can serve it directly with http.FileServer(http.FS(StaticFiles)).
var StaticFiles, _ = fs.Sub(embedded, "static")
