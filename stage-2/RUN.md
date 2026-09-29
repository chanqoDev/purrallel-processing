# Tablekeeper Stage 2

Build and launch from this directory:

```sh
docker build -t tablekeeper-stage2 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage2
```

The service listens on `0.0.0.0:$PORT` (8080 by default). It serves the browser app and API from one container with no runtime network dependencies. SQLite state is local to the container at `/tmp/tablekeeper-stage2.sqlite3`.

Open `http://localhost:8080/` to search, or visit `/signup`, `/login`, and `/lookup` directly. Check readiness with `curl http://localhost:8080/health`.

## Loading a fixture

`POST /_test/reset` accepts a fixture with `users`, `restaurants`, and optional `reservations` arrays. Opening hours are objects with a weekday and local opening/closing times. Optional `combinable` pairs enable approved joined seating:

```json
{
  "users": [{"id":"u1","email":"guest@example.test","password":"password1","display_name":"Guest"}],
  "restaurants": [{
    "id":"r1","name":"Example","timezone":"Europe/Berlin",
    "slot_minutes":30,"reservation_duration_minutes":90,"cancellation_cutoff_minutes":60,
    "opening_hours":[{"weekday":"mon","opens":"09:00","closes":"17:00"}],
    "tables":[{"id":"t1","label":"Window table","capacity":2},{"id":"t2","label":"Garden table","capacity":4}],
    "combinable":[["t1","t2"]]
  }],
  "reservations":[]
}
```

The API preserves Stage 1 authentication, idempotency, booking, cancellation, amendment, batch moves, DST handling, and export/import. It also accepts Stage 1 exports through `POST /_test/import`.

