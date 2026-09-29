#!/usr/bin/env python3
"""Tablekeeper Stage 2: dependency-free HTTP service and browser app."""
import base64
from contextlib import nullcontext
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DB = os.environ.get("TABLEKEEPER_DB", "/tmp/tablekeeper-stage2.sqlite3")
LOCK = threading.RLock()
SCRYPT_N = 2**12

def connect():
    c=sqlite3.connect(DB, timeout=5, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def schema(c):
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE,password TEXT,display_name TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS restaurants(id TEXT PRIMARY KEY,name TEXT,timezone TEXT,slot_minutes INTEGER,reservation_duration_minutes INTEGER,cancellation_cutoff_minutes INTEGER,opening_hours TEXT,tables_json TEXT,combinable TEXT);
    CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,reference TEXT UNIQUE,user_id TEXT,restaurant_id TEXT,table_id TEXT,table_ids_json TEXT,party_size INTEGER,status TEXT,starts_at_local TEXT,starts_at TEXT,ends_at TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS idem(user_id TEXT,ikey TEXT,method TEXT,path TEXT,body TEXT,status INTEGER,response TEXT,PRIMARY KEY(user_id,ikey));
    """)

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def canon(v):
    return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False)

def password_hash(p):
    salt=secrets.token_bytes(16)
    digest=hashlib.scrypt(p.encode(),salt=salt,n=SCRYPT_N,r=8,p=1)
    return f"scrypt${SCRYPT_N}$"+base64.b64encode(salt).decode()+"$"+digest.hex()

def password_ok(p, stored):
    try:
        parts=stored.split("$")
        if len(parts)==3: # hashes exported by the earlier Stage 1 image
            _,s,h=parts;n=2**14
        else:
            _,n,s,h=parts;n=int(n)
        if n not in (2**12,2**14):return False
        salt=base64.b64decode(s)
        return hmac.compare_digest(hashlib.scrypt(p.encode(),salt=salt,n=n,r=8,p=1).hex(),h)
    except Exception:return False

class ApiError(Exception):
    def __init__(self,status,code,message): self.status=status;self.code=code;self.message=message

def fail(status,code,message): raise ApiError(status,code,message)
def obj(v):
    if not isinstance(v,dict): fail(400,"malformed_request","Expected a JSON object")
    return v
def string(v): return isinstance(v,str)
def integer(v): return isinstance(v,int) and not isinstance(v,bool)
def email_valid(s): return bool(re.fullmatch(r"[^@\s]+@[^@\s]+",s))

def local_instant(local, zone):
    try: naive=dt.datetime.strptime(local,"%Y-%m-%dT%H:%M")
    except Exception: fail(422,"validation_failed","starts_at_local must be YYYY-MM-DDTHH:MM")
    if naive.strftime("%Y-%m-%dT%H:%M") != local: fail(422,"validation_failed","Invalid local date or time")
    z=ZoneInfo(zone)
    # Fold 0 is the first wall-clock occurrence. Roundtrip rejects spring gaps.
    aware=naive.replace(tzinfo=z,fold=0)
    if aware.astimezone(dt.timezone.utc).astimezone(z).replace(tzinfo=None)!=naive:
        fail(422,"invalid_local_time","Local time does not exist in this timezone")
    return aware

def instant(s): return dt.datetime.fromisoformat(s)
def local_end(start,minutes):
    return (start.astimezone(dt.timezone.utc)+dt.timedelta(minutes=minutes)).astimezone(start.tzinfo)
def same_date(value,date): return value[:10]==date

def hours_map(value):
    if isinstance(value,list):
        return {h["weekday"]:{"opens":h["opens"],"closes":h["closes"]} for h in value}
    return value

def hours_array(value):
    if isinstance(value,list): return value
    return [{"weekday":day,"opens":hours["opens"],"closes":hours["closes"]} for day,hours in value.items()]

def row_table_ids(row):
    try:
        values=json.loads(row["table_ids_json"])
        if isinstance(values,list):return values
    except (KeyError,TypeError,ValueError):pass
    return [row["table_id"]] if row["table_id"] else []

def normalize_table_ids(restaurant, value, *, fixture=False):
    if not isinstance(value,list) or not value:
        fail(422,"validation_failed","A non-empty table selection is required")
    if any(not string(x) for x in value):fail(400,"malformed_request","Table ids must be strings")
    if len(set(value))!=len(value):fail(422,"validation_failed","Duplicate table id")
    if len(value)>2:fail(422,"validation_failed" if fixture else "combination_not_allowed","At most two tables may be combined")
    tables=json.loads(restaurant["tables_json"])
    by_id={t["id"]:t for t in tables}
    for table_id in value:
        if len(table_id)>64:fail(422,"validation_failed","ID is too long")
        if table_id not in by_id:fail(422 if fixture else 404,"validation_failed" if fixture else "not_found","Unknown table")
    if len(value)==1:return [value[0]],by_id[value[0]]["capacity"]
    pairs=json.loads(restaurant["combinable"] or "[]")
    match=next((pair for pair in pairs if len(pair)==2 and set(pair)==set(value)),None)
    if match is None:
        fail(422,"validation_failed" if fixture else "combination_not_allowed","This table pair is not declared combinable")
    return list(match),sum(by_id[x]["capacity"] for x in match)

def occupied(c, restaurant_id, table_ids, start, end, skip_id=None):
    start_utc=start.astimezone(dt.timezone.utc);end_utc=end.astimezone(dt.timezone.utc)
    for reservation in c.execute("SELECT id,table_id,table_ids_json,starts_at,ends_at FROM reservations WHERE restaurant_id=? AND status='confirmed'",(restaurant_id,)):
        if skip_id and reservation["id"]==skip_id:continue
        if not set(table_ids).intersection(row_table_ids(reservation)):continue
        if start_utc<instant(reservation["ends_at"]) and instant(reservation["starts_at"])<end_utc:return True
    return False

def seed(c,data):
    if not isinstance(data,dict): fail(400,"malformed_request","Expected a JSON object")
    if "users" not in data or "restaurants" not in data: fail(422,"validation_failed","users and restaurants are required")
    users=data.get("users",[])
    restaurants=data.get("restaurants",[])
    if not isinstance(users,list) or not isinstance(restaurants,list): fail(400,"malformed_request","users and restaurants must be arrays")
    for u in users:
        if not isinstance(u,dict): fail(400,"malformed_request","Invalid user object")
        if any(k not in u for k in ("id","email","password","display_name")): fail(422,"validation_failed","Required user fields are missing")
        if not all(string(u.get(k)) for k in ("id","email","password","display_name")): fail(400,"malformed_request","Invalid user field types")
        if len(u["id"])>64: fail(422,"validation_failed","ID is too long")
        if len(u["password"])<8 or not email_valid(u["email"]): fail(422,"validation_failed","Invalid user credentials")
        c.execute("INSERT INTO users VALUES(?,?,?,?)",(u["id"],u["email"],password_hash(u["password"]),u["display_name"]))
    for r in restaurants:
        if not isinstance(r,dict): fail(400,"malformed_request","Invalid restaurant object")
        if any(k not in r for k in ("id","name","timezone")): fail(422,"validation_failed","Required restaurant fields are missing")
        if not all(string(r.get(k)) for k in ("id","name","timezone")): fail(400,"malformed_request","Invalid restaurant field types")
        if len(r["id"])>64: fail(422,"validation_failed","ID is too long")
        try: ZoneInfo(r["timezone"])
        except (ZoneInfoNotFoundError,ValueError): fail(422,"validation_failed","Invalid timezone")
        hours=r.get("opening_hours",[])
        tables=r.get("tables",[])
        if not isinstance(hours,list) or not isinstance(tables,list): fail(400,"malformed_request","Opening hours and tables must be arrays")
        for h in hours:
            if not isinstance(h,dict): fail(400,"malformed_request","Invalid opening-hours object")
            if any(k not in h for k in ("weekday","opens","closes")): fail(422,"validation_failed","Required opening-hours fields are missing")
            if not all(string(h.get(k)) for k in ("weekday","opens","closes")): fail(400,"malformed_request","Invalid opening-hours field types")
            if h["weekday"] not in ("mon","tue","wed","thu","fri","sat","sun"):
                fail(422,"validation_failed","Invalid opening hours")
            try:
                opens=dt.datetime.strptime(h["opens"],"%H:%M").strftime("%H:%M")
                closes=dt.datetime.strptime(h["closes"],"%H:%M").strftime("%H:%M")
            except Exception: fail(422,"validation_failed","Invalid opening-hours time")
            if opens!=h["opens"] or closes!=h["closes"] or closes<=opens: fail(422,"validation_failed","Invalid opening-hours interval")
        for t in tables:
            if not isinstance(t,dict) or not all(string(t.get(k)) for k in ("id","label")) or not integer(t.get("capacity")):
                fail(400,"malformed_request","Invalid table field types")
            if len(t["id"])>64:fail(422,"validation_failed","ID is too long")
        numeric_fields=("slot_minutes","reservation_duration_minutes","cancellation_cutoff_minutes")
        if any(k not in r for k in numeric_fields): fail(422,"validation_failed","Required restaurant settings are missing")
        if any(not integer(r[k]) for k in numeric_fields): fail(400,"malformed_request","Invalid restaurant configuration types")
        slot,duration,cutoff=(r[k] for k in numeric_fields)
        if min(slot,duration)<1 or cutoff<0: fail(422,"validation_failed","Invalid restaurant configuration")
        if any(t["capacity"]<1 for t in tables):fail(422,"validation_failed","Table capacity must be positive")
        combinable=r.get("combinable",[])
        if not isinstance(combinable,list):fail(400,"malformed_request","combinable must be an array")
        known={t["id"] for t in tables}
        seen=set()
        for pair in combinable:
            if not isinstance(pair,list) or len(pair)!=2:
                fail(422,"validation_failed","Invalid combinable pair")
            if any(not string(x) for x in pair):fail(400,"malformed_request","Combination ids must be strings")
            if pair[0]==pair[1] or any(x not in known for x in pair) or frozenset(pair) in seen:
                fail(422,"validation_failed","Invalid combinable pair")
            seen.add(frozenset(pair))
        c.execute("INSERT INTO restaurants VALUES(?,?,?,?,?,?,?,?,?)",(r["id"],r["name"],r["timezone"],slot,duration,cutoff,canon(hours),canon(tables),canon(combinable)))
    reservations=data.get("reservations",[])
    if not isinstance(reservations,list):fail(400,"malformed_request","reservations must be an array")
    for b in reservations:
        if not isinstance(b,dict): fail(400,"malformed_request","Invalid reservation object")
        user=c.execute("SELECT id FROM users WHERE id=?",(b.get("user_id"),)).fetchone()
        rest=c.execute("SELECT * FROM restaurants WHERE id=?",(b.get("restaurant_id"),)).fetchone()
        if not user or not rest: fail(422,"validation_failed","Invalid reservation fixture")
        if "table_id" in b and "table_ids" in b:fail(422,"validation_failed","Use either table_id or table_ids")
        raw_ids=[b.get("table_id")] if "table_id" in b else b.get("table_ids")
        if not isinstance(raw_ids,list) or not raw_ids or not all(string(x) for x in raw_ids):fail(422,"validation_failed","Invalid reservation fixture tables")
        table_ids,capacity=normalize_table_ids(rest,raw_ids,fixture=True)
        if not all(string(b.get(k)) for k in ("id","reference","starts_at_local")) or len(b.get("id",""))>64 or not string(b.get("reference")) or not 6<=len(b["reference"])<=12 or not re.fullmatch(r"[A-Z0-9]+",b["reference"]) or not integer(b.get("party_size")) or b["party_size"]<1 or b["party_size"]>capacity:
            fail(422,"validation_failed","Invalid reservation fixture")
        start=local_instant(b["starts_at_local"],rest["timezone"])
        end=local_end(start,rest["reservation_duration_minutes"])
        status=b.get("status","confirmed")
        if status not in ("confirmed","cancelled"):fail(422,"validation_failed","Invalid reservation status")
        table_id=table_ids[0] if len(table_ids)==1 else None
        start_iso=b.get("starts_at") or start.isoformat()
        end_iso=b.get("ends_at") or local_end(start,rest["reservation_duration_minutes"]).isoformat()
        created_at=b.get("created_at") or now()
        c.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(b["id"],b["reference"],b["user_id"],b["restaurant_id"],table_id,canon(table_ids),b["party_size"],status,b["starts_at_local"],start_iso,end_iso,created_at))

class Handler(BaseHTTPRequestHandler):
    server_version="Tablekeeper/1"
    def log_message(self,*args): pass
    def send_error(self,code,message=None,explain=None):
        status=404 if code==501 else code
        error_code={400:"malformed_request",404:"not_found"}.get(status,"internal_error")
        self.send_json(status,{"error":{"code":error_code,"message":message or "Request could not be handled"}})
    def send_json(self,status,data):
        raw=json.dumps(data,separators=(",",":"),ensure_ascii=False).encode()
        self._last_json=(status,data)
        self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
    def send_empty(self,status):
        self.send_response(status);self.send_header("Content-Length","0");self.end_headers()
    def send_asset(self,filename,content_type):
        try:
            with open(os.path.join(os.path.dirname(__file__),"static",filename),"rb") as f:data=f.read()
        except OSError:
            fail(404,"not_found","Page asset not found")
        self.send_response(200);self.send_header("Content-Type",content_type);self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def body(self):
        try:
            n=int(self.headers.get("Content-Length","0")); raw=self.rfile.read(n); return obj(json.loads(raw.decode("utf-8")))
        except ApiError: raise
        except Exception: fail(400,"malformed_request","Malformed JSON request")
    def auth(self,c,public=False):
        if public:return None
        a=self.headers.get("Authorization","")
        if not a.startswith("Bearer "): fail(401,"unauthenticated","Bearer token required")
        row=c.execute("SELECT user_id FROM tokens WHERE token=?",(a[7:],)).fetchone()
        if not row: fail(401,"unauthenticated","Invalid bearer token")
        return row["user_id"]
    def dispatch(self):
        path=self.path.split("?",1)[0]
        method=self.command
        mutating=(method=="POST" and path in ("/_test/reset","/_test/import","/auth/signup","/reservations","/reservation-moves")) or (method=="PATCH") or (method=="POST" and path.endswith("/cancel"))
        with (LOCK if mutating else nullcontext()):
            c=connect()
            try:
                if method=="GET" and path=="/health": return self.send_json(200,{"status":"ok"})
                if method=="GET" and path in ("/","/signup","/login","/lookup"):
                    return self.send_asset("index.html","text/html; charset=utf-8")
                if method=="GET" and path=="/static/app.css":return self.send_asset("app.css","text/css; charset=utf-8")
                if method=="GET" and path=="/static/app.js":return self.send_asset("app.js","text/javascript; charset=utf-8")
                if method=="POST" and path=="/_test/reset":
                    c.execute("BEGIN IMMEDIATE")
                    for t in ("idem","reservations","tokens","users","restaurants"): c.execute("DELETE FROM "+t)
                    try:seed(c,self.body())
                    except Exception:
                        c.rollback();raise
                    c.commit();return self.send_empty(204)
                if method=="GET" and path=="/_test/export": return self.send_json(200,export_state(c))
                if method=="POST" and path=="/_test/import":
                    data=self.body(); import_state(c,data);return self.send_empty(204)
                if method=="POST" and path in ("/auth/signup","/auth/login"):
                    b=self.body();return self.auth_route(c,path,b)
                if method=="GET" and path=="/restaurants": return self.send_json(200,{"restaurants":[{"id":r["id"],"name":r["name"],"timezone":r["timezone"]} for r in c.execute("SELECT * FROM restaurants ORDER BY rowid")]})
                if method=="GET" and path.startswith("/restaurants/"):
                    rid=path.split("/")[-1]
                    if len(rid)>64: fail(422,"validation_failed","ID is too long")
                    r=c.execute("SELECT * FROM restaurants WHERE id=?",(rid,)).fetchone()
                    if not r: fail(404,"not_found","Restaurant not found")
                    return self.send_json(200,restaurant_view(r))
                if method=="GET" and path=="/availability": return self.availability(c,None)
                uid=self.auth(c)
                if method=="POST" and path=="/reservations":
                    b=self.body();return self.idempotent(c,uid,method,path,b,lambda:self.create_res(c,uid,b))
                if method=="POST" and path=="/reservation-moves":
                    b=self.body();return self.idempotent(c,uid,method,path,b,lambda:self.moves(c,uid,b))
                if method=="GET" and path=="/reservations": return self.send_json(200,{"reservations":[self.resview(r) for r in c.execute("SELECT * FROM reservations WHERE user_id=? ORDER BY starts_at DESC",(uid,))]})
                m=re.fullmatch(r"/reservations/([^/]+)(?:/(cancel))?",path)
                if m:
                    ref,action=m.groups();r=c.execute("SELECT * FROM reservations WHERE reference=? AND user_id=?",(ref,uid)).fetchone()
                    if not r: fail(404,"not_found","Reservation not found")
                    if method=="GET" and not action:return self.send_json(200,self.resview(r))
                    if method=="POST" and action=="cancel":return self.cancel(c,uid,r)
                    if method=="PATCH" and not action:return self.amend(c,uid,r,self.body())
                fail(404,"not_found","Not found")
            finally:c.close()
    def do_GET(self): self.run_dispatch()
    def do_POST(self): self.run_dispatch()
    def do_PATCH(self): self.run_dispatch()
    def run_dispatch(self):
        try:self.dispatch()
        except ApiError as e:self.send_json(e.status,{"error":{"code":e.code,"message":e.message}})
        except sqlite3.IntegrityError:self.send_json(422,{"error":{"code":"validation_failed","message":"Conflicting fixture or resource data"}})
        except Exception:self.send_json(500,{"error":{"code":"internal_error","message":"Internal server error"}})
    def auth_route(self,c,path,b):
        email,password,name=b.get("email"),b.get("password"),b.get("display_name")
        required=("email","password","display_name") if path.endswith("signup") else ("email","password")
        if any(k not in b for k in required): fail(422,"validation_failed","Required credentials are missing")
        if not all(string(b.get(k)) for k in required): fail(400,"malformed_request","Invalid credentials field types")
        if path.endswith("signup"):
            if not email_valid(email): fail(422,"validation_failed","Invalid email")
            if len(password)<8: fail(422,"validation_failed","Password must contain at least 8 characters")
            if c.execute("SELECT 1 FROM users WHERE email=?",(email,)).fetchone(): fail(409,"email_taken","Email is already registered")
            uid=uuid.uuid4().hex;c.execute("INSERT INTO users VALUES(?,?,?,?)",(uid,email,password_hash(password),name))
        else:
            u=c.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
            if not u or not password_ok(password,u["password"]): fail(401,"unauthenticated","Invalid email or password")
            uid=u["id"];name=u["display_name"]
        token=secrets.token_urlsafe(32);c.execute("INSERT INTO tokens VALUES(?,?)",(token,uid));c.commit()
        return self.send_json(201 if path.endswith("signup") else 200,{"user_id":uid,"display_name":name,"token":token})
    def idempotent(self,c,uid,method,path,b,fn):
        key=self.headers.get("Idempotency-Key")
        if key is None or key=="": fail(400,"missing_idempotency_key","Idempotency-Key is required")
        if not 1<=len(key)<=255: fail(422,"validation_failed","Idempotency-Key length must be 1 to 255")
        body=canon(b);row=c.execute("SELECT * FROM idem WHERE user_id=? AND ikey=?",(uid,key)).fetchone()
        if row:
            if row["method"]!=method or row["path"]!=path or row["body"]!=body: fail(409,"idempotency_key_reuse","Idempotency key was used for a different request")
            return self.send_raw_json(200,row["response"])
        try:
            c.execute("BEGIN IMMEDIATE")
            # Recheck under write lock.
            row=c.execute("SELECT * FROM idem WHERE user_id=? AND ikey=?",(uid,key)).fetchone()
            if row:
                if row["method"]!=method or row["path"]!=path or row["body"]!=body: fail(409,"idempotency_key_reuse","Idempotency key was used for a different request")
                c.commit();return self.send_raw_json(200,row["response"])
            self._last_json=None
            result=fn()
            status,response=self._last_json
            c.execute("INSERT INTO idem VALUES(?,?,?,?,?,?,?)",(uid,key,method,path,body,status,json.dumps(response,separators=(",",":"),ensure_ascii=False)))
            c.commit()
            return result
        except ApiError:
            c.rollback();raise
        except Exception:
            c.rollback();raise
    def restaurant(self,c,rid):
        r=c.execute("SELECT * FROM restaurants WHERE id=?",(rid,)).fetchone()
        if not r:fail(404,"not_found","Restaurant not found")
        return r
    def availability(self,c,uid):
        from urllib.parse import urlsplit,parse_qs
        q=parse_qs(urlsplit(self.path).query)
        rid=q.get("restaurant_id",[None])[0];date=q.get("date",[None])[0];ps=q.get("party_size",[None])[0]
        if rid is None or date is None or ps is None: fail(422,"validation_failed","restaurant_id, date and party_size are required")
        if len(rid)>64: fail(422,"validation_failed","ID is too long")
        if not re.fullmatch(r"[0-9]+",ps):fail(422,"validation_failed","party_size must be an integer")
        party=int(ps)
        if party<1:fail(422,"validation_failed","party_size must be positive")
        try:
            parsed_date=dt.date.fromisoformat(date)
            if parsed_date.isoformat()!=date:raise ValueError()
        except Exception: fail(422,"validation_failed","date must be YYYY-MM-DD")
        r=self.restaurant(c,rid);hours=hours_map(json.loads(r["opening_hours"]));tables=json.loads(r["tables_json"]);zone=ZoneInfo(r["timezone"])
        wd=dt.date.fromisoformat(date).strftime("%a").lower()[:3]
        h=hours.get(wd)
        slots=[]
        if h:
            op,cl=h.get("opens"),h.get("closes")
            try:o=dt.datetime.strptime(op,"%H:%M").time();z=dt.datetime.strptime(cl,"%H:%M").time()
            except Exception:fail(422,"validation_failed","Invalid opening hours")
            cur=dt.datetime.combine(dt.date.fromisoformat(date),o);close=dt.datetime.combine(dt.date.fromisoformat(date),z)
            open_min=o.hour*60+o.minute
            close_aware=dt.datetime.combine(dt.date.fromisoformat(date),z,zone)
            while cur<close:
                local=cur.strftime("%Y-%m-%dT%H:%M")
                # Exclude skipped wall-clock instants; keep first repeated instant only.
                try:start=local_instant(local,r["timezone"])
                except ApiError:cur+=dt.timedelta(minutes=r["slot_minutes"]);continue
                end=local_end(start,r["reservation_duration_minutes"])
                if end.astimezone(dt.timezone.utc)>close_aware.astimezone(dt.timezone.utc): break
                free=[];options=[]
                for t in tables:
                    if not integer(t.get("capacity")) or t["capacity"]<party:continue
                    if not occupied(c,rid,[t["id"]],start,end):
                        free.append(t["id"]);options.append({"table_ids":[t["id"]],"capacity":t["capacity"]})
                for pair in json.loads(r["combinable"] or "[]"):
                    by_id={t["id"]:t for t in tables}
                    capacity=sum(by_id[table_id]["capacity"] for table_id in pair)
                    if capacity>=party and not occupied(c,rid,pair,start,end):
                        options.append({"table_ids":list(pair),"capacity":capacity})
                slots.append({"starts_at_local":local,"starts_at":start.isoformat(),"available_table_ids":free,"available_options":options})
                cur+=dt.timedelta(minutes=r["slot_minutes"])
        return self.send_json(200,{"restaurant_id":rid,"date":date,"timezone":r["timezone"],"slots":slots})
    def validate_slot(self,c,uid,b,old=None,check_occupancy=True):
        rid=b.get("restaurant_id",old["restaurant_id"] if old else None)
        if old and rid!=old["restaurant_id"]:fail(422,"validation_failed","Reservation restaurant cannot change")
        if old is None and "restaurant_id" not in b: fail(422,"validation_failed","restaurant_id is required")
        if not string(rid): fail(400,"malformed_request","restaurant_id must be a string")
        if len(rid)>64: fail(422,"validation_failed","ID is too long")
        r=self.restaurant(c,rid)
        if "table_id" in b and "table_ids" in b:fail(422,"validation_failed","Use either table_id or table_ids")
        if "table_ids" in b: raw_ids=b["table_ids"]
        elif "table_id" in b: raw_ids=[b["table_id"]]
        elif old is not None: raw_ids=row_table_ids(old)
        else: fail(422,"validation_failed","table_id or table_ids is required")
        if not isinstance(raw_ids,list):fail(400,"malformed_request","table_ids must be an array")
        table_ids,capacity=normalize_table_ids(r,raw_ids)
        local=b.get("starts_at_local",old["starts_at_local"] if old else None)
        if old is None and "starts_at_local" not in b:fail(422,"validation_failed","starts_at_local is required")
        if not string(local):fail(400,"malformed_request","starts_at_local must be a string")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",local):
            fail(422,"validation_failed","starts_at_local must be YYYY-MM-DDTHH:MM")
        start=local_instant(local,r["timezone"])
        party=b.get("party_size",old["party_size"] if old else None)
        if not integer(party) or party<1:fail(422,"validation_failed","party_size must be a positive integer")
        if party>capacity:fail(422,"party_exceeds_capacity","Party exceeds table capacity")
        hours=hours_map(json.loads(r["opening_hours"]));wd=start.strftime("%a").lower()[:3];h=hours.get(wd)
        if not h:fail(422,"outside_opening_hours","Restaurant is closed")
        opening=dt.datetime.combine(start.date(),dt.datetime.strptime(h["opens"],"%H:%M").time(),start.tzinfo)
        close=dt.datetime.combine(start.date(),dt.datetime.strptime(h["closes"],"%H:%M").time(),start.tzinfo)
        if start<opening or start.astimezone(dt.timezone.utc)+dt.timedelta(minutes=r["reservation_duration_minutes"])>close.astimezone(dt.timezone.utc):fail(422,"outside_opening_hours","Reservation is outside opening hours")
        open_min=dt.datetime.strptime(h["opens"],"%H:%M").hour*60+dt.datetime.strptime(h["opens"],"%H:%M").minute
        if (start.hour*60+start.minute-open_min)%r["slot_minutes"]!=0:fail(422,"not_on_slot_grid","Start time is not on the slot grid")
        end=local_end(start,r["reservation_duration_minutes"])
        if check_occupancy:
            if occupied(c,rid,table_ids,start,end,old["id"] if old else None):fail(409,"table_unavailable","Table is unavailable")
        return r,table_ids,party,local,start,end
    def create_res(self,c,uid,b):
        r,tids,p,local,start,end=self.validate_slot(c,uid,b)
        rid=uuid.uuid4().hex;ref=secrets.token_hex(4).upper()
        while c.execute("SELECT 1 FROM reservations WHERE reference=?",(ref,)).fetchone():ref=secrets.token_hex(4).upper()
        tid=tids[0] if len(tids)==1 else None
        row=(rid,ref,uid,r["id"],tid,canon(tids),p,"confirmed",local,start.isoformat(),end.isoformat(),now())
        c.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",row)
        return self.send_json(201,self.resview(dict(zip(("id","reference","user_id","restaurant_id","table_id","table_ids_json","party_size","status","starts_at_local","starts_at","ends_at","created_at"),row))))
    def resview(self,r):
        value={"reservation_id":r["reservation_id"] if "reservation_id" in r else r["id"],**{k:r[k] for k in ("reference","restaurant_id","party_size","status","starts_at_local","starts_at","ends_at","created_at")}}
        tids=row_table_ids(r);value["table_ids"]=tids
        if len(tids)==1:value["table_id"]=tids[0]
        return value
    def cancel(self,c,uid,r):
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM reservations WHERE id=? AND user_id=?",(r["id"],uid)).fetchone()
        if not r:fail(404,"not_found","Reservation not found")
        if r["status"]=="cancelled":return self.send_json(200,self.resview(r))
        rest=self.restaurant(c,r["restaurant_id"])
        if instant(r["starts_at"])-dt.datetime.now(dt.timezone.utc)<=dt.timedelta(minutes=rest["cancellation_cutoff_minutes"]):fail(409,"cutoff_passed","Cancellation cutoff has passed")
        c.execute("UPDATE reservations SET status='cancelled' WHERE id=?",(r["id"],))
        c.commit()
        nr=c.execute("SELECT * FROM reservations WHERE id=?",(r["id"],)).fetchone();return self.send_json(200,self.resview(nr))
    def amend(self,c,uid,r,b):
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT * FROM reservations WHERE id=? AND user_id=?",(r["id"],uid)).fetchone()
        if not r:fail(404,"not_found","Reservation not found")
        result=self.apply_amend(c,uid,r,b);c.commit()
        return self.send_json(200,result)
    def apply_amend(self,c,uid,r,b):
        if r["status"]=="cancelled":fail(409,"reservation_cancelled","Reservation is cancelled")
        rest=self.restaurant(c,r["restaurant_id"])
        if instant(r["starts_at"])-dt.datetime.now(dt.timezone.utc)<=dt.timedelta(minutes=rest["cancellation_cutoff_minutes"]):fail(409,"cutoff_passed","Amendment cutoff has passed")
        merged={"restaurant_id":r["restaurant_id"]}
        if "table_id" in b:merged["table_id"]=b["table_id"]
        elif "table_ids" in b:merged["table_ids"]=b["table_ids"]
        else:merged["table_ids"]=row_table_ids(r)
        merged["starts_at_local"]=b.get("starts_at_local",r["starts_at_local"])
        merged["party_size"]=b.get("party_size",r["party_size"])
        nr,tids,p,local,start,end=self.validate_slot(c,uid,merged,r)
        tid=tids[0] if len(tids)==1 else None
        c.execute("UPDATE reservations SET table_id=?,table_ids_json=?,party_size=?,starts_at_local=?,starts_at=?,ends_at=? WHERE id=?",(tid,canon(tids),p,local,start.isoformat(),end.isoformat(),r["id"]))
        return self.resview(c.execute("SELECT * FROM reservations WHERE id=?",(r["id"],)).fetchone())
    def moves(self,c,uid,b):
        moves=b.get("moves")
        if not isinstance(moves,list) or not 1<=len(moves)<=8:fail(422,"validation_failed","moves must contain 1 to 8 items")
        refs=[];rows=[]
        for m in moves:
            if not isinstance(m,dict) or not string(m.get("reference")):fail(422,"validation_failed","Each move requires a reference")
            if m["reference"] in refs:fail(422,"validation_failed","Duplicate reference")
            refs.append(m["reference"])
            row=c.execute("SELECT * FROM reservations WHERE reference=? AND user_id=?",(m["reference"],uid)).fetchone()
            if not row:fail(404,"not_found","Reservation not found")
            rows.append((row,m))
        if len({r["restaurant_id"] for r,m in rows})!=1:fail(422,"validation_failed","All reservations must be in one restaurant")
        prepared=[];rest=self.restaurant(c,rows[0][0]["restaurant_id"])
        # Validate domain and cutoff precedence before occupancy, then check all final intervals jointly.
        for row,m in rows:
            if row["status"]=="cancelled":fail(409,"reservation_cancelled","Reservation is cancelled")
            if instant(row["starts_at"])-dt.datetime.now(dt.timezone.utc)<=dt.timedelta(minutes=rest["cancellation_cutoff_minutes"]):fail(409,"cutoff_passed","Amendment cutoff has passed")
            merged={"restaurant_id":row["restaurant_id"]}
            if "table_id" in m and "table_ids" in m:fail(422,"validation_failed","Use either table_id or table_ids")
            if "table_id" in m:merged["table_id"]=m["table_id"]
            elif "table_ids" in m:merged["table_ids"]=m["table_ids"]
            else:merged["table_ids"]=row_table_ids(row)
            merged["starts_at_local"]=m.get("starts_at_local",row["starts_at_local"])
            merged["party_size"]=m.get("party_size",row["party_size"])
            r,tids,p,local,start,end=self.validate_slot(c,uid,merged,row,check_occupancy=False)
            prepared.append((row,tids,p,local,start,end))
        # Validate all occupancy against the final set, ignoring listed old occupancy.
        ids={x[0]["id"] for x in prepared}
        intervals=[]
        for row,tids,p,local,start,end in prepared:
            for x in c.execute("SELECT * FROM reservations WHERE restaurant_id=? AND status='confirmed'",(row["restaurant_id"],)):
                if x["id"] in ids or not set(tids).intersection(row_table_ids(x)):continue
                if start.astimezone(dt.timezone.utc)<instant(x["ends_at"]) and instant(x["starts_at"])<end:fail(409,"table_unavailable","Table is unavailable")
            for _,other_ids,_,_,other_start,other_end in intervals:
                if set(tids).intersection(other_ids) and start.astimezone(dt.timezone.utc)<other_end.astimezone(dt.timezone.utc) and other_start.astimezone(dt.timezone.utc)<end.astimezone(dt.timezone.utc):
                    fail(409,"table_unavailable","Moves overlap")
            intervals.append((row,tids,p,local,start,end))
        result=[]
        for row,tids,p,local,start,end in prepared:
            tid=tids[0] if len(tids)==1 else None
            c.execute("UPDATE reservations SET table_id=?,table_ids_json=?,party_size=?,starts_at_local=?,starts_at=?,ends_at=? WHERE id=?",(tid,canon(tids),p,local,start.isoformat(),end.isoformat(),row["id"]))
            result.append(self.resview(c.execute("SELECT * FROM reservations WHERE id=?",(row["id"],)).fetchone()))
        return self.send_json(201,{"reservations":result})
    def send_raw_json(self,status,raw):
        data=raw.encode()
        self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)

def restaurant_view(r):
    return {"id":r["id"],"name":r["name"],"timezone":r["timezone"],"slot_minutes":r["slot_minutes"],"reservation_duration_minutes":r["reservation_duration_minutes"],"cancellation_cutoff_minutes":r["cancellation_cutoff_minutes"],"opening_hours":hours_array(json.loads(r["opening_hours"])),"tables":json.loads(r["tables_json"]),"combinable":json.loads(r["combinable"] or "[]")}

def export_state(c):
    state={}
    for table in ("users","tokens","restaurants","reservations","idem"):
        state[table]=[dict(r) for r in c.execute("SELECT * FROM "+table)]
    return {"track":"tablekeeper","format_version":1,"state":state}

def import_state(c,data):
    if not isinstance(data,dict) or data.get("track")!="tablekeeper" or data.get("format_version")!=1 or not isinstance(data.get("state"),dict):fail(422,"validation_failed","Invalid export")
    s=data["state"];tables=("users","tokens","restaurants","reservations","idem")
    if any(not isinstance(s.get(t),list) for t in tables):fail(422,"validation_failed","Invalid export state")
    try:
        c.execute("BEGIN IMMEDIATE")
        for t in ("idem","reservations","tokens","users","restaurants"):c.execute("DELETE FROM "+t)
        for t in tables:
            cols=[x[1] for x in c.execute("PRAGMA table_info("+t+")")]
            for row in s[t]:
                if not isinstance(row,dict):raise ValueError("schema")
                row=dict(row)
                if t=="restaurants" and "combinable" in cols and "combinable" not in row:row["combinable"]="[]"
                if t=="reservations" and "table_ids_json" in cols and "table_ids_json" not in row:
                    row["table_ids_json"]=canon([row["table_id"]] if row.get("table_id") else [])
                if set(row)!=set(cols):raise ValueError("schema")
                c.execute("INSERT INTO "+t+" VALUES("+",".join("?" for _ in cols)+")",[row[k] for k in cols])
        c.commit()
    except Exception:
        c.rollback();fail(422,"validation_failed","Invalid export state")

class HttpServer(ThreadingHTTPServer):
    request_queue_size=128
    daemon_threads=True

if __name__=="__main__":
    c=connect();schema(c);c.close()
    HttpServer(("0.0.0.0",int(os.environ.get("PORT","8080"))),Handler).serve_forever()
