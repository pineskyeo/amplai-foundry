#!/bin/sh
# Pilot: propose evaluator version eval-6 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-6.json \
  --reason "a reported cost is no overrun when cost is not compared (D-088) 2026-10-09" \
  --config "$HOME/.amplai/meta/local.json"
