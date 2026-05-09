# Auth Testing Playbook (RISEDUAL Admin)

Auth strategy: Custom email/password JWT (httpOnly cookies) with seeded admin.

## Seeded Credentials
See `/app/memory/test_credentials.md`.

## Endpoints
- POST /api/auth/login — body: {email, password}, sets httpOnly cookies, returns user
- GET  /api/auth/me — returns current user (requires cookie)
- POST /api/auth/logout — clears cookies
- POST /api/auth/refresh — issues new access cookie from refresh cookie

## Manual API Test
```
API_URL=$(grep REACT_APP_BACKEND_URL /app/frontend/.env | cut -d '=' -f2)
curl -c cookies.txt -X POST "$API_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@risedual.io","password":"risedual-admin-2026"}'
curl -b cookies.txt "$API_URL/api/auth/me"
```

## Mongo Verification
```
mongosh
use test_database
db.users.find({role:"admin"}).pretty()
db.users.getIndexes()  # email unique
db.login_attempts.getIndexes()
db.password_reset_tokens.getIndexes()  # TTL on expires_at
```

## Frontend
- AuthContext checks /api/auth/me on mount
- Protected routes redirect to /login if 401
- All axios calls use withCredentials: true
- Error rendering uses formatApiErrorDetail to avoid React crash on FastAPI 422 array detail
