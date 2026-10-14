#!/bin/sh
# Pilot: propose evaluator version eval-12 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-12.json \
  --reason "D-128 calibration overrun record, resume, imports, not-run zero charge, partial summaries 2026-10-14" \
  --config "$HOME/.amplai/meta/local.json"
