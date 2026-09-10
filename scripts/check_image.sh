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
