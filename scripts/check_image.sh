#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 IMAGE" >&2
    exit 2
fi

image=$1

docker run --rm --entrypoint sh "$image" -c \
    'command -v curl >/dev/null && command -v uv >/dev/null && \
     ! command -v uvx >/dev/null && ! command -v gh >/dev/null && \
     ! command -v npm >/dev/null && ! command -v npx >/dev/null'

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
