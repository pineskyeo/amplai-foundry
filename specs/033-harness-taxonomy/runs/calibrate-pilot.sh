#!/bin/sh
# Pilot calibration of the four cells on the separate meta deployment (human operator act).
# Runs detached in its own session (perl POSIX::setsid; macOS has no setsid command), so the end
# of the shell or agent session that started it does not stop it (calplan-0034e3f6 stopped that way).
# The log is ~/.amplai/meta/logs/calibration-9.log.
cd "$(dirname "$0")/../../.." || exit 1
R=specs/033-harness-taxonomy/runs
LOG="$HOME/.amplai/meta/logs/calibration-9.log"
mkdir -p "$HOME/.amplai/meta/logs"
nohup perl -MPOSIX=setsid -e 'setsid(); exec @ARGV or die "exec: $!"' -- \
  .venv/bin/amplai meta calibrate --config "$HOME/.amplai/meta/local.json" \
  --cells codex-cli.gpt-5.6-sol.medium,codex-cli.gpt-5.6-sol.high,claude-cli.claude-sonnet-5.high,claude-cli.claude-opus-5-5.high \
  --max-repeats 3 --max-trials 980 --max-tokens 650000000 --max-wall-seconds 172800 \
  --per-trial-tokens 4000000 \
  --basis "bench app driver qualifications 2026-10-08 with offline argv; eval-8 (PRs #66-#68); max repeats 3; per-trial ceiling 4M" \
  --evidence "$R/qualification-bench-codex.json" \
  --evidence "$R/qualification-bench-claude-sonnet-5.json" \
  --evidence "$R/qualification-bench-claude-opus-5-5.json" \
  --parallel 2 > "$LOG" 2>&1 < /dev/null &
echo "calibration started (pid $!); log: $LOG"
