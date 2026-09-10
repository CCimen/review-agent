#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
    printf '%s\n' "usage: generate_python_runtime_sbom.sh <image> <runtime-python> <output.cyclonedx.json>" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
image=$1
runtime_python=$2
output_file=$3
case "$output_file" in
    /*.cyclonedx.json) ;;
    *)
        printf '%s\n' "output must be an absolute CycloneDX JSON path" >&2
        exit 2
        ;;
esac

: "${CYCLONEDX_SPEC_VERSION:?CYCLONEDX_SPEC_VERSION is required}"

case "$runtime_python" in
    /opt/hermes/.venv/bin/python|/opt/admin-venv/bin/python) ;;
    *) echo "Unsupported release runtime Python" >&2; exit 2 ;;
esac

output_dir=$(CDPATH= cd -- "$(dirname -- "$output_file")" && pwd)
bootstrap_dir=$(mktemp -d)
trap 'rm -r -- "$bootstrap_dir"' EXIT
# pip is pure Python, so this read-only bootstrap works with either runtime.
python3 -m pip install --quiet --no-cache-dir --disable-pip-version-check \
    --no-compile --no-deps --only-binary=:all: --require-hashes \
    --target "$bootstrap_dir/pip" \
    --requirement "$root/requirements-release-sbom-bootstrap.txt"

docker run --rm -i \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/review-agent-cyclonedx-home \
    -e CYCLONEDX_SPEC_VERSION \
    -v "$output_dir:/out" \
    -v "$bootstrap_dir/pip:/cdx/pip:ro" \
    -v "$root/requirements-release-sbom.txt:/cdx/requirements-release-sbom.txt:ro" \
    --entrypoint /bin/sh \
    "$image" -s -- "/out/$(basename -- "$output_file")" "$runtime_python" <<'CONTAINER'
set -eu
output_file=$1
runtime_python=$2
runtime_distributions=/tmp/review-agent-runtime-distributions.json
tool_venv=/tmp/review-agent-cyclonedx-tool
tool_requirements=/cdx/requirements-release-sbom.txt

if [ ! -x "$runtime_python" ]; then
    printf 'Runtime Python not found at %s\n' "$runtime_python" >&2
    exit 1
fi
if [ ! -f "$tool_requirements" ]; then
    printf 'Release SBOM tool lock not found at %s\n' "$tool_requirements" >&2
    exit 1
fi

mkdir -p "$HOME"

"$runtime_python" - <<'PY' > "$runtime_distributions"
import importlib.metadata as metadata
import json
import sysconfig

paths = sorted({sysconfig.get_paths()[key] for key in ("purelib", "platlib")})
rows = []
seen = set()

for distribution in metadata.distributions(path=paths):
    name = distribution.metadata.get("Name") or distribution.name
    version = distribution.version
    identity = (name, version)
    if identity in seen:
        continue
    seen.add(identity)
    rows.append({"name": name, "version": version})

print(json.dumps(sorted(rows, key=lambda row: row["name"].lower())))
PY

"$runtime_python" -m venv --without-pip "$tool_venv"
PYTHONPATH=/cdx/pip "$tool_venv/bin/python" -m pip install \
    --quiet \
    --no-cache-dir \
    --disable-pip-version-check \
    --require-hashes \
    --requirement "$tool_requirements"

"$tool_venv/bin/cyclonedx-py" environment "$runtime_python" \
    --spec-version "$CYCLONEDX_SPEC_VERSION" \
    --output-format JSON \
    --output-file "$output_file" \
    --output-reproducible \
    --validate

"$runtime_python" - "$runtime_distributions" "$output_file" "$CYCLONEDX_SPEC_VERSION" <<'PY'
import json
import re
import sys

runtime_path, sbom_path, expected_spec = sys.argv[1:]


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


with open(runtime_path, encoding="utf-8") as handle:
    runtime = json.load(handle)
with open(sbom_path, encoding="utf-8") as handle:
    bom = json.load(handle)

if bom.get("bomFormat") != "CycloneDX":
    raise SystemExit("Generated Python runtime SBOM is not CycloneDX JSON")
if bom.get("specVersion") != expected_spec:
    raise SystemExit(
        f"Expected CycloneDX specVersion {expected_spec}, got {bom.get('specVersion')}"
    )

components = bom.get("components") or []
if not components:
    raise SystemExit("Generated Python runtime SBOM has no components")

component_pairs = {
    (normalize(component.get("name", "")), str(component.get("version", "")))
    for component in components
    if component.get("name") and component.get("version")
}
missing = [
    f"{distribution['name']}=={distribution['version']}"
    for distribution in runtime
    if (
        normalize(distribution["name"]),
        str(distribution["version"]),
    )
    not in component_pairs
]
if missing:
    raise SystemExit(
        "Generated Python runtime SBOM is missing installed distributions: "
        + ", ".join(missing[:20])
    )

print(
    f"Validated {len(runtime)} installed Python distributions "
    f"against {len(components)} CycloneDX components."
)
PY
CONTAINER
