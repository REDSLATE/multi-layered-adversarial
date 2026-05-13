#!/usr/bin/env bash
# install_patch.sh — pull a patch from MC and install it locally.
#
# Usage (from inside any brain's sidecar container):
#   curl -s "$MONOREPO_BASE_URL/api/patches/install.sh" \
#     -H "X-Runtime-Token: $MONOREPO_INGEST_TOKEN" \
#     | bash -s -- <patch_name> [target_dir]
#
# Example:
#   curl -s ... | bash -s -- decision_machine services
#
# Behavior:
#   1. Fetches the patch manifest from MC.
#   2. Pulls each file listed in the manifest, validating sha256.
#   3. Writes files into <target_dir> (default: ./services).
#   4. Prints the install_hint at the end. Does NOT run any code.
#
# Requirements: bash, curl, python3 with stdlib only.
# Required env vars (must be set in the calling shell):
#   MONOREPO_BASE_URL
#   MONOREPO_INGEST_TOKEN

set -euo pipefail

PATCH_NAME="${1:-}"
TARGET_DIR="${2:-./services}"

if [[ -z "$PATCH_NAME" ]]; then
  echo "usage: install_patch.sh <patch_name> [target_dir]"
  exit 2
fi
if [[ -z "${MONOREPO_BASE_URL:-}" || -z "${MONOREPO_INGEST_TOKEN:-}" ]]; then
  echo "ERROR: MONOREPO_BASE_URL and MONOREPO_INGEST_TOKEN must be set"
  exit 2
fi

BASE="${MONOREPO_BASE_URL%/}"
AUTH_H="X-Runtime-Token: ${MONOREPO_INGEST_TOKEN}"

echo "→ fetching manifest for patch '${PATCH_NAME}' from MC..."
MANIFEST=$(curl -sf -H "${AUTH_H}" "${BASE}/api/patches/${PATCH_NAME}/manifest")
if [[ -z "$MANIFEST" ]]; then
  echo "ERROR: empty manifest. Is the patch name correct? Is the token valid?"
  exit 1
fi

# Parse manifest with python3 (stdlib only).
echo "$MANIFEST" | BASE="$BASE" TOKEN="$MONOREPO_INGEST_TOKEN" TARGET_DIR="$TARGET_DIR" python3 -c "
import json, sys, os, hashlib, subprocess, urllib.parse

m = json.load(sys.stdin)
print(f\"  patch:   {m['name']} v{m.get('version','?')}\")
print(f\"  files:   {m['count']}\")
print(f\"  hint:    {m['install_hint']}\")
print()

target = os.path.abspath(os.environ.get('TARGET_DIR', './services'))
os.makedirs(target, exist_ok=True)

base = os.environ['BASE']
token = os.environ['TOKEN']

for f in m['files']:
    if not f['present']:
        print(f\"  ! {f['path']} missing on MC, skipping\")
        continue
    url = f\"{base}/api/patches/{m['name']}/file/{urllib.parse.quote(f['path'])}\"
    out = subprocess.run(
        ['curl', '-sf', '-H', f'X-Runtime-Token: {token}', url],
        check=True, capture_output=True,
    ).stdout
    doc = json.loads(out)
    content = doc['content'].encode('utf-8')
    got_sha = hashlib.sha256(content).hexdigest()
    if got_sha != f['sha256']:
        print(f\"  ERROR sha256 mismatch on {f['path']}: got {got_sha} expected {f['sha256']}\")
        sys.exit(1)
    dest = os.path.join(target, f['path'])
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    with open(dest, 'wb') as fh:
        fh.write(content)
    print(f\"  installed {f['path']:40s}  {f['bytes']:>6d} bytes  sha256={got_sha[:12]}\")

print()
print(f\"installed into: {target}\")
print('done.')
"
