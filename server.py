#!/usr/bin/env python3
"""Xdigitex Trade operations portal. Trading fails closed; Xdigitex Pay handles configured cash flows."""
import argparse, getpass, hashlib, hmac, json, os, re, secrets, sqlite3, sys, time, uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from xpay import XDigitexPay, XPayError
from marketdata import get_quotes, is_configured as market_data_configured

ROOT=Path(__file__).resolve().parent
DATA_DIR=Path(os.environ.get("DATA_DIR",ROOT/"data")).resolve()
DB_PATH=DATA_DIR/"xdigitex_trade.sqlite3"
HOST=os.environ.get("HOST","0.0.0.0")
PORT=int(os.environ.get("PORT","8080"))
COOKIE_SECURE=os.environ.get("COOKIE_SECURE","false").lower()=="true"
ROUNDS=310_000
MAX_CENTS=100_000_000
SUPPORTED_CURRENCIES={"KES":2,"USD":2,"CDF":2,"UGX":0,"XOF":0,"XAF":0,"RWF":0,"ZMW":2,"SLE":2,"USDT":6,"BTC":8,"ETH":8,"BNB":8}
CRYPTO_CURRENCIES={"USDT","BTC","ETH","BNB"}
MIN_WITHDRAWAL={"KES":1000,"CDF":100000,"UGX":500,"XOF":500,"XAF":500,"RWF":500,"ZMW":500,"SLE":500}
LOGIN_ATTEMPTS={}

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
@contextmanager
def db():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(DB_PATH,timeout=15,isolation_level=None); c.row_factory=sqlite3.Row
    try:
        c.execute("PRAGMA foreign_keys=ON");c.execute("PRAGMA journal_mode=WAL");c.execute("PRAGMA busy_timeout=15000")
        yield c
        if c.in_transaction:c.commit()
    except Exception:
        if c.in_transaction:c.rollback()
        raise
    finally:c.close()
