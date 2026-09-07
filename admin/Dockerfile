FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS frontend
WORKDIR /build
COPY admin/package.json admin/package-lock.json ./
RUN npm ci
COPY admin/ ./
RUN npm run build

FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/bootstrap/plugins \
    PATH=/opt/admin-venv/bin:$PATH
WORKDIR /app
COPY requirements-admin.txt /app/requirements-admin.txt
RUN python -m venv /opt/admin-venv \
    && /opt/admin-venv/bin/pip install --no-cache-dir --disable-pip-version-check -r requirements-admin.txt
COPY bootstrap/plugins/review_agent_tools /app/bootstrap/plugins/review_agent_tools
COPY --from=frontend /build/dist /app/admin/dist
USER 10000:10000
EXPOSE 8090
CMD ["uvicorn", "review_agent_tools.admin_api:app_from_environment", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-access-log", "--no-proxy-headers"]
