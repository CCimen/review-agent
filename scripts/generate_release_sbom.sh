#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
    echo "usage: generate_release_sbom.sh <image> <release-tag> <output-directory>" >&2
    exit 2
fi

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
image="$1"
release_tag="$2"
output_dir="$3"

: "${SYFT_CMD:?SYFT_CMD must point to the pinned Syft executable}"
: "${EXPECTED_IMAGE_DIGEST:?EXPECTED_IMAGE_DIGEST must be the published manifest digest}"
: "${CYCLONEDX_SPEC_VERSION:?CYCLONEDX_SPEC_VERSION is required}"

python3 "$root/scripts/validate_release_tag.py" "$release_tag"

for command in docker jq sha256sum; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "$command is required to generate release SBOMs" >&2
        exit 1
    fi
done
if [[ ! -x "$SYFT_CMD" ]]; then
    echo "Syft executable is not available at $SYFT_CMD" >&2
    exit 1
fi

if [[ -e "$output_dir" ]]; then
    if [[ ! -d "$output_dir" ]]; then
        echo "SBOM output path exists and is not a directory: $output_dir" >&2
        exit 1
    fi
    if [[ -n "$(find "$output_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        echo "SBOM output directory must be empty: $output_dir" >&2
        exit 1
    fi
else
    mkdir -p "$output_dir"
fi
output_dir="$(cd "$output_dir" && pwd)"

image_ref="$image:$release_tag"
manifest_ref="$image@$EXPECTED_IMAGE_DIGEST"

require_digest() {
    local label="$1"
    local digest="$2"
    if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "$label is not a sha256 image digest: $digest" >&2
        exit 1
    fi
}

require_digest "expected manifest digest" "$EXPECTED_IMAGE_DIGEST"
raw_manifest="$(docker buildx imagetools inspect "$manifest_ref" --raw)"
manifest_digest="$EXPECTED_IMAGE_DIGEST"

platform_digest() {
    local architecture="$1"
    jq -er --arg architecture "$architecture" '
      [.manifests[]?
        | select(.platform.os == "linux" and .platform.architecture == $architecture)
        | .digest]
      | if length == 1 then .[0]
        else error("expected exactly one linux/" + $architecture + " image")
        end
    ' <<<"$raw_manifest"
}

validate_image_sbom() {
    local prefix="$1"
    jq -e '
      .bomFormat == "CycloneDX"
      and ((.components // []) | length > 0)
    ' "$output_dir/${prefix}.cyclonedx.json" >/dev/null
    jq -e '
      .spdxVersion == "SPDX-2.3"
      and ((.packages // []) | length > 0)
    ' "$output_dir/${prefix}.spdx.json" >/dev/null
    test -s "$output_dir/${prefix}.table.txt"
}

declare -a checksum_assets=("IMAGE-DIGESTS.txt")
amd64_digest_ref=""

printf 'review-agent manifest %s %s@%s\n' \
    "$image_ref" "$image" "$manifest_digest" \
    >"$output_dir/IMAGE-DIGESTS.txt"

for architecture in amd64 arm64; do
    digest="$(platform_digest "$architecture")"
    require_digest "linux/$architecture digest" "$digest"
    digest_ref="$image@$digest"
    if [[ "$architecture" == "amd64" ]]; then
        amd64_digest_ref="$digest_ref"
    fi
    prefix="review-agent-${release_tag}-linux-${architecture}"

    printf 'review-agent linux/%s %s %s\n' \
        "$architecture" "$image_ref" "$digest_ref" \
        >>"$output_dir/IMAGE-DIGESTS.txt"

    "$SYFT_CMD" "registry:$digest_ref" \
        --platform "linux/$architecture" \
        -q \
        -o "cyclonedx-json=$output_dir/${prefix}.cyclonedx.json" \
        -o "spdx-json=$output_dir/${prefix}.spdx.json" \
        -o "syft-table=$output_dir/${prefix}.table.txt"
    validate_image_sbom "$prefix"
    checksum_assets+=(
        "${prefix}.cyclonedx.json"
        "${prefix}.spdx.json"
        "${prefix}.table.txt"
    )
done

runtime_asset="review-agent-python-runtime-${release_tag}-linux-amd64.cyclonedx.json"
docker run --rm \
    --platform linux/amd64 \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/review-agent-cyclonedx-home \
    -e CYCLONEDX_SPEC_VERSION \
    -v "$output_dir:/out" \
    -v "$root/scripts/generate_python_runtime_sbom.sh:/cdx/generate-python-runtime-sbom.sh:ro" \
    -v "$root/requirements-release-sbom.txt:/cdx/requirements-release-sbom.txt:ro" \
    --entrypoint /bin/sh \
    "$amd64_digest_ref" \
    /cdx/generate-python-runtime-sbom.sh "/out/$runtime_asset"

jq -e --arg spec "$CYCLONEDX_SPEC_VERSION" '
  .bomFormat == "CycloneDX"
  and .specVersion == $spec
  and ((.components // []) | length > 0)
' "$output_dir/$runtime_asset" >/dev/null
checksum_assets+=("$runtime_asset")

(
    cd "$output_dir"
    sha256sum -- "${checksum_assets[@]}" >SBOM-SHA256SUMS.txt
)

echo "Generated release SBOMs for $image_ref from $manifest_ref"
