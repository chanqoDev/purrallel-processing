#!/usr/bin/env python3
"""Tablekeeper Stage 1: dependency-free, single-process HTTP service."""
import base64
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

DB = os.environ.get("TABLEKEEPER_DB", "/tmp/tablekeeper.sqlite3")
LOCK = threading.RLock()

def connect():
    c=sqlite3.connect(DB, timeout=5, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE,password TEXT,display_name TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS restaurants(id TEXT PRIMARY KEY,name TEXT,timezone TEXT,slot_minutes INTEGER,reservation_duration_minutes INTEGER,cancellation_cutoff_minutes INTEGER,opening_hours TEXT,tables_json TEXT);
    CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,reference TEXT UNIQUE,user_id TEXT,restaurant_id TEXT,table_id TEXT,party_size INTEGER,status TEXT,starts_at_local TEXT,starts_at TEXT,ends_at TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS idem(user_id TEXT,ikey TEXT,method TEXT,path TEXT,body TEXT,status INTEGER,response TEXT,PRIMARY KEY(user_id,ikey));
    """)

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def canon(v):
    return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False)

def password_hash(p):
    salt=secrets.token_bytes(16)
    return "scrypt$"+base64.b64encode(salt).decode()+"$"+hashlib.scrypt(p.encode(),salt=salt,n=2**14,r=8,p=1).hex()

def password_ok(p, stored):
    try:
        _,s,h=stored.split("$"); salt=base64.b64decode(s)
        return hmac.compare_digest(hashlib.scrypt(p.encode(),salt=salt,n=2**14,r=8,p=1).hex(),h)
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

def seed(c,data):
    if not isinstance(data,dict): fail(400,"malformed_request","Expected a JSON object")
    users=data.get("users",[])
    restaurants=data.get("restaurants",[])
    if not isinstance(users,list) or not isinstance(restaurants,list): fail(422,"validation_failed","users and restaurants must be arrays")
    for u in users:
        if not isinstance(u,dict): fail(422,"validation_failed","Invalid user")
        if not all(string(u.get(k)) for k in ("id","email","password","display_name")): fail(400,"malformed_request","Invalid user field types")
        if len(u["id"])>64: fail(422,"validation_failed","ID is too long")
        if len(u["password"])<8 or not email_valid(u["email"]): fail(422,"validation_failed","Invalid user credentials")
        c.execute("INSERT INTO users VALUES(?,?,?,?)",(u["id"],u["email"],password_hash(u["password"]),u["display_name"]))
    for r in restaurants:
        if not isinstance(r,dict) or not all(string(r.get(k)) for k in ("id","name","timezone")): fail(400,"malformed_request","Invalid restaurant field types")
        if len(r["id"])>64: fail(422,"validation_failed","ID is too long")
        try: ZoneInfo(r["timezone"])
        except ZoneInfoNotFoundError: fail(422,"validation_failed","Invalid timezone")
        hours=r.get("opening_hours",[])
        tables=r.get("tables",[])
        if not isinstance(hours,list) or not isinstance(tables,list): fail(422,"validation_failed","Invalid restaurant configuration")
        hmap={}
        for h in hours:
            if not isinstance(h,dict) or not string(h.get("weekday")) or h["weekday"] not in ("mon","tue","wed","thu","fri","sat","sun") or not string(h.get("opens")) or not string(h.get("closes")):
                fail(422,"validation_failed","Invalid opening hours")
            hmap[h["weekday"]]={"opens":h["opens"],"closes":h["closes"]}
        for t in tables:
            if not isinstance(t,dict) or not all(string(t.get(k)) for k in ("id","label")) or not integer(t.get("capacity")):
                fail(400,"malformed_request","Invalid table field types")
            if len(t["id"])>64:fail(422,"validation_failed","ID is too long")
        try:
            slot=int(r.get("slot_minutes",30));duration=int(r.get("reservation_duration_minutes",90));cutoff=int(r.get("cancellation_cutoff_minutes",60))
        except Exception: fail(400,"malformed_request","Invalid restaurant configuration types")
        if min(slot,duration)<1 or cutoff<0: fail(422,"validation_failed","Invalid restaurant configuration")
        c.execute("INSERT INTO restaurants VALUES(?,?,?,?,?,?,?,?)",(r["id"],r["name"],r["timezone"],slot,duration,cutoff,canon(hmap),canon(tables)))
    reservations=data.get("reservations",[])
    if not isinstance(reservations,list):fail(400,"malformed_request","reservations must be an array")
    for b in reservations:
        if not isinstance(b,dict): fail(422,"validation_failed","Invalid reservation fixture")
        user=c.execute("SELECT id FROM users WHERE id=?",(b.get("user_id"),)).fetchone()
        rest=c.execute("SELECT * FROM restaurants WHERE id=?",(b.get("restaurant_id"),)).fetchone()
        if not user or not rest: fail(422,"validation_failed","Invalid reservation fixture")
        table=next((t for t in json.loads(rest["tables_json"]) if t.get("id")==b.get("table_id")),None)
        if table is None: fail(422,"validation_failed","Invalid reservation fixture")
        if not all(string(b.get(k)) for k in ("id","reference","starts_at_local")) or len(b.get("id",""))>64 or not string(b.get("reference")) or not 6<=len(b["reference"])<=12 or not re.fullmatch(r"[A-Z0-9]+",b["reference"]) or not integer(b.get("party_size")) or b["party_size"]<1:
            fail(422,"validation_failed","Invalid reservation fixture")
        start=local_instant(b["starts_at_local"],rest["timezone"])
        end=local_end(start,rest["reservation_duration_minutes"])
        status=b.get("status","confirmed")
        if status not in ("confirmed","cancelled"):fail(422,"validation_failed","Invalid reservation status")
        c.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?,?)",(b["id"],b["reference"],b["user_id"],b["restaurant_id"],b["table_id"],b["party_size"],status,b["starts_at_local"],start.isoformat(),end.isoformat(),b.get("created_at",now())))

class Handler(BaseHTTPRequestHandler):
    server_version="Tablekeeper/1"
    def log_message(self,*args): pass
    def send_json(self,status,data):
        raw=json.dumps(data,separators=(",",":"),ensure_ascii=False).encode()
        self._last_json=(status,data)
        self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
    def send_empty(self,status):
        self.send_response(status);self.send_header("Content-Length","0");self.end_headers()
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
        with LOCK:
            c=connect()
            try:
                schema(c)
                if method=="GET" and path=="/health": return self.send_json(200,{"status":"ok"})
                if method=="POST" and path=="/_test/reset":
                    c.execute("BEGIN IMMEDIATE")
                    for t in ("idem","reservations","tokens","users","restaurants"): c.execute("DELETE FROM "+t)
                    seed(c,self.body());c.commit();return self.send_empty(204)
                if method=="GET" and path=="/_test/export": return self.send_json(200,export_state(c))
                if method=="POST" and path=="/_test/import":
                    data=self.body(); import_state(c,data);return self.send_empty(204)
                if method=="POST" and path in ("/auth/signup","/auth/login"):
                    b=self.body();return self.auth_route(c,path,b)
                if method=="GET" and path=="/restaurants": return self.send_json(200,{"restaurants":[{"id":r["id"],"name":r["name"],"timezone":r["timezone"]} for r in c.execute("SELECT * FROM restaurants ORDER BY rowid")]})
                if method=="GET" and path.startswith("/restaurants/"):
                    r=c.execute("SELECT * FROM restaurants WHERE id=?",(path.split("/")[-1],)).fetchone()
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
        except Exception:self.send_json(500,{"error":{"code":"internal_error","message":"Internal server error"}})
    def auth_route(self,c,path,b):
        email,password,name=b.get("email"),b.get("password"),b.get("display_name")
        if not all(string(x) for x in (email,password)) or (path.endswith("signup") and not string(name)): fail(400,"malformed_request","Invalid credentials field types")
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
        if not re.fullmatch(r"[0-9]+",ps):fail(422,"validation_failed","party_size must be an integer")
        party=int(ps)
        if party<1:fail(422,"validation_failed","party_size must be positive")
        try:
            parsed_date=dt.date.fromisoformat(date)
            if parsed_date.isoformat()!=date:raise ValueError()
        except Exception: fail(422,"validation_failed","date must be YYYY-MM-DD")
        r=self.restaurant(c,rid);hours=json.loads(r["opening_hours"]);tables=json.loads(r["tables_json"]);zone=ZoneInfo(r["timezone"])
        wd=dt.date.fromisoformat(date).strftime("%a").lower()[:3]
        h=hours.get(wd)
        slots=[]
        if h:
            op,cl=h.get("opens"),h.get("closes")
            try:o=dt.datetime.strptime(op,"%H:%M").time();z=dt.datetime.strptime(cl,"%H:%M").time()
            except Exception:fail(422,"validation_failed","Invalid opening hours")
            cur=dt.datetime.combine(dt.date.fromisoformat(date),o);close=dt.datetime.combine(dt.date.fromisoformat(date),z)
            open_min=o.hour*60+o.minute
            while cur+dt.timedelta(minutes=r["reservation_duration_minutes"])<=close:
                local=cur.strftime("%Y-%m-%dT%H:%M")
                # Exclude skipped wall-clock instants; keep first repeated instant only.
                try:start=local_instant(local,r["timezone"])
                except ApiError:cur+=dt.timedelta(minutes=r["slot_minutes"]);continue
                end=local_end(start,r["reservation_duration_minutes"])
                free=[]
                booked=list(c.execute("SELECT starts_at,ends_at,table_id FROM reservations WHERE restaurant_id=? AND status='confirmed'",(rid,)))
                for t in tables:
                    if not integer(t.get("capacity")) or t["capacity"]<party:continue
                    occupied=any(b["table_id"]==t.get("id") and start.astimezone(dt.timezone.utc)<instant(b["ends_at"]) and instant(b["starts_at"])<end for b in booked)
                    if not occupied:free.append(t["id"])
                slots.append({"starts_at_local":local,"starts_at":start.isoformat(),"available_table_ids":free})
                cur+=dt.timedelta(minutes=r["slot_minutes"])
        return self.send_json(200,{"restaurant_id":rid,"date":date,"timezone":r["timezone"],"slots":slots})
    def validate_slot(self,c,uid,b,old=None,check_occupancy=True):
        rid=b.get("restaurant_id",old["restaurant_id"] if old else None)
        if old and rid!=old["restaurant_id"]:fail(422,"validation_failed","Reservation restaurant cannot change")
        r=self.restaurant(c,rid)
        table_id=b.get("table_id",old["table_id"] if old else None)
        table=next((t for t in json.loads(r["tables_json"]) if t.get("id")==table_id),None)
        if not table:fail(404,"not_found","Table not found")
        local=b.get("starts_at_local",old["starts_at_local"] if old else None)
        if not string(local):fail(422,"validation_failed","starts_at_local is required")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",local):
            fail(422,"validation_failed","starts_at_local must be YYYY-MM-DDTHH:MM")
        start=local_instant(local,r["timezone"])
        party=b.get("party_size",old["party_size"] if old else None)
        if not integer(party) or party<1:fail(422,"validation_failed","party_size must be a positive integer")
        if party>table.get("capacity",0):fail(422,"party_exceeds_capacity","Party exceeds table capacity")
        hours=json.loads(r["opening_hours"]);wd=start.strftime("%a").lower()[:3];h=hours.get(wd)
        if not h:fail(422,"outside_opening_hours","Restaurant is closed")
        opening=dt.datetime.combine(start.date(),dt.datetime.strptime(h["opens"],"%H:%M").time(),start.tzinfo)
        close=dt.datetime.combine(start.date(),dt.datetime.strptime(h["closes"],"%H:%M").time(),start.tzinfo)
        if start<opening or start.astimezone(dt.timezone.utc)+dt.timedelta(minutes=r["reservation_duration_minutes"])>close.astimezone(dt.timezone.utc):fail(422,"outside_opening_hours","Reservation is outside opening hours")
        open_min=dt.datetime.strptime(h["opens"],"%H:%M").hour*60+dt.datetime.strptime(h["opens"],"%H:%M").minute
        if (start.hour*60+start.minute-open_min)%r["slot_minutes"]!=0:fail(422,"not_on_slot_grid","Start time is not on the slot grid")
        end=local_end(start,r["reservation_duration_minutes"])
        if check_occupancy:
            for x in c.execute("SELECT * FROM reservations WHERE restaurant_id=? AND table_id=? AND status='confirmed'",(rid,table_id)):
                if old and x["id"]==old["id"]:continue
                if start.astimezone(dt.timezone.utc)<instant(x["ends_at"]) and instant(x["starts_at"])<end:fail(409,"table_unavailable","Table is unavailable")
        return r,table_id,party,local,start,end
    def create_res(self,c,uid,b):
        r,tid,p,local,start,end=self.validate_slot(c,uid,b)
        rid=uuid.uuid4().hex;ref=secrets.token_hex(4).upper()
        while c.execute("SELECT 1 FROM reservations WHERE reference=?",(ref,)).fetchone():ref=secrets.token_hex(4).upper()
        row=(rid,ref,uid,r["id"],tid,p,"confirmed",local,start.isoformat(),end.isoformat(),now())
        c.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?,?)",row)
        return self.send_json(201,self.resview(dict(zip(("id","reference","user_id","restaurant_id","table_id","party_size","status","starts_at_local","starts_at","ends_at","created_at"),row))))
    def resview(self,r):
        return {k:r[k] for k in ("reservation_id","reference","restaurant_id","table_id","party_size","status","starts_at_local","starts_at","ends_at","created_at")} if "reservation_id" in r else {"reservation_id":r["id"],**{k:r[k] for k in ("reference","restaurant_id","table_id","party_size","status","starts_at_local","starts_at","ends_at","created_at")}}
    def cancel(self,c,uid,r):
        if r["status"]=="cancelled":return self.send_json(200,self.resview(r))
        rest=self.restaurant(c,r["restaurant_id"])
        if instant(r["starts_at"])-dt.datetime.now(dt.timezone.utc)<=dt.timedelta(minutes=rest["cancellation_cutoff_minutes"]):fail(409,"cutoff_passed","Cancellation cutoff has passed")
        c.execute("UPDATE reservations SET status='cancelled' WHERE id=?",(r["id"],))
        c.commit()
        nr=c.execute("SELECT * FROM reservations WHERE id=?",(r["id"],)).fetchone();return self.send_json(200,self.resview(nr))
    def amend(self,c,uid,r,b):
        result=self.apply_amend(c,uid,r,b);c.commit()
        return self.send_json(200,result)
    def apply_amend(self,c,uid,r,b):
        if r["status"]=="cancelled":fail(409,"reservation_cancelled","Reservation is cancelled")
        rest=self.restaurant(c,r["restaurant_id"])
        if instant(r["starts_at"])-dt.datetime.now(dt.timezone.utc)<=dt.timedelta(minutes=rest["cancellation_cutoff_minutes"]):fail(409,"cutoff_passed","Amendment cutoff has passed")
        merged={k:b.get(k,r[k]) for k in ("restaurant_id","table_id","starts_at_local","party_size")}
        nr,tid,p,local,start,end=self.validate_slot(c,uid,merged,r)
        c.execute("UPDATE reservations SET table_id=?,party_size=?,starts_at_local=?,starts_at=?,ends_at=? WHERE id=?",(tid,p,local,start.isoformat(),end.isoformat(),r["id"]))
        return self.resview(c.execute("SELECT * FROM reservations WHERE id=?",(r["id"],)).fetchone())
    def moves(self,c,uid,b):
        moves=b.get("moves")
        if not isinstance(moves,list) or not 1<=len(moves)<=8:fail(422,"validation_failed","moves must contain 1 to 8 items")
        c.execute("BEGIN IMMEDIATE")
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
            merged={k:m.get(k,row[k]) for k in ("restaurant_id","table_id","starts_at_local","party_size")}
            r,tid,p,local,start,end=self.validate_slot(c,uid,merged,row,check_occupancy=False)
            prepared.append((row,tid,p,local,start,end))
        # Validate all occupancy against the final set, ignoring listed old occupancy.
        ids={x[0]["id"] for x in prepared}
        intervals=[]
        for row,tid,p,local,start,end in prepared:
            for x in c.execute("SELECT * FROM reservations WHERE restaurant_id=? AND table_id=? AND status='confirmed'",(row["restaurant_id"],tid)):
                if x["id"] in ids:continue
                if start.astimezone(dt.timezone.utc)<instant(x["ends_at"]) and instant(x["starts_at"])<end:fail(409,"table_unavailable","Table is unavailable")
            for _,ot,op,ol,os,oe in intervals:
                if tid==ot and start.astimezone(dt.timezone.utc)<oe.astimezone(dt.timezone.utc) and os.astimezone(dt.timezone.utc)<end:fail(409,"table_unavailable","Moves overlap")
            intervals.append((row,tid,p,local,start,end))
        result=[]
        for row,tid,p,local,start,end in prepared:
            c.execute("UPDATE reservations SET table_id=?,party_size=?,starts_at_local=?,starts_at=?,ends_at=? WHERE id=?",(tid,p,local,start.isoformat(),end.isoformat(),row["id"]))
            result.append(self.resview(c.execute("SELECT * FROM reservations WHERE id=?",(row["id"],)).fetchone()))
        c.commit()
        return self.send_json(201,{"reservations":result})
    def send_raw_json(self,status,raw):
        data=raw.encode()
        self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)

def restaurant_view(r):
    return {"id":r["id"],"name":r["name"],"timezone":r["timezone"],"slot_minutes":r["slot_minutes"],"reservation_duration_minutes":r["reservation_duration_minutes"],"cancellation_cutoff_minutes":r["cancellation_cutoff_minutes"],"opening_hours":json.loads(r["opening_hours"]),"tables":json.loads(r["tables_json"])}

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
                if not isinstance(row,dict) or set(row)!=set(cols):raise ValueError("schema")
                c.execute("INSERT INTO "+t+" VALUES("+",".join("?" for _ in cols)+")",[row[k] for k in cols])
        c.commit()
    except Exception:
        c.rollback();fail(422,"validation_failed","Invalid export state")

if __name__=="__main__":
    c=connect();schema(c);c.close()
    ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8080"))),Handler).serve_forever()
