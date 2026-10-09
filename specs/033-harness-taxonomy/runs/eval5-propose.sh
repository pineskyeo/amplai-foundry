#!/bin/sh
# Pilot: propose evaluator version eval-5 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-5.json \
  --reason "calibration safety continues (A) and clean-room grading 2026-10-09" \
  --config "$HOME/.amplai/meta/local.json"
