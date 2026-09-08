#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 IMAGE" >&2
    exit 2
fi

docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
    --entrypoint python "$1" -c '
import os
import json
from pathlib import Path
from review_agent_tools.admin_api import create_app
from review_agent_tools.build_info import read_build_info
from review_agent_tools.postgres.runtime import PostgreSQLRuntime
from review_agent_tools.settings import PostgresDatabaseUrl
assert os.getuid() == 10000
static = Path("/app/admin/dist")
assert (static / "index.html").is_file()
assert list((static / "assets").glob("*.js"))
build_path = Path("/app/bootstrap/plugins/review_agent_tools/_build.json")
assert build_path.is_file()
assert read_build_info().model_dump() == json.loads(build_path.read_bytes())
inventory = json.loads(Path("/app/share/review-agent/frontend.cyclonedx.json").read_bytes())
assert inventory["bomFormat"] == "CycloneDX"
assert {"@tanstack/react-query", "react", "react-dom", "swagger-ui"} <= {item["name"] for item in inventory["components"]}
assert not (static / "frontend.cyclonedx.json").exists()
runtime = PostgreSQLRuntime(PostgresDatabaseUrl("postgresql://localhost/not_connected"))
try:
    create_app(runtime, public_url="", static_dir=static)
except ValueError:
    pass
else:
    raise AssertionError("missing public origin must fail closed")
app = create_app(runtime, public_url="https://admin.example.test", static_dir=static)
schema = app.openapi()
assert {"/api/repositories", "/api/history", "/api/auth/login", "/api/users", "/api/version"} <= set(schema["paths"])
assert set(schema["paths"]["/api/history"]) == {"get"}
assert "/api/auth/register" not in schema["paths"]
assert not Path("/opt/hermes").exists()
print("Admin image: non-root runtime, compiled frontend, authenticated reports and account management")
'
