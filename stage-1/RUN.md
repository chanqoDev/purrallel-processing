# Tablekeeper Stage 1

Build and run from this directory:

```sh
docker build -t tablekeeper-stage1 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage1
```

The service listens on `0.0.0.0:$PORT` (8080 by default). It has no runtime network dependencies. State uses SQLite in `/tmp/tablekeeper.sqlite3` and is intentionally process/container local.

Check readiness with `curl http://localhost:8080/health`.

## Fixture loading

`POST /_test/reset` replaces all state. Its JSON body contains `users`, `restaurants`, and optional `reservations` arrays. Opening hours use weekday keys `mon` through `sun`, each with `opens` and `closes` local times. Example:

```json
{
  "users": [{"id":"u1","email":"guest@example.test","password":"password1","display_name":"Guest"}],
  "restaurants": [{
    "id":"r1","name":"Example","timezone":"Europe/Berlin",
    "slot_minutes":30,"reservation_duration_minutes":90,"cancellation_cutoff_minutes":60,
    "opening_hours":[{"weekday":"mon","opens":"09:00","closes":"17:00"}],
    "tables":[{"id":"t1","label":"Table 1","capacity":4}]
  }]
}
```

Every error is JSON in the documented `{"error":{"code":"…","message":"…"}}` shape. Protected endpoints accept `Authorization: Bearer <token>`; use `/auth/signup` or `/auth/login` to obtain one. Reservation creation and batch moves require `Idempotency-Key`.
