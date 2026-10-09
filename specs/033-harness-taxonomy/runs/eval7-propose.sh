#!/bin/sh
# Pilot: propose evaluator version eval-7 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-7.json \
  --reason "a reported cost is not spent when cost is not compared (D-088 ledger) 2026-10-09" \
  --config "$HOME/.amplai/meta/local.json"
