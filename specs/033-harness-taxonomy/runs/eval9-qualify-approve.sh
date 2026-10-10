#!/bin/sh
# Pilot: qualify (runs the Q-suite) and then approve an evaluator change (human operator acts).
# Usage: eval9-qualify-approve.sh <evalchange-id printed by eval9-propose.sh>
cd "$(dirname "$0")/../../.." || exit 1
CHANGE="${1:?usage: $0 <evalchange-id>}"
CONFIG="$HOME/.amplai/meta/local.json"
.venv/bin/amplai meta evaluator qualify-change "$CHANGE" --config "$CONFIG" | grep -E '"state"|"passed"|"failed"' | head -5 || exit 1
exec .venv/bin/amplai meta evaluator approve-change "$CHANGE" --config "$CONFIG"
