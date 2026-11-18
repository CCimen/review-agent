FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS frontend
WORKDIR /build
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
COPY examples/repository-context/.review-agent/documentation.toml /examples/repository-context/.review-agent/documentation.toml
RUN npm run build \
    && npm sbom --sbom-format=cyclonedx --package-lock-only > frontend.cyclonedx.json \
    && sha256sum package-lock.json > frontend-lock.sha256

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/admin-venv/bin:$PATH
WORKDIR /app
COPY requirements/requirements-admin.txt /app/requirements-admin.txt
# Apply security fixes published after the pinned base image was built.
# Debian package maintenance needs Perl, so remove it only after installation.
# The API does not need Perl at runtime.
RUN apt-get -o Acquire::Retries=3 update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 upgrade \
        -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/admin-venv \
    && /opt/admin-venv/bin/pip install --no-cache-dir --disable-pip-version-check -r requirements-admin.txt \
    && dpkg --purge --force-remove-essential perl-base
# The package is installed on top of the lock: --no-deps keeps every dependency
# on its pin. The source is mounted for the build only, so no layer holds it,
# and nothing needs pip or its vendored packages after that.
RUN --mount=type=bind,target=/mnt,rw \
    /opt/admin-venv/bin/pip install --no-cache-dir --disable-pip-version-check --no-deps /mnt \
    && /opt/admin-venv/bin/python -m pip uninstall --yes pip \
    && /usr/local/bin/python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip
COPY --from=frontend /build/dist /app/admin/dist
COPY --from=frontend /build/frontend.cyclonedx.json /build/frontend-lock.sha256 /app/share/review-agent/
ARG REVIEW_AGENT_VERSION=development
ARG REVIEW_AGENT_REVISION=unknown
RUN --mount=type=bind,source=scripts,target=/mnt \
    python /mnt/write_build_info.py \
    /opt/admin-venv/lib/python3.14/site-packages/review_agent/_build.json \
    --version "$REVIEW_AGENT_VERSION" --revision "$REVIEW_AGENT_REVISION"
LABEL org.opencontainers.image.version=$REVIEW_AGENT_VERSION \
      org.opencontainers.image.revision=$REVIEW_AGENT_REVISION
USER 10000:10000
EXPOSE 8090
CMD ["uvicorn", "review_agent.console.app:app_from_environment", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-access-log", "--no-proxy-headers"]
