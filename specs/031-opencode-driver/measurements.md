# 031 Measurements (M1 auth, M2 SSE)

Date: 2026-09-29. Harness: `scripts/opencode_auth_probe.py` (reuses `ContainerServer` of
`scripts/opencode_qualify.py`). Image: `localhost:5000/amplai-worker-opencode@sha256:773c1b7a…`
(opencode 1.17.13), qualified egress profile via `ContainerSandbox` (`--read-only`, `--cap-drop=ALL`,
`no-new-privileges`, uid 65534, `amplai-internal` network, `amplai-egress-proxy`). Scoped credential copy
`~/.amplai-sandbox-probes/opencode-home` mounted as `/home/agent` (read by the server, not copied,
not printed). Model `opencode-go/glm-5.3-flash`. **Model turns used: 1** (two earlier runs stopped before
any turn, see Setup Findings). All secret values redacted; the password was a per-run random value and a
write-time scan found 0 occurrences of it in the recorded output (`values_redacted_on_write: 0`).

Setup as measured: `OPENCODE_SERVER_PASSWORD` and `OPENCODE_SERVER_USERNAME` passed by name
(`docker run --env NAME`), B = plugin `shell.env` hook from a read-only config dir
(`OPENCODE_CONFIG_DIR=/amplai-input/oc-config`) that sets both variables to `""` and sets a marker
`AMPLAI_PLUGIN_LOADED=1`, C = `SHELL=/amplai-input/oc-config/shell` (`unset OPENCODE_SERVER_PASSWORD
OPENCODE_SERVER_USERNAME; exec /bin/bash "$@"`).

## M1 (c) Authentication

| request | status |
| --- | --- |
| `GET /global/health` with password | 200 |
| `GET /global/health` without credentials | 401 |
| `GET /session` without credentials | 401 |
| `GET /event` without credentials | 401 |
| `GET /global/health` with a wrong password | 401 |
| listeners on port 4096 (`/proc/net/tcp`) | `0100007F` only (127.0.0.1) |

## M1 (a) What the agent's tools can see

One real turn; the model ran `sh /workspace/probe.sh` with the bash tool (state `completed`, 4.85 s). The
script printed only names and 0/1 counts, never a value.

| observation | result |
| --- | --- |
| plugin loaded | yes: marker `AMPLAI_PLUGIN_LOADED=1` is in the bash tool env |
| `SHELL` in the tool | `/amplai-input/oc-config/shell` (the wrapper) |
| `OPENCODE_SERVER_*` in the bash tool env | 0 lines; the variable **names are gone** (the plugin alone would leave them present but empty: it can only set a value, not remove a name, because the tool env is `{...process.env, ...hookEnv}`) |
| tool process parent chain | `sh probe.sh` (pid 163) → `opencode serve` (pid 6) |
| `/proc/6/environ` (the server) read by the tool | **readable; `OPENCODE_SERVER_PASSWORD=` present and non-empty** |
| password value in the recorded messages | absent (the script never printed it) |

Independent of the model (`docker exec` as uid 65534, the agent's uid), before the turn:

- `/proc/1/environ` (docker-init) and `/proc/6/environ` (server): both readable, both contain the non-empty
  variable. The value read from pid 6 equals the real password (compared in memory, not printed).
- Server environ variable names: `HOME HOSTNAME HTTPS_PROXY HTTP_PROXY NODE_VERSION NO_PROXY
  OPENCODE_CONFIG_DIR OPENCODE_SERVER_PASSWORD OPENCODE_SERVER_USERNAME PATH SHELL YARN_VERSION`.

**Result: with B+C the tool's own environment is clean, but the agent can read the password from
`/proc/<server pid>/environ` because it runs as the same uid in the same container.**

## M1 (b) Formatter and git children

- Can measure: `/proc/<pid>/environ` of any same-uid process is readable, so what a child inherits does
  not matter for the agent; the server's own environ already holds the value.
- Cannot measure: the env a formatter/git child actually receives. No formatter is on `PATH` in the image
  (`prettier black ruff gofmt rustfmt clang-format`: none). `/usr/bin/git` exists. Reading the binary
  (design.md Auth Options) shows those children inherit the server env without the `shell.env` hook; that
  is a reading of the code, not an observation of a process. Not needed for the conclusion above.

## M2 SSE (one connection during the turn, one after it)

| fact | observation |
| --- | --- |
| response | `200`, `Content-Type: text/event-stream`, chunked, `Cache-Control: no-cache, no-transform` |
| `id:` lines in the SSE frames | 0 (turn connection) and 0 (reconnect) |
| event `id` | present in the JSON `data` (`evt_…`), 143 events in the turn, all unique |
| first event on every connection | `server.connected` at t=0.03 s / 0.04 s |
| heartbeat | `server.heartbeat` at 10.05 s, 20.06 s, 30.07 s on an idle connection: every 10 s |
| reconnect after the turn | only `server.connected` then heartbeats; **no replay of the finished turn's events** |
| `message.updated` properties | `info` and `sessionID` (both at the top level of `properties`) |
| `session.status` / `session.idle` properties | `sessionID`, `status` / `sessionID` |

Order at the end of the turn (t in seconds, all within 0.01 s): `message.part.updated` ×2,
`message.updated` ×2, `session.status` ×2, `session.idle`, `session.updated`, `session.diff`,
`message.updated`. A `message.updated` arrives **after** `session.idle`. Other types seen:
`session.created`, `plugin.added` (many, at the first prompt while the instance boots),
`catalog.updated`, `integration.updated`, `reference.updated`, `message.part.delta`.

Consequence for `OpenCodeEvents`: no replay and no SSE cursor confirmed, so reconcile-on-`server.connected`
is required; completion by the correlated `message.updated` (completed) before `session.idle` is sufficient
and the late `message.updated` after idle is ignored (first completion kept).

## Setup Findings (they change the design)

1. A **read-only** config dir makes the server return HTTP 500 on `POST /session`: on first load it writes
   `<config dir>/.gitignore` if that file is missing (`EROFS: read-only file system`, server log). Pre-creating
   `.gitignore` in the dir fixes it (code: `Config.ensureGitignore` writes only when absent).
2. The server also tries a background `npm install` of `@opencode-ai/plugin` into the config dir; under the
   egress profile it fails with `403 Forbidden` as a WARN and does not stop the plugin from loading.
3. `plugin.added` events flood the stream at boot (about 40 in the first second of the first prompt).

## Not Measured

- Whether the agent can read the provider login `auth.json` in its own HOME (`/home/agent`, the scoped
  copy is bind-mounted there, `sandbox/container.py:137`). Expected yes because it is the agent's HOME
  (추정); one `cat`/`test -r` in a future turn settles it.
- A control run without B+C (the earlier qualifier already measured the `env` leak,
  `specs/017-external-qualification-closure/spec.md:44-45`).
- Resume after a server restart (M4), `docker exec curl -N` streaming (M3), app image (M5), startup cost (M6).
