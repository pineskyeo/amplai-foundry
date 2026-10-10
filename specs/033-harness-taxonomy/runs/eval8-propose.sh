#!/bin/sh
# Pilot: propose evaluator version eval-8 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-8.json \
  --reason "reconcile fix, provider failure stop, rate-limit fields, calibration recovery (PRs #66-#68) 2026-10-10" \
  --config "$HOME/.amplai/meta/local.json"
