#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 4 ]]; then
    echo "usage: generate_release_sbom.sh <image> <release-tag> <output-directory> <admin-image>" >&2
    exit 2
fi

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
image="$1"
release_tag="$2"
output_dir="$3"
admin_image="$4"

: "${SYFT_CMD:?SYFT_CMD must point to the pinned Syft executable}"
: "${EXPECTED_IMAGE_DIGEST:?EXPECTED_IMAGE_DIGEST must be the published manifest digest}"
: "${EXPECTED_ADMIN_IMAGE_DIGEST:?EXPECTED_ADMIN_IMAGE_DIGEST must be the admin manifest digest}"
: "${CYCLONEDX_SPEC_VERSION:?CYCLONEDX_SPEC_VERSION is required}"
: "${SOURCE_SHA:?SOURCE_SHA must be the verified release source revision}"
if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "SOURCE_SHA must be a full lowercase Git SHA" >&2
    exit 1
fi

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
metadata_dir="$(mktemp -d)"
container_id=""
cleanup() {
    if [[ -n "$container_id" ]]; then docker rm --volumes "$container_id" >/dev/null; fi
    rm -r -- "$metadata_dir"
}
trap cleanup EXIT


require_digest() {
    local label="$1"
    local digest="$2"
    if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "$label is not a sha256 image digest: $digest" >&2
        exit 1
    fi
}


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
: >"$output_dir/IMAGE-DIGESTS.txt"

generate_image_sboms() {
    local component="$1" image="$2" manifest_digest="$3" runtime_python="$4"
    local image_ref="$image:$release_tag" manifest_ref="$image@$manifest_digest"
    local raw_manifest architecture digest digest_ref prefix runtime_asset
    require_digest "$component manifest digest" "$manifest_digest"
    raw_manifest="$(docker buildx imagetools inspect "$manifest_ref" --raw)"
    local amd64_digest_ref=""

    printf '%s manifest %s %s@%s\n' \
        "$component" "$image_ref" "$image" "$manifest_digest" \
        >>"$output_dir/IMAGE-DIGESTS.txt"

    for architecture in amd64 arm64; do
        digest="$(platform_digest "$architecture")"
        require_digest "linux/$architecture digest" "$digest"
        digest_ref="$image@$digest"
        if [[ "$architecture" == "amd64" ]]; then
            amd64_digest_ref="$digest_ref"
        fi
        prefix="${component}-${release_tag}-linux-${architecture}"

        printf '%s linux/%s %s %s\n' \
            "$component" "$architecture" "$image_ref" "$digest_ref" \
            >>"$output_dir/IMAGE-DIGESTS.txt"

        docker buildx imagetools inspect "$digest_ref" --format '{{json .Image}}' \
            | jq -e --arg revision "$SOURCE_SHA" --arg version "$release_tag" \
                '.config.Labels["org.opencontainers.image.revision"] == $revision
                 and .config.Labels["org.opencontainers.image.version"] == $version' >/dev/null || {
                    echo "$component linux/$architecture registry labels do not match the verified release" >&2
                    exit 1
                }
        if [[ "$component" == review-agent-admin ]]; then
            # Creating a stopped container reads either architecture without emulation.
            container_id="$(docker create --platform "linux/$architecture" "$digest_ref")"
            docker cp "$container_id:/app/bootstrap/plugins/review_agent_tools/_build.json" "$metadata_dir/build.json"
            jq -e --arg version "$release_tag" --arg revision "$SOURCE_SHA" \
                '. == {version: $version, revision: $revision}' \
                "$metadata_dir/build.json" >/dev/null || {
                    echo "$component linux/$architecture build metadata does not match the verified release" >&2
                    exit 1
                }
            docker cp "$container_id:/app/share/review-agent/frontend-lock.sha256" "$metadata_dir/frontend-lock.sha256"
            test "$(cat "$metadata_dir/frontend-lock.sha256")" = \
                "$(cd "$root/admin" && sha256sum package-lock.json)" || {
                    echo "$component linux/$architecture frontend lockfile does not match the verified source" >&2
                    exit 1
                }
            docker cp "$container_id:/app/share/review-agent/frontend.cyclonedx.json" "$metadata_dir/frontend.cyclonedx.json"
            local frontend_asset="review-agent-admin-frontend-${release_tag}-linux-${architecture}.cyclonedx.json"
            python3 "$root/scripts/prepare_frontend_sbom.py" \
                "$metadata_dir/frontend.cyclonedx.json" "$root/admin/package-lock.json" "$output_dir/$frontend_asset" \
                --version "$release_tag" --revision "$SOURCE_SHA" \
                --image "$digest_ref" --platform "linux/$architecture"
            checksum_assets+=("$frontend_asset")
            docker rm --volumes "$container_id" >/dev/null
            container_id=""
        fi

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

    runtime_asset="${component}-python-runtime-${release_tag}-linux-amd64.cyclonedx.json"
    sh "$root/scripts/generate_python_runtime_sbom.sh" \
        "$amd64_digest_ref" "$runtime_python" "$output_dir/$runtime_asset"

    jq -e --arg spec "$CYCLONEDX_SPEC_VERSION" '
      .bomFormat == "CycloneDX"
      and .specVersion == $spec
      and ((.components // []) | length > 0)
    ' "$output_dir/$runtime_asset" >/dev/null
    checksum_assets+=("$runtime_asset")
}

generate_image_sboms review-agent "$image" "$EXPECTED_IMAGE_DIGEST" /opt/hermes/.venv/bin/python
generate_image_sboms review-agent-admin "$admin_image" "$EXPECTED_ADMIN_IMAGE_DIGEST" /opt/admin-venv/bin/python

(
    cd "$output_dir"
    sha256sum -- "${checksum_assets[@]}" >SBOM-SHA256SUMS.txt
)

echo "Generated release SBOMs for both images at $release_tag"
