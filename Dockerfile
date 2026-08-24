ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.8.3@sha256:16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e
FROM ${HERMES_IMAGE}

USER root
COPY --chown=root:root requirements.txt /opt/review-agent-requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gh \
    && uv pip install --no-cache --python /opt/hermes/.venv/bin/python \
        --requirement /opt/review-agent-requirements.txt \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=hermes:hermes bootstrap/ /opt/review-agent-bootstrap/
# Offline operator helpers imported by review-agent-memory. The webhook agent
# cannot reach them because file, terminal, and code execution are disabled.
COPY --chown=root:root tools/review_agent_*.py /usr/local/bin/
RUN cp /usr/local/bin/review_agent_memory.py /usr/local/bin/review-agent-memory \
    && cp /usr/local/bin/review_agent_database.py /usr/local/bin/review-agent-database \
    && cp /usr/local/bin/review_agent_feedback_bridge.py /usr/local/bin/review-agent-feedback-bridge \
    && cp /usr/local/bin/review_agent_admission.py /usr/local/bin/review-agent-admission \
    && cp /usr/local/bin/review_agent_worker.py /usr/local/bin/review-agent-worker \
    && cp /usr/local/bin/review_agent_hermes_contract.py /usr/local/bin/review-agent-hermes-contract \
    && chmod 0755 /opt/review-agent-bootstrap/install.sh \
    /opt/review-agent-bootstrap/install.py \
    /usr/local/bin/review-agent-memory \
    /usr/local/bin/review-agent-database \
    /usr/local/bin/review-agent-feedback-bridge \
    /usr/local/bin/review-agent-admission \
    /usr/local/bin/review-agent-hermes-contract \
    /usr/local/bin/review-agent-worker

# Hermes runs s6-overlay as PID 1, which must start as root to initialize /run
# and then drops privileges to the unprivileged hermes user (uid 10000) on its
# own. Do NOT add `USER hermes` here: a non-root PID 1 leaves s6 unable to chown
# /run, and the container crash-loops at preinit (exit code 100). The gateway
# still runs unprivileged via that s6-managed privilege drop.