def init_db():
    with db() as c:c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'investor' CHECK(role IN ('admin','investor')),kyc_status TEXT NOT NULL DEFAULT 'pending' CHECK(kyc_status IN ('pending','verified','rejected')),accepted_disclosures INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf_token TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS funding_requests(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL CHECK(kind IN ('deposit','withdrawal')),amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','paid','rejected')),method TEXT,customer_reference TEXT,destination TEXT,external_reference TEXT,reviewer_id TEXT REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,currency TEXT NOT NULL DEFAULT 'USD',gross_cents INTEGER NOT NULL DEFAULT 0,fee_cents INTEGER NOT NULL DEFAULT 0,provider_reference TEXT,provider_status TEXT,provider_data TEXT,pay_phone TEXT);
    CREATE INDEX IF NOT EXISTS funding_user_idx ON funding_requests(user_id,created_at DESC);
    CREATE TABLE IF NOT EXISTS ledger_entries(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),request_id TEXT UNIQUE REFERENCES funding_requests(id),entry_type TEXT NOT NULL CHECK(entry_type IN ('deposit_credit','withdrawal_debit')),delta_cents INTEGER NOT NULL CHECK(delta_cents!=0),currency TEXT NOT NULL DEFAULT 'USD',description TEXT NOT NULL,external_reference TEXT NOT NULL,created_by TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,previous_hash TEXT NOT NULL,entry_hash TEXT UNIQUE NOT NULL);
    CREATE INDEX IF NOT EXISTS ledger_user_idx ON ledger_entries(user_id,created_at DESC);
    CREATE TABLE IF NOT EXISTS audit_events(id TEXT PRIMARY KEY,actor_id TEXT REFERENCES users(id),action TEXT NOT NULL,target_type TEXT,target_id TEXT,summary TEXT NOT NULL,created_at TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT UNIQUE NOT NULL);
    CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_events(created_at DESC);
    ''')
    with db() as c:
        cols={r[1] for r in c.execute("PRAGMA table_info(funding_requests)")}
        additions={"currency":"TEXT NOT NULL DEFAULT 'USD'","gross_cents":"INTEGER NOT NULL DEFAULT 0","fee_cents":"INTEGER NOT NULL DEFAULT 0","provider_reference":"TEXT","provider_status":"TEXT","provider_data":"TEXT","pay_phone":"TEXT"}
        for name,definition in additions.items():
            if name not in cols:c.execute(f"ALTER TABLE funding_requests ADD COLUMN {name} {definition}")
def pw_hash(p,salt=None):
    salt=salt or secrets.token_bytes(16); d=hashlib.pbkdf2_hmac("sha256",p.encode(),salt,ROUNDS)
    return f"pbkdf2_sha256${ROUNDS}${salt.hex()}${d.hex()}"
def pw_ok(p,e):
    try:
        kind,n,s,d=e.split("$");return kind=="pbkdf2_sha256" and hmac.compare_digest(hashlib.pbkdf2_hmac("sha256",p.encode(),bytes.fromhex(s),int(n)).hex(),d)
    except Exception:return False
def cents(v,currency="USD"):
    decimals=SUPPORTED_CURRENCIES.get(currency)
    if decimals is None:raise ValueError("Unsupported currency.")
    s=str(v).strip() if isinstance(v,(str,int,float)) and not isinstance(v,bool) else ""
    pattern=r"\d{1,7}"+(rf"(?:\.\d{{1,{decimals}}})?" if decimals else "")
    if not re.fullmatch(pattern,s):raise ValueError(f"Enter a valid {currency} amount.")
    a,_,b=s.partition(".");scale=10**decimals;n=int(a)*scale+(int((b+"0"*decimals)[:decimals]) if decimals else 0)
    if n<=0 or n>MAX_CENTS*scale//100:raise ValueError("Amount is outside the allowed range.")
    return n
def cash(n,currency="USD"):
    decimals=SUPPORTED_CURRENCIES.get(currency,2);return round(n/(10**decimals),decimals)
def api_minor(v,currency):
    try:return int((Decimal(str(v))* (10**SUPPORTED_CURRENCIES[currency])).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
    except (InvalidOperation,KeyError,TypeError):raise ValueError("Provider returned an invalid amount.")
def api_amount(n,currency):return cash(n,currency)
def currency_totals(rows):
    out={}
    for r in rows:out[r["currency"]]=cash(r["n"],r["currency"])
    return out
def grouped_totals(c,sql,args=()):return currency_totals(c.execute(sql,args).fetchall())
def money_map(m):return " · ".join(f"{k} {v:,.{SUPPORTED_CURRENCIES.get(k,2)}f}" for k,v in sorted(m.items())) or "—"
def pay_api():return XDigitexPay()
def integration_status():
    configured=pay_api().ready
    market_configured=market_data_configured()
    return [{"name":"market_data","label":"Read-only FX market prices","connected":market_configured,"detail":"Twelve Data key configured; prices are references only and do not execute trades." if market_configured else "Optional: set TWELVE_DATA_API_KEY on the server to show read-only FX prices."},{"name":"pool_execution","label":"Xdigitex internal pool","connected":False,"detail":"Internal order execution, member units, and settlement rules are not implemented; trading stays disabled."},{"name":"xdigitex_pay","label":"Xdigitex Pay deposits and withdrawals","connected":configured,"detail":"Merchant key and public HTTPS callback are configured; verify connectivity with a real provider request." if configured else "Set XDIGITEX_PAY_API_KEY and PUBLIC_BASE_URL on the server."},{"name":"identity","label":"Identity verification provider","connected":False,"detail":"Manual review only; no KYC vendor connected."}]
def public_url(path):
    base=os.environ.get("PUBLIC_BASE_URL","").strip().rstrip("/")
    if not base:raise XPayError("PUBLIC_BASE_URL must be configured so Xdigitex Pay can call the product webhook.")
    if not base.startswith("https://") and not (base.startswith("http://localhost") or base.startswith("http://127.0.0.1")):
        raise XPayError("PUBLIC_BASE_URL must use HTTPS in production.")
    return base+path
def provider_url(value):
    from urllib.parse import urlsplit
    p=urlsplit(str(value or ""))
    if p.scheme!="https" or not p.hostname:raise XPayError("Xdigitex Pay did not return a secure checkout URL.")
    return p.geturl()
def public_user(u):return {k:u[k] for k in ("id","name","email","role","kyc_status","created_at")}
def chain_hash(previous,*parts):return hashlib.sha256("|".join([previous,*map(str,parts)]).encode()).hexdigest()
def audit(c,actor,action,target_type,target_id,summary):
    prev=c.execute("SELECT event_hash FROM audit_events ORDER BY rowid DESC LIMIT 1").fetchone();ph=prev[0] if prev else "GENESIS";i=str(uuid.uuid4());t=now()
    c.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?)",(i,actor,action,target_type,target_id,summary,t,ph,chain_hash(ph,i,actor or "SYSTEM",action,target_type or "",target_id or "",summary,t)))
def add_entry(c,u,r,kind,delta,desc,ref,actor,currency="USD"):
    prev=c.execute("SELECT entry_hash FROM ledger_entries ORDER BY rowid DESC LIMIT 1").fetchone();ph=prev[0] if prev else "GENESIS";i=str(uuid.uuid4());t=now()
    eh=chain_hash(ph,i,u,r,kind,delta,currency,desc,ref,actor,t)
    c.execute("INSERT INTO ledger_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(i,u,r,kind,delta,currency,desc,ref,actor,t,ph,eh))
def bootstrap_admin(email,name,password):
    email=email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email):raise ValueError("Use a valid email address.")
    if len(password)<16:raise ValueError("Administrator password must be at least 16 characters.")
    init_db()
    with db() as c:
        u=c.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone()
        if u:
            uid=u[0];c.execute("UPDATE users SET name=?,password_hash=?,role='admin',kyc_status='verified' WHERE id=?",(name,password_hash(password),uid));audit(c,uid,"admin.bootstrap.reset","user",uid,"Administrator credentials reset locally.")
        else:
            uid=str(uuid.uuid4());c.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",(uid,name,email,pw_hash(password),"admin","verified",1,now()));audit(c,uid,"admin.bootstrap.created","user",uid,"Initial administrator created locally.")

class Handler(BaseHTTPRequestHandler):
    server_version="XdigitexTrade/0.1"
    def log_message(self,fmt,*args):print(f"[{now()}] {self.address_string()} {fmt%args}")
    def reply(self,code,data,cookies=()):
        raw=json.dumps(data,separators=(",",":")).encode();self.send_response(code);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.send_header("Cache-Control","no-store");self.send_header("X-Content-Type-Options","nosniff");self.send_header("Referrer-Policy","same-origin");self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        for cookie in cookies:self.send_header("Set-Cookie",cookie)
        self.end_headers();self.wfile.write(raw)
    def fail(self,code,msg):self.reply(code,{"error":msg})
    def body(self):
        try:
            n=int(self.headers.get("Content-Length","0"))
            if not 1<=n<=16384:raise ValueError
            obj=json.loads(self.rfile.read(n));
            if not isinstance(obj,dict):raise ValueError
            return obj
        except Exception:raise ValueError("Request body must be a JSON object under 16 KB.")
    def jar(self):
        c=SimpleCookie()
        try:c.load(self.headers.get("Cookie",""))
        except Exception:pass
        return c
    def session(self):
        m=self.jar().get("xd_session")
        if not m:return None,None
        th=hashlib.sha256(m.value.encode()).hexdigest()
        with db() as c:u=c.execute("SELECT u.*,s.csrf_token,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",(th,)).fetchone()
        if not u or u["expires_at"]<now():return None,None
        return u,u["csrf_token"]
    def auth(self,admin=False):
        u,csrf=self.session()
        if not u:self.fail(401,"Sign in required.");return None,None
        if admin and u["role"]!="admin":self.fail(403,"Administrator access required.");return None,None
        return u,csrf
    def csrf(self,t):
        if not t or not hmac.compare_digest(t,self.headers.get("X-CSRF-Token","")):self.fail(403,"Security token expired. Sign in again.");return False
        return True
    def do_GET(self):
        p=urlparse(self.path).path
        if p.startswith("/api/"):
            try:self.get_api(p)
            except Exception as e:print("GET:",repr(e),file=sys.stderr);self.fail(500,"An internal error occurred.")
            return
        files={"/":"index.html","/index.html":"index.html","/styles.css":"styles.css","/app.js":"app.js"}
        if p=="/favicon.ico":self.send_response(204);self.end_headers();return
        name=files.get(p)
        if not name:self.fail(404,"Not found.");return
        path=ROOT/name
        try:raw=path.read_bytes()
        except OSError:self.fail(404,"Product asset is missing.");return
        mime={"index.html":"text/html; charset=utf-8","styles.css":"text/css; charset=utf-8","app.js":"text/javascript; charset=utf-8"}[name]
        self.send_response(200);self.send_header("Content-Type",mime);self.send_header("Content-Length",str(len(raw)));self.send_header("Cache-Control","no-cache");self.send_header("X-Content-Type-Options","nosniff");self.end_headers();self.wfile.write(raw)
    def get_api(self,p):
        if p=="/api/health":self.reply(200,{"status":"ok","product":"Xdigitex Trade","mode":"inspection","live_execution":False});return
        if p=="/api/market/quotes":self.reply(200,get_quotes());return
        if p=="/api/me":
            u,csrf=self.session();self.reply(200,{"user":public_user(u) if u else None,"csrf_token":csrf});return
        if p=="/api/dashboard":
            u,_=self.auth()
            if not u:return
            with db() as c:
                balances=grouped_totals(c,"SELECT currency,SUM(delta_cents) n FROM ledger_entries WHERE user_id=? GROUP BY currency",(u["id"],))
                totals=grouped_totals(c,"SELECT currency,SUM(amount_cents) n FROM funding_requests WHERE user_id=? AND kind='deposit' AND status IN ('confirmed','paid') GROUP BY currency",(u["id"],))
                wtotal=grouped_totals(c,"SELECT currency,SUM(amount_cents) n FROM funding_requests WHERE user_id=? AND kind='withdrawal' AND status='paid' GROUP BY currency",(u["id"],))
                pend=grouped_totals(c,"SELECT currency,SUM(amount_cents) n FROM funding_requests WHERE user_id=? AND status='pending' AND kind='deposit' GROUP BY currency",(u["id"],))
                pwith=grouped_totals(c,"SELECT currency,SUM(amount_cents) n FROM funding_requests WHERE user_id=? AND status='pending' AND kind='withdrawal' GROUP BY currency",(u["id"],))
                rows=c.execute("SELECT * FROM ledger_entries WHERE user_id=? ORDER BY rowid DESC LIMIT 100",(u["id"],)).fetchall()
                allrows=c.execute("SELECT currency,delta_cents FROM ledger_entries WHERE user_id=? ORDER BY rowid",(u["id"],)).fetchall();running={}
                for x in allrows:running[x["currency"]]=running.get(x["currency"],0)+x["delta_cents"]
                ledger=[]
                for r in rows:
                    x=dict(r);cur=x["currency"];x["amount"]=cash(abs(x["delta_cents"]),cur);x["balance_after"]=cash(running.get(cur,0),cur);x["reference"]=x["external_reference"];ledger.append(x);running[cur]=running.get(cur,0)-x["delta_cents"]
                req=c.execute("SELECT * FROM funding_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(u["id"],)).fetchall()
                activity=[dict(description=r["description"],entry_type=r["entry_type"],reference=r["external_reference"],created_at=r["created_at"],amount=cash(abs(r["delta_cents"]),r["currency"]),currency=r["currency"]) for r in rows[:6]]
                activity += [dict(description=f"{r['kind'].title()} · {r['provider_status'] or r['status']}",entry_type=r["kind"],reference=r["provider_reference"] or r["customer_reference"] or r["destination"],created_at=r["created_at"],amount=cash(r["amount_cents"],r["currency"]),currency=r["currency"],status=r["status"]) for r in req[:6]];activity.sort(key=lambda x:x["created_at"],reverse=True)
            pool={"name":"Xdigitex Internal Pool","member_reference":"XDT-"+u["id"].replace("-","")[:8].upper(),"balance_source":"customer_payment_ledger","execution_mode":"internal_price_reference","trading_enabled":False,"execution_status":"not_implemented","units_issued":False}
            self.reply(200,{"balance_by_currency":balances,"total_deposits_by_currency":totals,"total_withdrawals_by_currency":wtotal,"pending_deposits_by_currency":pend,"pending_withdrawals_by_currency":pwith,"kyc_status":u["kyc_status"],"pool":pool,"integrations":integration_status(),"ledger":ledger,"requests":[self.req_json(r) for r in req],"recent":activity[:6]});return
        if p=="/api/admin/overview":
            u,_=self.auth(True)
            if not u:return
            with db() as c:
                users=c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 500").fetchall()
                req=c.execute("SELECT r.*,u.name user_name,u.email user_email FROM funding_requests r JOIN users u ON u.id=r.user_id ORDER BY r.created_at DESC LIMIT 500").fetchall()
                total=grouped_totals(c,"SELECT currency,SUM(delta_cents) n FROM ledger_entries WHERE entry_type='deposit_credit' GROUP BY currency")
                nusers=c.execute("SELECT COUNT(*) FROM users WHERE role='investor'").fetchone()[0]
                pd=c.execute("SELECT COUNT(*) FROM funding_requests WHERE kind='deposit' AND status='pending'").fetchone()[0];pw=c.execute("SELECT COUNT(*) FROM funding_requests WHERE kind='withdrawal' AND status='pending'").fetchone()[0];pk=c.execute("SELECT COUNT(*) FROM users WHERE role='investor' AND kyc_status='pending'").fetchone()[0]
                audits=c.execute("SELECT a.*,u.email actor_email FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.rowid DESC LIMIT 250").fetchall()
            ints=integration_status()
            self.reply(200,{"metrics":{"users":nusers,"confirmed_contributions_by_currency":total,"pending_deposit_count":pd,"pending_withdrawal_count":pw,"pending_kyc_count":pk},"users":[public_user(x) for x in users],"requests":[self.req_json(x,True) for x in req],"audit":[dict(x) for x in audits],"integrations":ints});return
        m=re.fullmatch(r"/api/requests/([0-9a-f-]+)/status",p)
        if m:
            u,_=self.auth()
            if not u:return
            self.refresh_request(m.group(1),u["id"]);return
        self.fail(404,"Not found.")
    @staticmethod
    def req_json(r,admin=False):
        x=dict(r);cur=x.get("currency","USD");x.pop("provider_data",None);x.pop("pay_phone",None);x.pop("reviewer_id",None);x["amount"]=cash(x.pop("amount_cents"),cur);x["gross_amount"]=cash(x.pop("gross_cents",0),cur);x["fee"]=cash(x.pop("fee_cents",0),cur);x["reference"]=x.get("provider_reference") or x.get("external_reference") or x.get("customer_reference");return x
    def refresh_request(self,rid,uid=None):
        with db() as c:
            q="SELECT * FROM funding_requests WHERE id=?";args=[rid]
            if uid:q+=" AND user_id=?";args.append(uid)
            r=c.execute(q,args).fetchone()
        if not r:self.fail(404,"Request not found.");return
        if r["status"]!="pending":self.reply(200,{"id":rid,"status":r["status"],"provider_status":r["provider_status"]});return
        try:
            pay=pay_api()
            if not r["provider_reference"]:self.reply(409,{"error":"Provider confirmation is pending reconciliation; no retry was made because the initial request outcome is unknown."});return
            if r["kind"]=="deposit":
                data=pay.payment_status(r["provider_reference"]);reference=data.get("reference")
                if reference!=r["provider_reference"]:raise XPayError("Provider status reference did not match this request.")
                if data.get("currency") and data["currency"]!=r["currency"]:raise XPayError("Provider status currency does not match this request.")
                if data.get("amount") is not None and api_minor(data["amount"],r["currency"])!=r["gross_cents"]:raise XPayError("Provider status amount does not match this request.")
                status=str(data.get("status","pending")).lower()
                if status=="completed":
                    if data.get("net_amount") is None:raise XPayError("Provider completed response has no net amount; manual reconciliation is required.")
                    if data.get("currency")!=r["currency"] or data.get("amount") is None:raise XPayError("Provider completion did not include the expected currency and amount.")
                    net=api_minor(data["net_amount"],r["currency"]);fee=api_minor(data.get("fee",0),r["currency"])
                    if net<=0 or fee<0 or net+fee!=r["gross_cents"]:raise XPayError("Provider gross, fee, and net amounts do not reconcile.")
                    self.settle_provider(r,"confirmed",status,data,net,fee,"deposit_credit","Confirmed Xdigitex Pay deposit")
                elif status in ("failed","rejected","cancelled"):self.settle_provider(r,"rejected",status,data)
                else:self.update_provider_status(r,status,data)
            else:
                data=pay.withdrawals();items=data.get("data",[]) if isinstance(data,dict) else []
                if not isinstance(items,list):raise XPayError("Provider withdrawals response has an unexpected format.")
                item=next((x for x in items if str(x.get("reference",""))==r["provider_reference"]),None)
                if not item:self.reply(200,{"id":rid,"status":"pending","provider_status":"processing","message":"Provider has not returned a final status yet."});return
                status=str(item.get("status","processing")).lower()
                if status in ("completed","paid","success","successful") and (item.get("currency")!=r["currency"] or item.get("amount") is None):raise XPayError("Provider withdrawal completion did not include the expected currency and amount.")
                if item.get("currency") and item["currency"]!=r["currency"]:raise XPayError("Provider withdrawal currency does not match this request.")
                if item.get("amount") is not None and api_minor(item["amount"],r["currency"])!=r["gross_cents"]:raise XPayError("Provider withdrawal amount does not match this request.")
                if status in ("completed","paid","success","successful"):
                    fee=api_minor(item.get("fee",cash(r["fee_cents"],r["currency"])),r["currency"])
                    if fee<0:raise XPayError("Provider withdrawal fee is invalid.")
                    self.settle_provider(r,"paid",status,item,None,fee,"withdrawal_debit","Paid Xdigitex Pay withdrawal")
                elif status in ("failed","rejected","cancelled"):self.settle_provider(r,"rejected",status,item)
                else:self.update_provider_status(r,status,item)
            self.reply(200,{"id":rid,"status":status,"message":"Provider status checked."})
        except XPayError as e:self.fail(502,str(e))
    def update_provider_status(self,r,status,data):
        with db() as c:c.execute("UPDATE funding_requests SET provider_status=?,provider_data=?,updated_at=? WHERE id=? AND status='pending'",(status,json.dumps(data)[:16000],now(),r["id"]))
    def settle_provider(self,r,final_status,status,data,net=None,fee=None,entry_type=None,description=None):
        with db() as c:
            c.execute("BEGIN IMMEDIATE");fresh=c.execute("SELECT * FROM funding_requests WHERE id=?",(r["id"],)).fetchone()
            if not fresh or fresh["status"]!="pending":c.rollback();return
            c.execute("UPDATE funding_requests SET status=?,provider_status=?,provider_data=?,amount_cents=CASE WHEN ?='confirmed' THEN ? ELSE amount_cents END,fee_cents=COALESCE(?,fee_cents),external_reference=?,updated_at=? WHERE id=?",(final_status,status,json.dumps(data)[:16000],final_status,net,fee,r["provider_reference"],now(),r["id"]))
            if entry_type:
                delta=net if entry_type=="deposit_credit" else -(r["gross_cents"]+(fee or 0))
                add_entry(c,r["user_id"],r["id"],entry_type,delta,description,r["provider_reference"],r["user_id"],r["currency"])
            audit(c,None,f"{r['kind']}.{final_status}","funding_request",r["id"],f"Xdigitex Pay status {status}; reference {r['provider_reference']}.");c.commit()
    def do_POST(self):
        p=urlparse(self.path).path
        try:
            b=self.body()
            if p=="/api/payments/webhook":self.payment_webhook(b);return
            if p=="/api/auth/register":self.register(b);return
            if p=="/api/auth/login":self.login(b);return
            u,token=self.auth()
            if not u:return
            if not self.csrf(token):return
            if p=="/api/auth/logout":self.logout(u);return
            if p=="/api/requests/deposit":self.deposit(u,b);return
            if p=="/api/requests/withdrawal":self.withdrawal(u,b);return
            m=re.fullmatch(r"/api/admin/users/([0-9a-f-]+)/kyc",p)
            if m:
                if u["role"]!="admin":self.fail(403,"Administrator access required.");return
                self.set_kyc(u,m.group(1),b);return
            m=re.fullmatch(r"/api/admin/requests/(deposit|withdrawal)/([0-9a-f-]+)/decision",p)
            if m:
                if u["role"]!="admin":self.fail(403,"Administrator access required.");return
                self.decide(u,m.group(1),m.group(2),b);return
            m=re.fullmatch(r"/api/admin/requests/(deposit|withdrawal)/([0-9a-f-]+)/refresh",p)
            if m:
                if u["role"]!="admin":self.fail(403,"Administrator access required.");return
                self.refresh_request(m.group(2));return
            self.fail(404,"Not found.")
        except ValueError as e:self.fail(400,str(e))
        except Exception as e:print("POST:",repr(e),file=sys.stderr);self.fail(500,"An internal error occurred.")
    def register(self,b):
        name=str(b.get("name","")).strip();email=str(b.get("email","")).strip().lower();p=str(b.get("password",""))
        if len(name)<2 or len(name)>100:raise ValueError("Enter your full name.")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email):raise ValueError("Enter a valid email address.")
        if not 12<=len(p)<=200:raise ValueError("Password must be at least 12 characters.")
        if b.get("accepted_disclosures") is not True:raise ValueError("Acknowledge the account and risk notice to continue.")
        uid=str(uuid.uuid4())
        try:
            with db() as c:c.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",(uid,name,email,pw_hash(p),"investor","pending",1,now()));audit(c,uid,"account.registered","user",uid,"Investor account created; identity review pending.")
        except sqlite3.IntegrityError:raise ValueError("An account with that email already exists.")
        self.issue_session(uid)
    def login(self,b):
        ip=self.client_address[0];attempts=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if time.time()-t<900];LOGIN_ATTEMPTS[ip]=attempts
        if len(attempts)>=8:self.fail(429,"Too many sign-in attempts. Try again in 15 minutes.");return
        email=str(b.get("email"," ")).strip().lower();p=str(b.get("password",""))
        with db() as c:u=c.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
        if not u or not pw_ok(p,u["password_hash"]):
            attempts.append(time.time());LOGIN_ATTEMPTS[ip]=attempts;self.fail(401,"Email or password is incorrect.");return
        LOGIN_ATTEMPTS.pop(ip,None);self.issue_session(u["id"])
    def issue_session(self,uid):
        token=secrets.token_urlsafe(48);csrf=secrets.token_urlsafe(32);expires=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat(timespec="seconds");th=hashlib.sha256(token.encode()).hexdigest()
        with db() as c:
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",(th,uid,csrf,expires,now()));u=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone();audit(c,uid,"session.created","user",uid,"Authenticated session created.")
        cookie=f"xd_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800"+("; Secure" if COOKIE_SECURE else "")
        self.reply(200,{"user":public_user(u),"csrf_token":csrf},[cookie])
    def logout(self,u):
        m=self.jar().get("xd_session")
        if m:
            th=hashlib.sha256(m.value.encode()).hexdigest()
            with db() as c:c.execute("DELETE FROM sessions WHERE token_hash=?",(th,));audit(c,u["id"],"session.ended","user",u["id"],"User signed out.")
        cookie="xd_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"+("; Secure" if COOKIE_SECURE else "")
        self.reply(200,{"ok":True},[cookie])
    def deposit(self,u,b):
        if u["kyc_status"]!="verified":raise ValueError("Identity review must be verified before requesting a deposit.")
        currency=str(b.get("currency","KES")).upper();n=cents(b.get("amount"),currency);gateway=str(b.get("gateway","mobile"))
        if gateway not in ("card","safaricom","airtel","mobile","crypto"):raise ValueError("Choose a supported Xdigitex Pay gateway.")
        if (gateway=="crypto")!=(currency in CRYPTO_CURRENCIES):raise ValueError("Use USDT, BTC, ETH, or BNB for crypto checkout; choose a fiat currency for other gateways.")
        phone=str(b.get("phone"," ")).strip()
        if gateway in ("mobile","safaricom","airtel") and not re.fullmatch(r"\+[1-9]\d{7,14}",phone):raise ValueError("Enter a phone number in international format, including + and country code.")
        if gateway in ("safaricom","airtel") and currency!="KES":raise ValueError("Kenya M-Pesa and Airtel STK use KES; choose Pan-Africa Mobile Money for other supported countries.")
        pay=pay_api()
        if not pay.ready:raise XPayError("Set XDIGITEX_PAY_API_KEY and PUBLIC_BASE_URL on the server before accepting deposits.")
        i=str(uuid.uuid4());t=now();webhook=public_url("/api/payments/webhook");callback=public_url("/")
        with db() as c:c.execute("INSERT INTO funding_requests(id,user_id,kind,amount_cents,status,method,currency,gross_cents,provider_status,pay_phone,created_at,updated_at) VALUES(?,?, 'deposit',?,'pending',?,?,?,'initiating',?,?,?)",(i,u["id"],n,gateway,currency,n,phone if phone else None,t,t));audit(c,u["id"],"deposit.initiating","funding_request",i,f"Xdigitex Pay {gateway} {currency} {cash(n,currency)} checkout requested.")
        payload={"amount":api_amount(n,currency),"currency":currency,"gateway":gateway,"email":u["email"],"first_name":u["name"].split()[0],"last_name":" ".join(u["name"].split()[1:]) or "Customer","description":"Xdigitex Trade deposit","callback_url":callback,"webhook_url":webhook}
        if phone:payload["phone"]=phone
        try:data=pay.initiate_payment(payload)
        except XPayError as e:
            with db() as c:c.execute("UPDATE funding_requests SET provider_status='initiation_unknown',provider_data=?,updated_at=? WHERE id=?",(json.dumps({"error":str(e)})[:2000],now(),i));audit(c,u["id"],"deposit.initiation_unknown","funding_request",i,"Provider outcome is unknown; automatic retry is blocked.")
            self.fail(502,"Xdigitex Pay outcome is uncertain. Do not retry yet; contact operations to reconcile the provider account.");return
        ref=str(data.get("reference","")).strip()
        if not ref:
            with db() as c:c.execute("UPDATE funding_requests SET provider_status='initiation_unknown',provider_data=?,updated_at=? WHERE id=?",(json.dumps(data)[:16000],now(),i));audit(c,u["id"],"deposit.initiation_unknown","funding_request",i,"Provider response had no reference; automatic retry is blocked.")
            self.fail(502,"Xdigitex Pay returned no payment reference. Contact operations before retrying.");return
        fee=api_minor(data.get("fee",0),currency) if data.get("fee") is not None else 0
        with db() as c:c.execute("UPDATE funding_requests SET provider_reference=?,provider_status=?,fee_cents=?,provider_data=?,updated_at=? WHERE id=?",(ref,str(data.get("pawa_status","pending")),fee,json.dumps(data)[:16000],now(),i));audit(c,u["id"],"deposit.initiated","funding_request",i,f"Xdigitex Pay payment reference {ref}.")
        out={"id":i,"reference":ref,"status":"pending","provider_status":data.get("pawa_status","pending"),"message":data.get("message","Complete payment using Xdigitex Pay."),"checkout_url":None,"redirect_url":None,"qrcode_link":None}
        for key in ("redirect_url","checkout_url","qrcode_link"):
            if data.get(key):out[key]=provider_url(data[key])
        self.reply(201,out)
    def withdrawal(self,u,b):
        if u["kyc_status"]!="verified":raise ValueError("Identity review must be verified before requesting a withdrawal.")
        currency=str(b.get("currency","KES")).upper();n=cents(b.get("amount"),currency)
        if currency not in MIN_WITHDRAWAL:raise ValueError("Xdigitex Pay does not support withdrawals in this currency.")
        if n<MIN_WITHDRAWAL[currency]:raise ValueError(f"Minimum withdrawal is {cash(MIN_WITHDRAWAL[currency],currency)} {currency}.")
        destination=str(b.get("phone"," ")).strip()
        if not re.fullmatch(r"\+[1-9]\d{7,14}",destination):raise ValueError("Enter the mobile money number in international format, including + and country code.")
        pay=pay_api()
        if not pay.ready:raise XPayError("Set XDIGITEX_PAY_API_KEY and PUBLIC_BASE_URL on the server before requesting withdrawals.")
        i=str(uuid.uuid4());t=now();reserve_fee=(n*3+99)//100
        with db() as c:
            c.execute("BEGIN IMMEDIATE");bal=c.execute("SELECT COALESCE(SUM(delta_cents),0) FROM ledger_entries WHERE user_id=? AND currency=?",(u["id"],currency)).fetchone()[0];reserved=c.execute("SELECT COALESCE(SUM(gross_cents+fee_cents),0) FROM funding_requests WHERE user_id=? AND currency=? AND kind='withdrawal' AND status='pending'",(u["id"],currency)).fetchone()[0]
            if n+reserve_fee>bal-reserved:c.rollback();raise ValueError(f"Available {currency} balance does not cover the withdrawal amount plus the documented fee reserve.")
            c.execute("INSERT INTO funding_requests(id,user_id,kind,amount_cents,status,method,destination,currency,gross_cents,fee_cents,provider_status,created_at,updated_at) VALUES(?,?, 'withdrawal',?,'pending','mobile_money',?,?,?,?,'initiating',?,?)",(i,u["id"],n,destination,currency,n,reserve_fee,t,t));audit(c,u["id"],"withdrawal.initiating","funding_request",i,f"Xdigitex Pay mobile_money {currency} {cash(n,currency)} payout requested.");c.commit()
        try:data=pay.initiate_withdrawal({"amount":api_amount(n,currency),"currency":currency,"method":"mobile_money","account_number":destination})
        except XPayError as e:
            with db() as c:c.execute("UPDATE funding_requests SET provider_status='initiation_unknown',provider_data=?,updated_at=? WHERE id=?",(json.dumps({"error":str(e)})[:2000],now(),i));audit(c,u["id"],"withdrawal.initiation_unknown","funding_request",i,"Provider outcome is unknown; automatic retry is blocked.")
            self.fail(502,"Xdigitex Pay outcome is uncertain. Do not retry; contact operations to reconcile the payout.");return
        ref=str(data.get("reference","")).strip()
        if not ref:
            with db() as c:c.execute("UPDATE funding_requests SET provider_status='initiation_unknown',provider_data=?,updated_at=? WHERE id=?",(json.dumps(data)[:16000],now(),i));audit(c,u["id"],"withdrawal.initiation_unknown","funding_request",i,"Provider response had no reference; automatic retry is blocked.")
            self.fail(502,"Xdigitex Pay returned no withdrawal reference. Contact operations before retrying.");return
        fee=api_minor(data.get("fee",0),currency) if data.get("fee") is not None else reserve_fee
        with db() as c:c.execute("UPDATE funding_requests SET provider_reference=?,provider_status=?,fee_cents=?,provider_data=?,updated_at=? WHERE id=?",(ref,str(data.get("status","processing")),fee,json.dumps(data)[:16000],now(),i));audit(c,u["id"],"withdrawal.initiated","funding_request",i,f"Xdigitex Pay payout reference {ref}.")
        self.reply(201,{"id":i,"reference":ref,"status":"pending","provider_status":data.get("status","processing"),"amount":cash(n,currency),"fee":cash(fee,currency),"currency":currency,"message":data.get("message","Withdrawal submitted to Xdigitex Pay.")})
    def payment_webhook(self,b):
        event=str(b.get("event",""));ref=str(b.get("reference","")).strip()
        if not ref or event not in ("payment.completed","payment.failed"):
            self.fail(400,"Unsupported or incomplete Xdigitex Pay webhook event.");return
        with db() as c:r=c.execute("SELECT * FROM funding_requests WHERE kind='deposit' AND provider_reference=?",(ref,)).fetchone()
        if not r:self.fail(404,"Payment reference not found.");return
        # The pasted API docs specify no webhook signature. Treat the event only as a prompt to
        # fetch authenticated provider status; never credit based on the webhook body.
        try:
            data=pay_api().payment_status(ref)
            if data.get("reference")!=ref:raise XPayError("Provider status reference did not match the webhook.")
            status=str(data.get("status","pending")).lower()
            if status=="completed":
                if data.get("currency") and data["currency"]!=r["currency"]:raise XPayError("Provider status currency does not match this request.")
                if data.get("amount") is not None and api_minor(data["amount"],r["currency"])!=r["gross_cents"]:raise XPayError("Provider status amount does not match this request.")
                if data.get("currency")!=r["currency"] or data.get("amount") is None:raise XPayError("Provider completion did not include the expected currency and amount.")
                net=api_minor(data.get("net_amount"),r["currency"]);fee=api_minor(data.get("fee",0),r["currency"])
                if net<=0 or fee<0 or net+fee!=r["gross_cents"]:raise XPayError("Provider gross, fee, and net amounts do not reconcile.")
                self.settle_provider(r,"confirmed",status,data,net,fee,"deposit_credit","Confirmed Xdigitex Pay deposit")
            elif status in ("failed","rejected","cancelled"):self.settle_provider(r,"rejected",status,data)
            else:self.update_provider_status(r,status,data)
            self.reply(200,{"ok":True,"status":status})
        except XPayError as e:self.fail(502,str(e))
    def set_kyc(self,actor,uid,b):
        status=str(b.get("status",""))
        if status not in ("verified","rejected"):raise ValueError("Identity status must be verified or rejected.")
        with db() as c:
            u=c.execute("SELECT * FROM users WHERE id=? AND role='investor'",(uid,)).fetchone()
            if not u:self.fail(404,"Investor account not found.");return
            c.execute("UPDATE users SET kyc_status=? WHERE id=?",(status,uid));audit(c,actor["id"],f"kyc.{status}","user",uid,f"Manual identity review set status to {status}.")
        self.reply(200,{"ok":True,"status":status})
    def decide(self,actor,kind,rid,b):
        raise ValueError("Funding requests cannot be manually rejected or settled. Refresh against Xdigitex Pay and reconcile any provider exception first.")

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest="cmd");a=sub.add_parser("create-admin");a.add_argument("--email",required=True);a.add_argument("--name",default="Xdigitex Operations");args=p.parse_args()
    if args.cmd=="create-admin":
        x=getpass.getpass("Admin password (16+ characters): ");y=getpass.getpass("Repeat password: ")
        if x!=y:print("Passwords do not match.",file=sys.stderr);return 2
        try:bootstrap_admin(args.email,args.name,x)
        except Exception as e:print(str(e),file=sys.stderr);return 2
        print(f"Administrator provisioned: {args.email.strip().lower()}");return 0
    init_db();email=os.environ.get("BOOTSTRAP_ADMIN_EMAIL","");password=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD","")
    if email and password:
        try:bootstrap_admin(email,os.environ.get("BOOTSTRAP_ADMIN_NAME","Xdigitex Operations"),password)
        except Exception as e:print("Administrator bootstrap failed:",e,file=sys.stderr);return 2
    print(f"Xdigitex Trade at http://{HOST}:{PORT} · inspection build · live trading and payouts disabled")
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever();return 0
if __name__=="__main__":raise SystemExit(main())
