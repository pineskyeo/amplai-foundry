#!/bin/sh
# Pilot calibration of the four cells on the separate meta deployment (human operator act).
# Runs in the background; the log is ~/.amplai/meta/logs/calibration-7.log.
cd "$(dirname "$0")/../../.." || exit 1
R=specs/033-harness-taxonomy/runs
mkdir -p "$HOME/.amplai/meta/logs"
nohup .venv/bin/amplai meta calibrate --config "$HOME/.amplai/meta/local.json" \
  --cells codex-cli.gpt-5.6-sol.medium,codex-cli.gpt-5.6-sol.high,claude-cli.claude-sonnet-5.high,claude-cli.claude-opus-5-5.high \
  --max-repeats 5 --max-trials 980 --max-tokens 650000000 --max-wall-seconds 172800 \
  --per-trial-tokens 4000000 \
  --basis "bench app driver qualifications 2026-10-08 with offline argv; eval-6 (cost not compared is no overrun); per-trial ceiling 4M" \
  --evidence "$R/qualification-bench-codex.json" \
  --evidence "$R/qualification-bench-claude-sonnet-5.json" \
  --evidence "$R/qualification-bench-claude-opus-5-5.json" \
  --parallel 2 > "$HOME/.amplai/meta/logs/calibration-7.log" 2>&1 &
echo "calibration started (pid $!); log: $HOME/.amplai/meta/logs/calibration-7.log"
