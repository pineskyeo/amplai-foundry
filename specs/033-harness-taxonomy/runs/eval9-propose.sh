#!/bin/sh
# Pilot: propose evaluator version eval-9 on the separate meta deployment (human operator act).
cd "$(dirname "$0")/../../.." || exit 1
exec .venv/bin/amplai meta evaluator propose-change \
  --to-file specs/033-harness-taxonomy/runs/eval-9.json \
  --reason "regression-set trials and pre-run failure stop 2026-10-11" \
  --config "$HOME/.amplai/meta/local.json"
