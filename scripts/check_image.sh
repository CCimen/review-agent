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

docker run --rm --network none \
    --env PYTHONPATH=/opt/review-agent-bootstrap/plugins \
    --entrypoint /opt/review-agent-code-graph/bin/python "$image" -c '
from pathlib import Path
from tempfile import TemporaryDirectory
from review_agent_tools.code_graph_indexer import execute

with TemporaryDirectory() as directory:
    root = Path(directory)
    source = root / "source"
    source.mkdir()
    (source / "sample.py").write_text("def answer():\n    return 42\n")
    (source / "sample.ts").write_text("export function answer(): number { return 42; }\n")
    (source / "sample.c").write_text("int answer(void) { return 42; }\n")
    result = execute({"operation": "build", "source": str(source),
                      "database": str(root / "graph.db"), "embeddings": "disabled"})
    assert result["parse_errors"] == 0, result
    assert result["nodes"] >= 3, result
'

for entrypoint in \
    review-agent-admission \
    review-agent-worker \
    review-agent-publisher
do
    docker run --rm \
        --entrypoint "/usr/local/bin/$entrypoint" \
        "$image" --help >/dev/null
done

docker run --rm \
    --entrypoint /usr/local/bin/review-agent-hermes-contract \
    "$image"

for check in test_hermes_auxiliary.py test_hermes_account_usage.py
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
    --entrypoint sh \
    "$image" -ec '
        /opt/review-agent-bootstrap/install.sh
        cp /opt/data/config.yaml /tmp/config.yaml.before-hermes
        /opt/hermes/bin/hermes config migrate >/dev/null
        cmp -s /tmp/config.yaml.before-hermes /opt/data/config.yaml
    '

docker run --rm --user 12345:0 \
    --tmpfs /opt/data:rw,mode=0770,uid=12345,gid=0 \
    --env HOME=/opt/data \
    --env HERMES_HOME=/opt/data \
    --entrypoint /opt/hermes/bin/hermes \
    "$image" gateway --help >/dev/null
