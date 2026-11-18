#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 IMAGE" >&2
    exit 2
fi

image=$1
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

docker run --rm --entrypoint sh "$image" -c \
    'command -v curl >/dev/null && command -v uv >/dev/null && \
     ! command -v uvx >/dev/null && ! command -v gh >/dev/null && \
     ! command -v npm >/dev/null && ! command -v npx >/dev/null && \
     test ! -d /opt/hermes/node_modules'

docker run --rm --entrypoint /opt/hermes/.venv/bin/python "$image" -c '
from importlib.metadata import version
from pathlib import Path
from packaging.requirements import Requirement
import subprocess

for package in ("linux-libc-dev", "openssh-client", "libxml2"):
    status = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Status}", package],
        capture_output=True, text=True,
    )
    assert status.returncode in (0, 1), status.stderr
    assert status.stdout.strip() in ("", "not-installed"), package

for line in Path("/opt/review-agent-requirements.txt").read_text().splitlines():
    if line and not line.startswith("#"):
        requirement = Requirement(line)
        installed = version(requirement.name)
        assert installed in requirement.specifier, f"{requirement}: installed {installed}"
'

# The graph service starts the indexer as a child in the graph environment. Go
# through the service's own launch, so its interpreter, restricted environment
# and argv are the ones under test, with the constraints of compose.code-graph.yaml.
docker run --rm --network none --user 10000:10000 --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --tmpfs /var/lib/review-agent-code-graph:rw,mode=0700,uid=10000,gid=10000 \
    --entrypoint /opt/hermes/.venv/bin/python "$image" -c '
from pathlib import Path
from review_agent.cli.code_graph import GRAPH_PYTHON, GRAPH_RUNNER
from review_agent.code_graph.contract import GraphIdentity, GraphSubject
from review_agent.code_graph.service import CodeGraphService

service = CodeGraphService(
    root=Path("/var/lib/review-agent-code-graph"),
    gateway_url="http://review-github-gateway:8646",
    python=GRAPH_PYTHON, runner=GRAPH_RUNNER,
)
(service.source / "sample.py").write_text("def answer():\n    return 42\n")
(service.source / "sample.ts").write_text("export function answer(): number { return 42; }\n")
(service.source / "sample.c").write_text("int answer(void) { return 42; }\n")
stage = service.work / "index"
stage.mkdir()
result = service._execute(
    GraphIdentity(1, 2, 3),
    GraphSubject(1, "example/repository", "a" * 40, True, "none"),
    {"operation": "build", "changed_files": None, "database": str(stage / "graph.db")},
    timeout=120,
)
assert result["parse_errors"] == 0, result
assert result["nodes"] >= 3, result
'

for entrypoint in \
    review-agent-admin \
    review-agent-admission \
    review-agent-code-graph \
    review-agent-github-app-worker \
    review-agent-github-gateway \
    review-agent-memory \
    review-agent-publisher \
    review-agent-worker
do
    docker run --rm \
        --entrypoint "/usr/local/bin/$entrypoint" \
        "$image" --help >/dev/null
done

docker run --rm \
    --entrypoint /usr/local/bin/review-agent-hermes-contract \
    "$image"

for check in test_hermes_auxiliary.py test_hermes_account_usage.py test_hermes_model_window.py
do
    docker run --rm --network none \
        --mount "type=bind,source=$ROOT/tests/$check,target=/tmp/$check,readonly" \
        --entrypoint /opt/hermes/.venv/bin/python \
        "$image" "/tmp/$check" -q
done

