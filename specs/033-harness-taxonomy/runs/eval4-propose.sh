#!/bin/sh
# Pilot: propose evaluator version eval-4 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-4.json \
  --reason "pilot fixes 2026-10-09" \
  --config "$HOME/.amplai/meta/local.json"
