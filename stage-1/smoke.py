"""Small end-to-end smoke check; run against a service URL."""
import json
import sys
import urllib.error
import urllib.request

BASE=sys.argv[1].rstrip("/") if len(sys.argv)>1 else "http://127.0.0.1:8080"
def request(path,method="GET",body=None,headers=None):
    raw=None if body is None else json.dumps(body).encode()
    h=dict(headers or {})
    if raw is not None:h["Content-Type"]="application/json"
    req=urllib.request.Request(BASE+path,data=raw,headers=h,method=method)
    try:
        with urllib.request.urlopen(req,timeout=5) as resp:return resp.status,resp.read()
    except urllib.error.HTTPError as e:return e.code,e.read()

assert request("/health")[0]==200
fixture={"users":[{"id":"u","email":"u@example.test","password":"password1","display_name":"U"}],
 "restaurants":[{"id":"r","name":"R","timezone":"Europe/Berlin","slot_minutes":30,"reservation_duration_minutes":60,"cancellation_cutoff_minutes":0,
 "opening_hours":[{"weekday":"tue","opens":"09:00","closes":"17:00"}],"tables":[{"id":"t","label":"T","capacity":4}]}]}
assert request("/_test/reset","POST",fixture)[0]==204
status,payload=request("/auth/login","POST",{"email":"u@example.test","password":"password1"})
assert status==200
token=json.loads(payload)["token"];auth={"Authorization":"Bearer "+token}
status,payload=request("/reservations","POST",{"restaurant_id":"r","table_id":"t","starts_at_local":"2026-09-29T10:00","party_size":2},{**auth,"Idempotency-Key":"smoke"})
assert status==201,payload
reference=json.loads(payload)["reference"]
replay_status,replay=request("/reservations","POST",{"restaurant_id":"r","table_id":"t","starts_at_local":"2026-09-29T10:00","party_size":2},{**auth,"Idempotency-Key":"smoke"})
assert replay_status==200 and replay==payload,(replay_status,payload,replay)
assert request("/_test/export")[0]==200
avail_status,avail=request("/availability?restaurant_id=r&date=2026-09-29&party_size=2")
assert avail_status==200
at_ten=next(slot for slot in json.loads(avail)["slots"] if slot["starts_at_local"].endswith("10:00"))
assert at_ten["available_table_ids"]==[]
cancel_status,_=request("/reservations/"+reference+"/cancel","POST",{},auth)
assert cancel_status==200
again_status,again=request("/reservations/"+reference,"GET",headers=auth)
assert again_status==200 and json.loads(again)["status"]=="cancelled"
_,avail=request("/availability?restaurant_id=r&date=2026-09-29&party_size=2")
at_ten=next(slot for slot in json.loads(avail)["slots"] if slot["starts_at_local"].endswith("10:00"))
assert at_ten["available_table_ids"]==["t"]
print("smoke checks passed")
