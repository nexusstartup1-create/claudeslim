#!/usr/bin/env bash
# Refresh tests/fixtures/corpus from upstream. Provenance in SOURCES.md.
set -uo pipefail
cd "$(dirname "$0")/fixtures/corpus"
get() { curl -sfL --max-time 30 "$2" -o "$1" && echo "  ok   $1" || echo "  FAIL $1"; }
get js_node_events.js     https://raw.githubusercontent.com/nodejs/node/main/lib/events.js
get js_vue_reactive.js    https://raw.githubusercontent.com/vuejs/core/main/packages/reactivity/src/reactive.ts
get js_jquery_core.js     https://raw.githubusercontent.com/jquery/jquery/main/src/core.js
get ts_vscode_uri.ts      https://raw.githubusercontent.com/microsoft/vscode/main/src/vs/base/common/uri.ts
get ts_axios_core.ts      https://raw.githubusercontent.com/axios/axios/v1.x/lib/core/Axios.js
get ts_zod_types.ts       https://raw.githubusercontent.com/colinhacks/zod/main/packages/zod/src/v4/classic/schemas.ts
get go_stdlib_errors.go   https://raw.githubusercontent.com/golang/go/master/src/errors/errors.go
get go_hugo_page.go       https://raw.githubusercontent.com/gohugoio/hugo/master/hugolib/page.go
get go_cobra_command.go   https://raw.githubusercontent.com/spf13/cobra/main/command.go
