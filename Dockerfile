ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.8.27@sha256:e0df6adebddf29b91112aefc999d4aaf6846c9eb544faca5672a16a13590ff79
ARG REVIEW_AGENT_UV_IMAGE=ghcr.io/astral-sh/uv:0.12.7-python3.13-trixie@sha256:767ae9f0bb33c54c8b6d1fc7e1ec842f85b18146936d7d0b9f16a357cec4f3fe
FROM ${REVIEW_AGENT_UV_IMAGE} AS review_agent_uv
FROM ${HERMES_IMAGE}

ARG HERMES_IMAGE
ENV REVIEW_AGENT_HERMES_IMAGE=${HERMES_IMAGE}

USER root
# A digest pin makes the upstream filesystem reproducible, but security fixes
# published after that image was built still need to be applied to the release
# candidate. Review Agent does not expose Hermes' npm-based terminal tooling,
# so remove that package manager rather than carry an unused executable tree.
RUN apt-get -o Acquire::Retries=3 update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 upgrade \
        -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
        /usr/local/lib/node_modules/npm \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/uvx
COPY --chmod=0755 --from=review_agent_uv /usr/local/bin/uv /usr/local/bin/uv
COPY --chown=root:root requirements.txt /opt/review-agent-requirements.txt
RUN uv pip install --no-cache --python /opt/hermes/.venv/bin/python \
        --requirement /opt/review-agent-requirements.txt

COPY --chown=hermes:hermes bootstrap/ /opt/review-agent-bootstrap/
# Offline operator helpers imported by review-agent-memory. The webhook agent
# cannot reach them because file, terminal, and code execution are disabled.
COPY --chown=root:root tools/review_agent_*.py /usr/local/bin/
RUN cp /usr/local/bin/review_agent_memory.py /usr/local/bin/review-agent-memory \
    && cp /usr/local/bin/review_agent_admin.py /usr/local/bin/review-agent-admin \
    && cp /usr/local/bin/review_agent_admission.py /usr/local/bin/review-agent-admission \
    && cp /usr/local/bin/review_agent_worker.py /usr/local/bin/review-agent-worker \
    && cp /usr/local/bin/review_agent_publisher.py /usr/local/bin/review-agent-publisher \
    && cp /usr/local/bin/review_agent_github_gateway.py /usr/local/bin/review-agent-github-gateway \
    && cp /usr/local/bin/review_agent_github_app_worker.py /usr/local/bin/review-agent-github-app-worker \
    && cp /usr/local/bin/review_agent_hermes_contract.py /usr/local/bin/review-agent-hermes-contract \
    && chmod 0755 /opt/review-agent-bootstrap/install.sh \
    /opt/review-agent-bootstrap/install.py \
    /usr/local/bin/review-agent-memory \
    /usr/local/bin/review-agent-admin \
    /usr/local/bin/review-agent-admission \
    /usr/local/bin/review-agent-hermes-contract \
    /usr/local/bin/review-agent-worker \
    /usr/local/bin/review-agent-github-gateway \
    /usr/local/bin/review-agent-github-app-worker \
    /usr/local/bin/review-agent-publisher

# Hermes runs s6-overlay as PID 1, which must start as root to initialize /run
# and then drops privileges to the unprivileged hermes user (uid 10000) on its
# own. Do NOT add `USER hermes` here: a non-root PID 1 leaves s6 unable to chown
# /run, and the container crash-loops at preinit (exit code 100). The gateway
# still runs unprivileged via that s6-managed privilege drop.
