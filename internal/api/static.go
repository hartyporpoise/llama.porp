package api

import (
	"io/fs"

	webpkg "github.com/hartyporpoise/porpulsion/web"
)

// staticFiles is the sub-filesystem containing the compiled web assets.
// It is served from the /static/ prefix and index.html is served at /.
var staticFiles fs.FS = webpkg.StaticFiles
