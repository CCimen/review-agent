FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS frontend
WORKDIR /build
COPY admin/package.json admin/package-lock.json ./
RUN npm ci
COPY admin/ ./
RUN npm run build \
    && npm sbom --sbom-format=cyclonedx --package-lock-only > frontend.cyclonedx.json \
    && sha256sum package-lock.json > frontend-lock.sha256

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/bootstrap/plugins \
    PATH=/opt/admin-venv/bin:$PATH
WORKDIR /app
COPY requirements-admin.txt /app/requirements-admin.txt
# Debian package maintenance needs Perl, so remove it only after installation.
# The API needs neither Perl nor pip and its vendored packages at runtime.
RUN python -m venv /opt/admin-venv \
    && /opt/admin-venv/bin/pip install --no-cache-dir --disable-pip-version-check -r requirements-admin.txt \
    && /opt/admin-venv/bin/python -m pip uninstall --yes pip \
    && /usr/local/bin/python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip \
    && dpkg --purge --force-remove-essential perl-base
COPY bootstrap/plugins/review_agent_tools /app/bootstrap/plugins/review_agent_tools
COPY --from=frontend /build/dist /app/admin/dist
COPY --from=frontend /build/frontend.cyclonedx.json /build/frontend-lock.sha256 /app/share/review-agent/
COPY scripts/write_build_info.py scripts/validate_release_tag.py /tmp/build-info/
ARG REVIEW_AGENT_VERSION=development
ARG REVIEW_AGENT_REVISION=unknown
RUN python /tmp/build-info/write_build_info.py /app/bootstrap/plugins/review_agent_tools/_build.json \
    --version "$REVIEW_AGENT_VERSION" --revision "$REVIEW_AGENT_REVISION" \
    && rm -r /tmp/build-info
LABEL org.opencontainers.image.version=$REVIEW_AGENT_VERSION \
      org.opencontainers.image.revision=$REVIEW_AGENT_REVISION
USER 10000:10000
EXPOSE 8090
CMD ["uvicorn", "review_agent_tools.admin_api:app_from_environment", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-access-log", "--no-proxy-headers"]
