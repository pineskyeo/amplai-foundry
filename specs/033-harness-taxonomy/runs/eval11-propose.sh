#!/bin/sh
# Pilot: propose evaluator version eval-11 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-11.json \
  --reason "drift calibration on the drift_tasks subset (operator decision 3) 2026-10-12" \
  --config "$HOME/.amplai/meta/local.json"
