#!/bin/sh
# Build the wheel, install it outside the checkout, and prove the installed copy
# is the whole package: its modules, its migrations and its plugin manifest.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PACKAGE=bootstrap/plugins/review_agent_tools
# The compiled lock that pins every declared dependency.
PINS=$ROOT/requirements/requirements-admin.txt
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
unset PYTHONPATH

python3 -m venv "$WORK/venv"
PYTHON=$WORK/venv/bin/python
install() {
  "$PYTHON" -m pip install --quiet --disable-pip-version-check --constraint "$PINS" "$@"
}

# Build from a copy of the inputs: an in-tree build leaves build/ and an
# egg-info directory in the checkout, and a stale build/ leaks deleted files.
mkdir -p "$WORK/source/bootstrap/plugins"
cp "$ROOT/pyproject.toml" "$ROOT/LICENSE" "$ROOT/NOTICE.md" "$WORK/source/"
cp -R "$ROOT/$PACKAGE" "$WORK/source/bootstrap/plugins/"
"$PYTHON" -m pip wheel --quiet --disable-pip-version-check --no-deps \
  --wheel-dir "$WORK/dist" "$WORK/source"
WHEEL=$(echo "$WORK"/dist/review_agent-*.whl)
printf '%s\n' "Wheel: $(basename "$WHEEL")"

cd "$WORK"
install "$WHEEL"
git -C "$ROOT/$PACKAGE" ls-files >"$WORK/tracked"
"$PYTHON" - "$ROOT/$PACKAGE" "$WORK/tracked" <<'PY'
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from pkgutil import walk_packages
import sys

import review_agent_tools
from review_agent_tools.postgres_migrations import runner

source = Path(sys.argv[1])
tracked = set(Path(sys.argv[2]).read_text(encoding="utf-8").split())
installed = Path(review_agent_tools.__file__).resolve().parent
if not installed.is_relative_to(Path(sys.prefix).resolve()):
    raise SystemExit(f"review_agent_tools resolved outside the install: {installed}")
print(f"Installed copy: {installed}")

shipped = {
    path.relative_to(installed).as_posix()
    for path in installed.rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
}
if shipped != tracked:
    raise SystemExit(f"wheel and tracked package differ: {sorted(shipped ^ tracked)}")
if "plugin.yaml" not in shipped:
    raise SystemExit("plugin.yaml is missing from the wheel")
print(f"Files: {len(shipped)}, the tracked package with plugin.yaml")

packaged = [(item.name, item.checksum) for item in runner.discover_migrations()]
checkout = [
    (item.name, item.checksum)
    for item in runner.discover_migrations(source / "postgres_migrations")
]
if packaged != checkout:
    raise SystemExit("installed migrations differ from the checkout")
print(f"Migrations: {len(packaged)} with the checkout's checksums")

if find_spec("fastapi") is not None or find_spec("aiohttp") is not None:
    raise SystemExit("the core install pulled in an optional group")
# Everything but the console and the Hermes runtime API is core: the worker,
# admission, the publisher, the GitHub gateway, operator commands and the stores.
optional = (
    "review_agent_tools.admin",
    "review_agent_tools.model_runtime.hermes_runtime_api",
)
core = [
    module.name
    for module in walk_packages(review_agent_tools.__path__, "review_agent_tools.")
    if not module.name.startswith(optional)
]
for name in core:
    import_module(name)
print(f"Core: {len(core)} modules import without the optional groups")
PY

install "$WHEEL[admin]"
"$PYTHON" -c "import review_agent_tools.admin.api"
printf '%s\n' "Console API imports with the admin group"
install "$WHEEL[hermes]"
"$PYTHON" -c "import review_agent_tools.model_runtime.hermes_runtime_api"
printf '%s\n' "Hermes runtime API imports with the hermes group"

printf '%s\n' "Package checks passed."
