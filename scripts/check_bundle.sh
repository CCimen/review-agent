#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 -m compileall -q "$ROOT/bootstrap" "$ROOT/tools" "$ROOT/scripts" "$ROOT/tests"
if ! command -v pyright >/dev/null 2>&1; then
  printf '%s\n' "pyright is required for bundle checks. Install pyright and rerun." >&2
  exit 1
fi
pyright -p "$ROOT"
python3 "$ROOT/scripts/check_app_only_candidate.py"
PYTHONPATH="$ROOT/bootstrap/plugins" python3 -m unittest discover -s "$ROOT/tests" -v
replay_fixture=""
for candidate in "$ROOT"/review-learning/replay/*.json; do
    [ -e "$candidate" ] || continue
    replay_fixture=$candidate
    break
done
if [ -n "$replay_fixture" ]; then
    python3 "$ROOT/tools/review_agent_memory.py" validate-replay "$ROOT/review-learning/replay"
else
    printf '%s\n' "Replay validation skipped: no historical fixtures are tracked."
fi

python3 - "$ROOT" <<'PY'
from pathlib import Path
import sys

import yaml

root = Path(sys.argv[1])
for relative in [
    "compose.yaml",
    "bootstrap/config.yaml",
    "bootstrap/plugins/review_agent_tools/plugin.yaml",
    "examples/openshift/review-agent-template.yaml",
]:
    path = root / relative
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"{relative} did not parse to a YAML mapping")
    print(f"YAML OK: {relative}")
PY

printf '%s\n' "Bundle checks passed."