docker run --rm --user 12345:0 \
    --tmpfs /opt/data:rw,mode=0770,uid=12345,gid=0 \
    --env HOME=/opt/data \
    --env HERMES_HOME=/opt/data \
    --env REVIEW_AGENT_MODEL=image-check-model \
    --entrypoint sh \
    "$image" -ec '
        /opt/review-agent-bootstrap/install.sh
        cp /opt/data/config.yaml /tmp/config.yaml.before-hermes
        /opt/hermes/bin/hermes config migrate >/dev/null
        cmp -s /tmp/config.yaml.before-hermes /opt/data/config.yaml
        # The installer copies the plugin from the installed package. Admission
        # derives the contract from that package and the worker from the copy:
        # they must agree, and a changed byte in the copy must be refused.
        /opt/hermes/.venv/bin/python - <<PY
import os
from pathlib import Path

from review_agent.review import contract

assert contract.source_checkout() is None
installed = contract.load_installed_contract()
packaged = contract.load_packaged_contract(
    installed.profile, environment=contract.deployment_environment(os.environ)
)
assert packaged == installed, (packaged, installed)
plugin_file = Path("/opt/data") / contract.PLUGIN_DIRECTORY / "capacity.py"
plugin_file.write_bytes(plugin_file.read_bytes() + b"#")
try:
    contract.load_installed_contract()
except contract.ReviewContractError:
    pass
else:
    raise AssertionError("a changed plugin file passed the receipt")
PY
    '

docker run --rm --user 12345:0 \
    --tmpfs /opt/data:rw,mode=0770,uid=12345,gid=0 \
    --env HOME=/opt/data \
    --env HERMES_HOME=/opt/data \
    --entrypoint /opt/hermes/bin/hermes \
    "$image" gateway --help >/dev/null

# Hermes must discover the installed plugin and register its tools: installing
# files proves nothing about loading. The second install is an upgrade over an
# existing profile home.
docker run --rm --user 12345:0 \
    --tmpfs /opt/data:rw,mode=0770,uid=12345,gid=0 \
    --env HOME=/opt/data \
    --env HERMES_HOME=/opt/data \
    --env REVIEW_AGENT_MODEL=image-check-model \
    --workdir /opt/hermes \
    --entrypoint sh \
    "$image" -ec '
        /opt/review-agent-bootstrap/install.sh >/dev/null
        /opt/review-agent-bootstrap/install.sh >/dev/null
        /opt/hermes/.venv/bin/python - <<PY
from importlib.metadata import entry_points
from pathlib import Path
import sys

import yaml
from hermes_cli.plugins import discover_plugins, get_plugin_manager

home = Path("/opt/data")
discover_plugins(force=True)
manager = get_plugin_manager()
rows = [row for row in manager.list_plugins() if row["name"] == "review-agent-tools"]
assert len(rows) == 1, rows
assert rows[0]["enabled"] and rows[0]["error"] is None, rows[0]

loaded = manager._plugins[rows[0]["key"]]
plugin = Path(loaded.module.__file__).parent
assert plugin.is_relative_to(home / "plugins"), plugin
declared = yaml.safe_load((plugin / "plugin.yaml").read_text())["provides_tools"]
assert sorted(loaded.tools_registered) == sorted(declared), loaded.tools_registered

factories = manager.get_platform_handler_factories("api_server")
assert [owner for _, owner in factories] == ["review-agent-tools"], factories

# Hermes loads one copy of the package, the plugin directory, inside the plugin
# namespace. The installed package is importable here but must stay unloaded and
# undiscovered, and the name "tools" still means the Hermes package.
package = loaded.module.__name__
strays = sorted(
    name for name, module in sys.modules.items()
    if getattr(module, "__file__", None)
    and Path(module.__file__).is_relative_to(plugin)
    and name != package and not name.startswith(package + ".")
)
assert not strays, strays
installed = sorted(
    name for name in sys.modules
    if name == "review_agent" or name.startswith("review_agent.")
)
assert not installed, installed
assert not [
    entry for entry in entry_points(group="hermes_agent.plugins")
    if entry.module.partition(".")[0] == "review_agent"
]
import tools
assert Path(tools.__file__).is_relative_to("/opt/hermes"), tools.__file__
PY
    '
