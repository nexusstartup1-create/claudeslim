# Corpus

Real files from well-known projects, fetched once and vendored so the corpus
test is deterministic and works offline. They are inputs to a compressor test,
never shipped in the package.

| file | project | licence |
| --- | --- | --- |
| js_node_events.js | nodejs/node `lib/events.js` | MIT |
| js_vue_reactive.js | vuejs/core `packages/reactivity/src/reactive.ts` | MIT |
| js_jquery_core.js | jquery/jquery `src/core.js` | MIT |
| ts_vscode_uri.ts | microsoft/vscode `src/vs/base/common/uri.ts` | MIT |
| ts_axios_core.ts | axios/axios `lib/core/Axios.js` | MIT |
| ts_zod_types.ts | colinhacks/zod `packages/zod/src/v4/classic/schemas.ts` | MIT |
| go_stdlib_errors.go | golang/go `src/errors/errors.go` | BSD-3-Clause |
| go_hugo_page.go | gohugoio/hugo `hugolib/page.go` | Apache-2.0 |
| go_cobra_command.go | spf13/cobra `command.go` | Apache-2.0 |

Refresh with `tests/fetch_corpus.sh`.
