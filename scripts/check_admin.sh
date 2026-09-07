#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
admin_contract_dir=$(mktemp -d)
trap 'rm -rf "$admin_contract_dir"' EXIT HUP INT TERM
python3 tools/generate_admin_openapi.py > "$admin_contract_dir/openapi.json"
diff -u admin/openapi.json "$admin_contract_dir/openapi.json"
npm --prefix admin ci
admin/node_modules/.bin/openapi-typescript admin/openapi.json -o "$admin_contract_dir/api.generated.ts"
diff -u admin/src/api.generated.ts "$admin_contract_dir/api.generated.ts"
npm --prefix admin run build
