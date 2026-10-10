#!/bin/sh
# Pilot: propose evaluator version eval-9 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-9.json \
  --reason "provider failure stop and rate-limit fields (§14 Q5) 2026-10-10" \
  --config "$HOME/.amplai/meta/local.json"
