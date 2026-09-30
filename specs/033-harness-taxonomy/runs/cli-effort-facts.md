# CLI Effort And Option Facts (worker image, 2026-10-01)

Image `localhost:5000/amplai-worker-app-amplai-foundry@sha256:2ab060217a109af7ca70ff724ee878881d4c76e1465f045fe41e9b242b89e0f7`,
run with `--network none`, `HOME=/tmp`. Quotes are verbatim help output.

## Codex CLI 0.155.1 (interfaces §14 Q1)

`codex exec --help`:

```text
  -c, --config <key=value>
          Override a configuration value that would otherwise be loaded from `~/.codex/config.toml`.
```

`codex exec resume --help` (the same option exists on resume):

```text
Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]
  -c, --config <key=value>
          Override a configuration value that would otherwise be loaded from `~/.codex/config.toml`.
          Use a dotted path (`foo.bar.baz`) to override nested values. The `value` portion is parsed
          as TOML. If it fails to parse as TOML, the raw string is used as a literal.
```

Config key (official reference, https://learn.chatgpt.com/docs/config-file/config-reference):
`model_reasoning_effort` — "low", "medium", "high", "xhigh", "max", or "ultra" depending on model support.
Which values gpt-5.6-sol accepts: not measured (probe per value, §14 Q1/Q2).

## Claude Code 2.1.278 (interfaces §14 Q15)

`claude --help`:

```text
  --effort <level>                      Effort level for the current session
  --allowedTools, --allowed-tools <tools...>
  --append-system-prompt <prompt>       Append a system prompt to the default
  --tools <tools...>                    Specify the list of available tools from
```

Values (official CLI reference, https://code.claude.com/docs/en/cli-reference, `--effort` row):
"Options: `low`, `medium`, `high`, `xhigh`, `max`, or `ultracode`. Available levels depend on the model."
`ultracode` is not a reasoning level for a driver cell (it turns on workflow orchestration) and is excluded.
Which values claude-sonnet-5 and claude-opus-5-5 accept: not measured (probe per value).
`--max-turns` does not appear in `claude --help` of this version; the docs say `--help` does not list every flag
(https://code.claude.com/docs/en/cli-reference); acceptance is 확인 필요 by probe (§14 Q13).

## OpenCode 1.17.13 (interfaces §14 Q3)

`opencode run --help`:

```text
      --attach       attach to a running opencode server (e.g., http://localhost:4096)      [string]
      --variant      model variant (provider-specific reasoning effort, e.g., high, max, minimal)
                                                                                            [string]
```

`run --attach` talks to a running `opencode serve`, so the server most likely accepts a per-message variant (추정);
the HTTP field name is 확인 필요 (read the server API or capture `run --attach --variant` traffic).
