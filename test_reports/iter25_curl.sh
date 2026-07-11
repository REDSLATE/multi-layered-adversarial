#!/bin/bash
set +e
BASE="https://multi-brain-backbone.preview.emergentagent.com"

TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@risedual.io","password":"risedual-admin-2026"}' | python -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))")
echo "token len=${#TOKEN}"

hdr="Authorization: Bearer $TOKEN"

for path in "/api/shared/technical/symbols?limit=5" "/api/shared/technical/feeders" "/api/shared/opinions?runtime=barracuda&limit=10" "/api/admin/runtime/stack/status" "/api/admin/trader/status"; do
  echo ""
  echo "=== $path ==="
  status=$(curl -s -o /tmp/resp.json -w "%{http_code}" -H "$hdr" "$BASE$path")
  echo "status=$status"
  echo "body_head:"
  head -c 400 /tmp/resp.json
  echo ""
done

echo ""
echo "=== stack/status parsed ==="
curl -s -H "$hdr" "$BASE/api/admin/runtime/stack/status" > /tmp/st.json
python -c "
import json
d=json.load(open('/tmp/st.json'))
print('ok=',d.get('ok'),'degraded=',d.get('degraded'))
print('equity_market_open at top level=', 'equity_market_open' in d, 'value=', d.get('equity_market_open'))
brains=d.get('brains',{})
print('brains=',list(brains))
for b,s in brains.items():
    print(f'  {b}: write_health={s.get(\"write_health\")}, has_ages={\"_ages\" in s}')
"

echo ""
echo "=== shared_opinions indexes (local Mongo) ==="
python <<'PY'
import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
async def main():
    c = AsyncIOMotorClient(os.environ.get('MONGO_URL','mongodb://localhost:27017'))
    db = c[os.environ.get('DB_NAME','risedual_prod')]
    idx = await db['shared_opinions'].index_information()
    for name,info in idx.items():
        print(name, '->', info.get('key'))
asyncio.run(main())
PY
