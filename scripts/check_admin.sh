#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
admin_contract_dir=$(mktemp -d)
trap 'rm -rf "$admin_contract_dir"' EXIT HUP INT TERM
python3 scripts/generate_admin_openapi.py > "$admin_contract_dir/openapi.json"
diff -u console/openapi.json "$admin_contract_dir/openapi.json"
npm --prefix console ci
console/node_modules/.bin/openapi-typescript console/openapi.json -o "$admin_contract_dir/api.generated.ts"
diff -u console/src/api.generated.ts "$admin_contract_dir/api.generated.ts"
npm --prefix console run build
npm --prefix console run build:preview
npm --prefix console test
