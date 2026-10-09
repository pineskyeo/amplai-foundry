#!/bin/sh
# Pilot: qualify (runs the Q-suite) and then approve evaluator version eval-4 (human operator acts).
cd "$(dirname "$0")/../../.." || exit 1
CHANGE=evalchange-d4573d5a614e42e384b08b3cc313133d
CONFIG="$HOME/.amplai/meta/local.json"
.venv/bin/amplai meta evaluator qualify-change "$CHANGE" --config "$CONFIG" | grep -E '"state"|"passed"|"failed"' | head -5 || exit 1
exec .venv/bin/amplai meta evaluator approve-change "$CHANGE" --config "$CONFIG"
