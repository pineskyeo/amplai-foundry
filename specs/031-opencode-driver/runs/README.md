# Work 031 Runs (2026-09-30)

All in the app image with OpenCode installed (031 D3):
`localhost:5000/amplai-worker-app-amplai-foundry@sha256:2ab06021…`, profile
`deployment/local-container-app-amplai-foundry-opencode.json`. The profile the running server uses
(`deployment/local-container-app-amplai-foundry.json`, the earlier image) is unchanged: switching the
server to the new image needs its Codex and Claude reports for that image too.

| Record | Result |
|---|---|
| `../driver-qualification-amplai-foundry.json` (`scripts/opencode_qualify.py --container`) | OpenCode 1.17.13, `opencode-go/glm-5.3-flash`: all nine checks pass and `tool_use` passes (a shell command and file write, and a 48 s foreground command). The server runs with a per-run password and the env guard (a `shell.env` plugin plus a `SHELL` wrapper): the bash tool ran and saw no `OPENCODE_SERVER_*` name. Unauthenticated health is 401. Evidence in `../artifacts/` |
| `../driver-qualification-codex-amplai-foundry.json` (`scripts/container_qualify.py`) | Codex 0.155.1 requalified in the same image: nine checks pass, `tool_use` passes |
| `trial-1.json` (`scripts/meta_smoke.py --driver opencode-server`) | One corpus task (`s01-semver-parse`) as a real goal on the OpenCode composition, through the product's per-dispatch launcher: `verified`, hidden tests passed, 46 s. Usage is `unknown`: the OpenCode driver does not report token counts yet (design R5) |

An earlier qualification run on this image ended `fail` because the model never used the bash tool
(`secret_isolation`: bash tool ran=False; `filesystem_containment` inconclusive). The next runs with
the same configuration used it and passed. The server password stays readable through
`/proc/<server pid>/environ` for the same uid; D-091 records that risk.
