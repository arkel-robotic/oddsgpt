"""
OddsGPT v7
"""
import os, sqlite3, uuid, httpx, re, asyncio, hashlib, hmac, base64, json
import random, string, smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
FRONTEND_PATH = os.path.join(BASE_DIR, "..", "frontend")
DB_PATH       = os.getenv("DB_PATH", os.path.join(BASE_DIR, "oddsgpt.db"))

GROQ_API_KEY      = os.getenv("GROQ_API_KEY",      "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY",     "")
JWT_SECRET        = os.getenv("JWT_SECRET",         "OddsGPT_v7_secret!")
OWNER_USERNAME    = os.getenv("OWNER_USERNAME",     "arkel")
OPERATOR_PASSWORD = os.getenv("OPERATOR_PASSWORD",  "Operator2025!")
GMAIL_USER        = os.getenv("GMAIL_USER",         "noreplyoddsgpt@gmail.com")
GMAIL_APP_PASS    = os.getenv("GMAIL_APP_PASS",     "")
APP_URL           = os.getenv("APP_URL",            "http://localhost:8000")
MONTHLY_CODE      = os.getenv("MONTHLY_CODE",       "ODDS2025")
MAX_DEVICES       = int(os.getenv("MAX_DEVICES",    "2"))
PAYPAL_CLIENT_ID  = os.getenv("PAYPAL_CLIENT_ID",   "")
PAYPAL_SECRET     = os.getenv("PAYPAL_SECRET",      "")
PAYPAL_MODE       = os.getenv("PAYPAL_MODE",        "sandbox")

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
GROQ_MODEL = "llama-3.3-70b-versatile"
FREE_LIMIT = 3

def get_conn():
    for attempt in range(3):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("SELECT 1")  # quick sanity check
            return conn
        except sqlite3.DatabaseError as e:
            print(f"[DB] Attempt {attempt+1} failed: {e}")
            try: conn.close()
            except: pass
            if os.path.exists(DB_PATH):
                print(f"[DB] Deleting corrupted database: {DB_PATH}")
                try: os.remove(DB_PATH)
                except OSError as oe: print(f"[DB] Could not delete: {oe}")
            if attempt == 2:
                raise
    raise RuntimeError("Could not open database after 3 attempts")

def init_db():
    with get_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_plain TEXT NOT NULL, password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'free', is_verified INTEGER DEFAULT 0,
                verify_token TEXT, reset_token TEXT, reset_expires TEXT,
                premium_source TEXT, premium_expires TEXT,
                daily_count INTEGER DEFAULT 0, last_reset_date TEXT DEFAULT '',
                total_searches INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0, ban_reason TEXT, ban_expires TEXT,
                created_at TEXT, last_login TEXT
            );
            CREATE TABLE IF NOT EXISTS device_accounts (
                device_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT,
                PRIMARY KEY (device_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                sport_tab TEXT DEFAULT 'all', title TEXT DEFAULT 'New Chat',
                created_at TEXT, updated_at TEXT, hidden INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                sport_tab TEXT DEFAULT 'all', created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS saved_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, user_id TEXT NOT NULL,
                match_name TEXT, bet_type TEXT, pick TEXT,
                confidence INTEGER, reasoning TEXT, risk TEXT,
                odds_range TEXT, result TEXT DEFAULT 'pending', created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS parlays (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                picks TEXT NOT NULL, combined_odds TEXT, confidence INTEGER,
                note TEXT, result TEXT DEFAULT 'pending', created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS search_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                query TEXT NOT NULL, sport TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY, type TEXT DEFAULT 'monthly',
                grants_role TEXT DEFAULT 'premium', expires_at TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ban_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT, device_id TEXT, banned_by TEXT,
                reason TEXT, expires_at TEXT, created_at TEXT,
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, paypal_order_id TEXT,
                amount TEXT, currency TEXT DEFAULT 'USD',
                status TEXT DEFAULT 'pending',
                created_at TEXT, confirmed_at TEXT, confirmed_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_msg_sess  ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_bets_user ON saved_bets(user_id);
            CREATE INDEX IF NOT EXISTS idx_log_user  ON search_log(user_id);
        """)
        for col in ["hidden"]:
            try: c.execute(f"ALTER TABLE sessions ADD COLUMN {col} INTEGER DEFAULT 0")
            except: pass
        for col in ["active"]:
            try: c.execute(f"ALTER TABLE ban_log ADD COLUMN {col} INTEGER DEFAULT 1")
            except: pass
    _seed()

def _seed():
    now=datetime.now().isoformat()
    eom=(datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1,hour=0,minute=0,second=0).isoformat()
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO promo_codes VALUES (?,?,?,?,?)",(MONTHLY_CODE.upper(),"monthly","premium",eom,now))
        c.execute("INSERT OR IGNORE INTO promo_codes VALUES (?,?,?,?,?)",("OWNER","permanent","owner",None,now))
        c.execute("INSERT OR IGNORE INTO promo_codes VALUES (?,?,?,?,?)",("OPERATOR","permanent","operator",None,now))
        for k,v in [("payments_enabled","false"),("premium_price","9.99"),("premium_currency","USD")]:
            c.execute("INSERT OR IGNORE INTO settings (key,value,updated_at) VALUES (?,?,?)",(k,v,now))

def get_setting(k):
    with get_conn() as c: r=c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
    return r["value"] if r else None

def set_setting(k,v):
    with get_conn() as c: c.execute("INSERT OR REPLACE INTO settings (key,value,updated_at) VALUES (?,?,?)",(k,v,datetime.now().isoformat()))

def hash_pw(p):
    s=os.urandom(16); k=hashlib.pbkdf2_hmac("sha256",p.encode(),s,200000)
    return base64.b64encode(s+k).decode()

def check_pw(p,stored):
    try:
        r=base64.b64decode(stored.encode()); s=r[:16]; k=r[16:]
        return hmac.compare_digest(k,hashlib.pbkdf2_hmac("sha256",p.encode(),s,200000))
    except: return False

def rand_tok(n=40): return ''.join(random.choices(string.ascii_letters+string.digits,k=n))

def is_privileged(u): return u.get("role") in("premium","operator","owner")
def is_staff(u):      return u.get("role") in("operator","owner")
def is_owner(u):      return u.get("role")=="owner"

def create_user(username,email,password):
    uid=str(uuid.uuid4()); now=datetime.now().isoformat(); vtok=rand_tok()
    if username.lower()==OWNER_USERNAME.lower(): role="owner"
    elif password==OPERATOR_PASSWORD:            role="operator"
    else:                                        role="free"
    with get_conn() as c:
        c.execute("INSERT INTO users (id,username,email,password_plain,password_hash,role,is_verified,verify_token,daily_count,last_reset_date,total_searches,created_at) VALUES (?,?,?,?,?,?,0,?,0,'',0,?)",
                  (uid,username.lower(),email.lower(),password,hash_pw(password),role,vtok,now))
    return get_user_by_id(uid)

def get_user_by_id(uid):
    with get_conn() as c: r=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
    return dict(r) if r else None

def get_user_by_username(u):
    with get_conn() as c: r=c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",(u,)).fetchone()
    return dict(r) if r else None

def get_user_by_email(e):
    with get_conn() as c: r=c.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE",(e,)).fetchone()
    return dict(r) if r else None

def get_user_by_token(tok,field):
    with get_conn() as c: r=c.execute(f"SELECT * FROM users WHERE {field}=?",(tok,)).fetchone()
    return dict(r) if r else None

def update_user(uid,**kw):
    sets=", ".join(f"{k}=?" for k in kw)
    with get_conn() as c: c.execute(f"UPDATE users SET {sets} WHERE id=?",list(kw.values())+[uid])

def register_device(device_id,user_id):
    if not device_id: return True
    with get_conn() as c:
        cnt=c.execute("SELECT COUNT(DISTINCT user_id) FROM device_accounts WHERE device_id=?",(device_id,)).fetchone()[0]
        ex=c.execute("SELECT 1 FROM device_accounts WHERE device_id=? AND user_id=?",(device_id,user_id)).fetchone()
    if ex: return True
    if cnt>=MAX_DEVICES: return False
    with get_conn() as c: c.execute("INSERT OR IGNORE INTO device_accounts VALUES (?,?,?)",(device_id,user_id,datetime.now().isoformat()))
    return True

def get_device_count(device_id):
    if not device_id: return 0
    with get_conn() as c: return c.execute("SELECT COUNT(DISTINCT user_id) FROM device_accounts WHERE device_id=?",(device_id,)).fetchone()[0]

def is_device_banned(device_id):
    if not device_id: return False
    now=datetime.now().isoformat()
    with get_conn() as c:
        r=c.execute("SELECT 1 FROM ban_log WHERE device_id=? AND active=1 AND (expires_at IS NULL OR expires_at>?) LIMIT 1",(device_id,now)).fetchone()
    return bool(r)

def ban_user(uid,reason,expires_at,by):
    update_user(uid,is_banned=1,ban_reason=reason,ban_expires=expires_at or "")
    with get_conn() as c:
        c.execute("INSERT INTO ban_log (user_id,banned_by,reason,expires_at,active,created_at) VALUES (?,?,?,?,1,?)",
                  (uid,by,reason,expires_at,datetime.now().isoformat()))

def ban_device(device_id,reason,expires_at,by):
    with get_conn() as c:
        c.execute("INSERT INTO ban_log (device_id,banned_by,reason,expires_at,active,created_at) VALUES (?,?,?,?,1,?)",
                  (device_id,by,reason,expires_at,datetime.now().isoformat()))

def unban_user(uid):
    update_user(uid,is_banned=0,ban_reason=None,ban_expires=None)
    with get_conn() as c: c.execute("UPDATE ban_log SET active=0 WHERE user_id=?",(uid,))

def unban_device(device_id):
    with get_conn() as c: c.execute("UPDATE ban_log SET active=0 WHERE device_id=?",(device_id,))

def check_ban(user):
    if not user.get("is_banned"): return None
    exp=user.get("ban_expires","")
    if exp and datetime.fromisoformat(exp)<datetime.now():
        unban_user(user["id"]); return None
    return user.get("ban_reason") or "Account banned."

def refresh_user(user):
    u=get_user_by_id(user["id"])
    if not u: return user
    if u.get("premium_source")=="monthly" and u.get("premium_expires"):
        if datetime.fromisoformat(u["premium_expires"])<datetime.now():
            update_user(u["id"],role="free",premium_source=None,premium_expires=None)
            u=get_user_by_id(u["id"])
    return u

def check_usage(user):
    if is_privileged(user): return True
    today=datetime.now().strftime("%Y-%m-%d")
    if user.get("last_reset_date","")!=today:
        update_user(user["id"],daily_count=1,last_reset_date=today); return True
    cnt=user.get("daily_count",0)
    if cnt>=FREE_LIMIT: return False
    update_user(user["id"],daily_count=cnt+1); return True

def apply_code(user,code):
    code=code.strip().upper()
    with get_conn() as c: row=c.execute("SELECT * FROM promo_codes WHERE code=? COLLATE NOCASE",(code,)).fetchone()
    if not row: return False,"Invalid code."
    row=dict(row); exp=row.get("expires_at","")
    if exp and datetime.fromisoformat(exp)<datetime.now(): return False,"This code has expired."
    grants=row.get("grants_role","premium")
    if user.get("role")=="owner": return True,"You already have the highest access."
    eom=(datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1).isoformat()
    exp_date=eom if row.get("type")=="monthly" else None
    update_user(user["id"],role=grants,premium_source=code.lower(),premium_expires=exp_date)
    msgs={"premium":f"Premium activated!{' Valid until '+exp_date[:10] if exp_date else ''}",
          "operator":"Operator access granted!","owner":"Owner access granted!"}
    return True,msgs.get(grants,"Access granted!")

def make_jwt(uid,username,role):
    p=json.dumps({"uid":uid,"usr":username,"role":role,"exp":(datetime.now()+timedelta(days=30)).isoformat()},separators=(",",":"))
    b=base64.urlsafe_b64encode(p.encode()).decode()
    s=hmac.new(JWT_SECRET.encode(),b.encode(),hashlib.sha256).hexdigest()
    return f"{b}.{s}"

def verify_jwt(tok):
    try:
        b,s=tok.split(".",1)
        if not hmac.compare_digest(s,hmac.new(JWT_SECRET.encode(),b.encode(),hashlib.sha256).hexdigest()): return None
        p=json.loads(base64.urlsafe_b64decode(b+"==").decode())
        if datetime.fromisoformat(p["exp"])<datetime.now(): return None
        return p
    except: return None

async def get_user(authorization:Optional[str]=Header(None),x_device_id:Optional[str]=Header(None)):
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(401,"Not authenticated")
    data=verify_jwt(authorization[7:].strip())
    if not data: raise HTTPException(401,"Session expired. Please log in again.")
    user=get_user_by_id(data["uid"])
    if not user: raise HTTPException(401,"Account not found.")
    if x_device_id and is_device_banned(x_device_id): raise HTTPException(403,"This device has been banned.")
    ban_msg=check_ban(user)
    if ban_msg: raise HTTPException(403,f"Banned: {ban_msg}")
    return refresh_user(user)

async def staff_user(user=Depends(get_user)):
    if not is_staff(user): raise HTTPException(403,"Staff access required.")
    return user

async def owner_user(user=Depends(get_user)):
    if not is_owner(user): raise HTTPException(403,"Owner access required.")
    return user

# DB helpers
def db_new_session(uid,sport_tab="all"):
    sid=str(uuid.uuid4())[:8]; now=datetime.now().isoformat()
    with get_conn() as c: c.execute("INSERT INTO sessions (id,user_id,sport_tab,title,created_at,updated_at,hidden) VALUES (?,?,?,?,?,?,0)",(sid,uid,sport_tab,"New Chat",now,now))
    return sid

def db_get_sessions(uid):
    with get_conn() as c:
        rows=c.execute("SELECT id,sport_tab,title,created_at,updated_at FROM sessions WHERE user_id=? AND hidden=0 ORDER BY updated_at DESC LIMIT 80",(uid,)).fetchall()
    return [dict(r) for r in rows]

def db_soft_delete(sid,uid):
    with get_conn() as c: c.execute("UPDATE sessions SET hidden=1 WHERE id=? AND user_id=?",(sid,uid))

def db_save_message(sid,uid,role,content,sport_tab="all"):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO messages (session_id,user_id,role,content,sport_tab,created_at) VALUES (?,?,?,?,?,?)",(sid,uid,role,content,sport_tab,now))
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?",(now,sid))
    if role=="user":
        title=content[:55]+("..." if len(content)>55 else "")
        with get_conn() as c:
            cur=c.execute("SELECT title FROM sessions WHERE id=?",(sid,)).fetchone()
            if cur and cur["title"]=="New Chat": c.execute("UPDATE sessions SET title=? WHERE id=?",(title,sid))

def db_get_history(sid,uid):
    with get_conn() as c:
        rows=c.execute("SELECT role,content FROM messages WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20",(sid,uid)).fetchall()
    return [{"role":r["role"],"content":r["content"]} for r in reversed(rows)]

def db_save_bets(sid,uid,bets):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("DELETE FROM saved_bets WHERE session_id=?",(sid,))
        for b in bets:
            c.execute("INSERT INTO saved_bets (session_id,user_id,match_name,bet_type,pick,confidence,reasoning,risk,odds_range,result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (sid,uid,b.get("match",""),b.get("type",""),b.get("pick",""),b.get("confidence",0),b.get("reasoning",""),b.get("risk","Medium"),b.get("odds_range","N/A"),"pending",now))

def db_get_bets(sid):
    with get_conn() as c: rows=c.execute("SELECT * FROM saved_bets WHERE session_id=? ORDER BY id",(sid,)).fetchall()
    return [dict(r) for r in rows]

def db_get_user_bets(uid):
    with get_conn() as c:
        rows=c.execute("SELECT sb.*,s.title s_title FROM saved_bets sb LEFT JOIN sessions s ON sb.session_id=s.id WHERE sb.user_id=? ORDER BY sb.created_at DESC LIMIT 200",(uid,)).fetchall()
    return [dict(r) for r in rows]

def db_log_search(uid,query,sport):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO search_log (user_id,query,sport,created_at) VALUES (?,?,?,?)",(uid,query,sport,now))
        c.execute("UPDATE users SET total_searches=total_searches+1 WHERE id=?",(uid,))

def db_get_hot_searched(sport=None,limit=20):
    with get_conn() as c:
        if sport and sport!="all":
            rows=c.execute("SELECT query,sport,COUNT(*) cnt FROM search_log WHERE sport=? GROUP BY query ORDER BY cnt DESC LIMIT ?",(sport,limit)).fetchall()
        else:
            rows=c.execute("SELECT query,sport,COUNT(*) cnt FROM search_log GROUP BY query ORDER BY cnt DESC LIMIT ?",(limit,)).fetchall()
    return [dict(r) for r in rows]

def db_update_result(bid,result,uid):
    with get_conn() as c: c.execute("UPDATE saved_bets SET result=? WHERE id=? AND user_id=?",(result,bid,uid))

def db_save_parlay(uid,picks,odds,conf,note):
    now=datetime.now().isoformat()
    with get_conn() as c: c.execute("INSERT INTO parlays (user_id,picks,combined_odds,confidence,note,result,created_at) VALUES (?,?,?,?,?,?,?)",(uid,json.dumps(picks),odds,conf,note,"pending",now))

def db_get_parlays(uid):
    with get_conn() as c: rows=c.execute("SELECT * FROM parlays WHERE user_id=? ORDER BY created_at DESC LIMIT 30",(uid,)).fetchall()
    result=[]
    for r in rows:
        d=dict(r)
        try: d["picks"]=json.loads(d["picks"])
        except: pass
        result.append(d)
    return result

# Email - dual method SSL + STARTTLS
def _send_sync(to,subject,html):
    if not GMAIL_APP_PASS:
        print(f"[Email] No GMAIL_APP_PASS → {subject} → {to}"); return
    errors=[]
    for method in ["ssl","starttls"]:
        try:
            msg=MIMEMultipart("alternative")
            msg["Subject"]=subject; msg["From"]=f"OddsGPT <{GMAIL_USER}>"; msg["To"]=to
            msg["Message-ID"]=f"<{uuid.uuid4()}@oddsgpt.com>"
            msg.attach(MIMEText(html,"html","utf-8"))
            if method=="ssl":
                with smtplib.SMTP_SSL("smtp.gmail.com",465,timeout=20) as s:
                    s.login(GMAIL_USER,GMAIL_APP_PASS); s.sendmail(GMAIL_USER,[to],msg.as_string())
            else:
                with smtplib.SMTP("smtp.gmail.com",587,timeout=20) as s:
                    s.ehlo(); s.starttls(); s.ehlo(); s.login(GMAIL_USER,GMAIL_APP_PASS)
                    s.sendmail(GMAIL_USER,[to],msg.as_string())
            print(f"[Email] OK {method} → {to}"); return
        except Exception as e: errors.append(f"{method}:{e}"); continue
    print(f"[Email] FAILED {to}: {errors}")

async def send_email(to,subject,html):
    await asyncio.get_event_loop().run_in_executor(None,_send_sync,to,subject,html)

TPL="""<div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;margin:auto;background:#07090e;color:#dde4f0;border-radius:14px;overflow:hidden;border:1px solid #1c2535"><div style="background:#0c1018;padding:18px 24px;border-bottom:1px solid #1c2535;display:flex;align-items:center;gap:10px"><span style="font-size:22px;font-weight:900;color:#f0c040">Odds</span><span style="font-size:22px;font-weight:900;color:#dde4f0">GPT</span><span style="font-size:10px;color:#4a5a70;margin-left:auto;font-family:monospace;letter-spacing:1px">AI BETTING ANALYST</span></div><div style="padding:28px">{body}<p style="color:#4a5a70;font-size:11px;margin-top:22px;padding-top:14px;border-top:1px solid #1c2535">OddsGPT · For entertainment only · 18+ · Bet responsibly<br>Vetëm për argëtim · 18+ · Basto me përgjegjësi</p></div></div>"""
def ebtn(u,t,c="#f0c040"): return f'<a href="{u}" style="display:inline-block;background:{c};color:#000;padding:12px 26px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px;margin:16px 0">{t}</a>'

async def send_verify(email,token):
    link=f"{APP_URL}/api/auth/verify/{token}"
    body=f'<h2 style="color:#f0c040;margin-top:0">Verify your email 🎯</h2><p style="color:#8a9ab0">Activate your OddsGPT account / Aktivizo llogarinë:</p>{ebtn(link,"✓ Verify Account / Verifiko")}<p style="color:#4a5a70;font-size:12px">Expires 24h · Skadon 24h</p>'
    await send_email(email,"[OddsGPT] Verify your account",TPL.format(body=body))

async def send_reset(email,token):
    link=f"{APP_URL}/reset?token={token}"
    body=f'<h2 style="color:#f0c040;margin-top:0">Reset Password 🔑</h2><p style="color:#8a9ab0">Expires in 1 hour:</p>{ebtn(link,"Reset Password","#e05c20")}<p style="color:#4a5a70;font-size:12px">Ignore if you didn\'t request this.</p>'
    await send_email(email,"[OddsGPT] Reset your password",TPL.format(body=body))

async def send_payment_confirm(email,username,amount):
    body=f'<h2 style="color:#f0c040;margin-top:0">Payment Received! 🎉</h2><p style="color:#8a9ab0">Hi <strong style="color:#dde4f0">{username}</strong>, your payment of <strong style="color:#f0c040">${amount}</strong> has been received. Premium will be activated shortly.</p>'
    await send_email(email,"[OddsGPT] Payment received",TPL.format(body=body))

# Hot games via AI+Tavily
async def groq_call(messages,max_tokens=1000):
    if not GROQ_API_KEY: return ""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r=await c.post(GROQ_URL,headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":GROQ_MODEL,"messages":messages,"temperature":0.2,"max_tokens":max_tokens,"stream":False})
    if r.status_code!=200: raise Exception(f"Groq error {r.status_code}")
    return r.json()["choices"][0]["message"]["content"]

async def tavily_one(sess,query):
    if not TAVILY_API_KEY: return ""
    try:
        r=await sess.post(TAVILY_URL,json={"api_key":TAVILY_API_KEY,"query":query,"search_depth":"advanced","max_results":4,"include_answer":True})
        if r.status_code!=200: return ""
        d=r.json(); parts=[]
        if d.get("answer"): parts.append(d["answer"])
        for res in d.get("results",[])[:3]: parts.append(f"• {res.get('title','')}: {res.get('content','')[:280]}")
        return "\n".join(parts)
    except Exception as e: print(f"[T] {e}"); return ""

SPORT_COUNTS={"football":4,"basketball":4,"tennis":4,"hockey":4,"baseball":4,"american football":4,"ufc":1}

async def fetch_sport_games(sport,count):
    today=datetime.now().strftime("%Y-%m-%d"); year=datetime.now().strftime("%Y")
    queries={
        "ufc":f"UFC next event main event fight card {today} {year}",
        "football":f"biggest football soccer matches this week {today} Premier League Champions League La Liga",
        "basketball":f"NBA biggest games this week {today} {year}",
        "tennis":f"ATP WTA tennis top matches this week {today} {year}",
        "hockey":f"NHL biggest games this week {today} {year}",
        "baseball":f"MLB biggest games this week {today} {year}",
        "american football":f"NFL upcoming games schedule {today} {year}",
    }
    query=queries.get(sport,f"{sport} biggest matches this week {today}")
    try:
        async with httpx.AsyncClient(timeout=12.0) as sess:
            raw=await tavily_one(sess,query)
        if not raw: return []
        prompt=f"""From this sports news extract exactly {count} upcoming match(es) for {sport}.
Return ONLY a JSON array, no explanation:
[{{"home":"Team A","away":"Team B","date":"2026-04-08","time":"20:00","sport":"{sport}","competition":"League"}}]
For UFC return only the main event. If fewer matches found return what you have.
NEWS:\n{raw[:1800]}"""
        resp=await groq_call([{"role":"user","content":prompt}],max_tokens=400)
        resp=re.sub(r'^```(?:json)?','',resp.strip()).strip().rstrip('`').strip()
        games=json.loads(resp)
        if not isinstance(games,list): return []
        result=[]
        for g in games[:count]:
            h=g.get("home","").strip(); a=g.get("away","").strip()
            if h and a:
                result.append({"home":h,"away":a,"date":g.get("date",""),"time":g.get("time",""),
                                "sport":sport,"competition":g.get("competition",""),
                                "query":f"{sport} - {h} vs {a}"})
        return result
    except Exception as e:
        print(f"[HotGames:{sport}] {e}"); return []

async def get_hot_games(sport="all"):
    if sport=="all":
        tasks=[fetch_sport_games(sp,1 if sp!="ufc" else 1) for sp in SPORT_COUNTS]
        results=await asyncio.gather(*tasks,return_exceptions=True)
        games=[]
        for r in results:
            if isinstance(r,list): games.extend(r[:1])
        return games[:8]
    count=SPORT_COUNTS.get(sport,4)
    return await fetch_sport_games(sport,count)

# Match analysis
async def search_match(team1,team2,sport):
    if not TAVILY_API_KEY: return "No Tavily key set."
    today=datetime.now().strftime("%Y-%m-%d"); year=datetime.now().strftime("%Y"); matchup=f"{team1} vs {team2}"
    if sport in("ufc","mma"):
        queries=[("📅 EVENT",f"{matchup} UFC MMA fight date time {today}"),("🥊 RECORDS",f"{team1} {team2} UFC MMA record {year}"),("💪 STATS",f"{team1} striking grappling stats {today}"),("🏋️ CAMP",f"{team1} {team2} training camp {today}"),("🎰 ODDS",f"{matchup} UFC odds {today}"),("📰 NEWS",f"{team1} {team2} UFC news {today}")]
    elif sport in("football","soccer"):
        queries=[("📅 DATE/TIME",f"{matchup} match date kickoff venue {today}"),("📋 CONFIRMED LINEUP",f"{matchup} confirmed lineup starting XI {today}"),("📋 PREDICTED LINEUP",f"{matchup} predicted lineup {year}"),("👥 SQUAD 1",f"{team1} current squad players {year}"),("👥 SQUAD 2",f"{team2} current squad players {year}"),("🏥 INJURIES",f"{team1} {team2} injuries suspended OUT {today}"),("🔢 TACTICS",f"{matchup} formation tactics {today}"),("📊 FORM 1",f"{team1} last 5 results form {today}"),("📊 FORM 2",f"{team2} last 5 results form {today}"),("⚔️ H2H",f"{matchup} head to head history"),("🎰 ODDS",f"{matchup} betting odds {today}"),("📈 STATS",f"{matchup} xG BTTS over 2.5 {today}")]
    else:
        queries=[("📅 DATE",f"{matchup} {sport} date {today}"),("👥 ROSTERS",f"{team1} {team2} {sport} current roster {year}"),("🏥 INJURIES",f"{team1} {team2} injuries {today}"),("📊 FORM",f"{matchup} form {today}"),("🎰 ODDS",f"{matchup} {sport} odds {today}"),("📈 STATS",f"{matchup} {sport} stats {today}")]
    async with httpx.AsyncClient(timeout=20.0) as sess:
        results=await asyncio.gather(*[tavily_one(sess,q) for _,q in queries],return_exceptions=True)
    parts=[]
    for (label,_),r in zip(queries,results):
        if isinstance(r,str) and r.strip(): parts.append(f"\n{label}:\n{r}")
    return "\n".join(parts) or "No live data found."

def extract_match(text):
    m=re.search(r"([A-Za-z0-9\s\.'\-]+?)\s+(?:vs?\.?|versus|[-])\s+([A-Za-z0-9\s\.'\-]+?)(?:\n|$|[,\?])",text,re.IGNORECASE)
    t1=m.group(1).strip() if m else ""; t2=m.group(2).strip() if m else ""
    for s in ["football","soccer","basketball","tennis","hockey","baseball","nfl","nba","rugby","cricket","nhl","mlb","ufc","mma"]:
        t1=re.sub(rf"^{s}\s*[-:]\s*","",t1,flags=re.IGNORECASE).strip()
    sport="football"
    for s in ["ufc","mma","basketball","tennis","hockey","baseball","nfl","nba","rugby","cricket","nhl","mlb","soccer"]:
        if s in text.lower(): sport=s; break
    return t1.strip(),t2.strip(),sport

async def needs_clarify(msg,t1,t2):
    if t1 and t2 and len(t1)>1 and len(t2)>1: return None
    if re.search(r"vs?\.?\s+[A-Za-z]",msg,re.IGNORECASE): return None
    try:
        r=await groq_call([{"role":"user","content":f'User: "{msg[:200]}". Identify sports match? PROCEED or CLARIFY: [question]'}],max_tokens=60)
        if r.strip().upper().startswith("CLARIFY:"): return r.strip()[8:].strip()
    except: pass
    return None

FOOTBALL_SECTION="""**🔢 FORMATION & TACTICS** — From live data only. Analyze tactical matchup.
**📋 LINEUPS** — ONLY from live data. Label CONFIRMED or PREDICTED."""
UFC_SECTION="""**🥊 RECORDS** — Current record from live data. **💪 MATCHUP** — Style analysis. **🏋️ CAMP** — Weight cut, training news."""
GENERIC_SECTION="""**📋 LINEUP** — From live data. Note key absences."""

def get_sport_section(sport):
    if sport in("football","soccer"): return FOOTBALL_SECTION
    if sport in("ufc","mma"):        return UFC_SECTION
    return GENERIC_SECTION

SYSTEM_PROMPT=r"""You are OddsGPT — elite sports betting analyst.
Today: {date} | Sport: {sport}

CRITICAL RULE: Your training data about current squads is OUTDATED.
Players transfer clubs constantly. Messi/Neymar left PSG. Henderson left Liverpool.
USE ONLY the LIVE DATA below for: current squads, lineups, injuries, form, results.
If a player/fact is NOT in live data — DO NOT include it.

=== LIVE DATA ===
{live_data}
=== END ===

{sport_section}

📅 MATCH DATE & TIME — From live data. Format: "[Date] at [Time] — [Venue]". If missing: "Not yet confirmed."
📊 CURRENT FORM — Last 5 results each team. W/D/L + score. From live data only.
⚔️ HEAD TO HEAD — Recent H2H. Who dominates?
🏥 INJURIES — ONLY from live data. If none: "No confirmed injuries in live data."
📋 LINEUPS — ONLY from live data. Label CONFIRMED or PREDICTED.
💰 ODDS — From live data. Best value.
📈 KEY STATS — BTTS rate, Over 2.5, avg goals, xG.
💡 EXPERT TIPS — What tipsters say.

🎯 MY CONCLUSION: 4-6 sentences. "In my opinion..." Be specific about markets and reasoning.

Bets (one per line at end):
BET: [pick] | TYPE: [1X2/BTTS/Over-Under/Handicap] | CONFIDENCE: [0-100] | RISK: [Low/Medium/High] | ODDS: [range] | MATCH: [Team A vs Team B] | REASON: [one sentence]
Give 4-6 BET lines."""

def parse_bets(text):
    bets=[]
    for line in text.split("\n"):
        line=line.strip()
        if not line.upper().startswith("BET:"): continue
        try:
            line=re.sub(r'^BET:\s*','',line,flags=re.IGNORECASE)
            def g(p,d=""): m=re.search(p,line,re.IGNORECASE); return m.group(1).strip() if m else d
            pick=re.split(r'\s*\|\s*TYPE:',line,flags=re.IGNORECASE)[0].strip()
            if not pick: continue
            risk=g(r'RISK:\s*(Low|Medium|High)'); risk=risk if risk in("Low","Medium","High") else "Medium"
            conf=g(r'CONFIDENCE:\s*(\d+)')
            bets.append({"match":g(r'MATCH:\s*([^|]+)'),"type":g(r'TYPE:\s*([^|]+)') or "Bet","pick":pick,
                         "confidence":min(100,max(0,int(conf))) if conf else 70,"reasoning":g(r'REASON:\s*(.+)$'),
                         "risk":risk,"odds_range":g(r'ODDS:\s*([^|]+)') or "N/A"})
        except: pass
    return bets

async def build_parlay(bets,uid):
    if len(bets)<2: return {}
    picks=sorted([b for b in bets if b["risk"]!="High"],key=lambda x:x["confidence"],reverse=True)[:3]
    if len(picks)<2: picks=sorted(bets,key=lambda x:x["confidence"],reverse=True)[:3]
    strs=[f"{b['pick']} ({b.get('match','')[:18]})" for b in picks]
    conf=int(sum(b["confidence"] for b in picks)/len(picks)*0.75)
    try: odds=f"~{round(1.85**len(picks),2):.2f}"
    except: odds="N/A"
    db_save_parlay(uid,strs,odds,conf,f"{len(picks)}-leg parlay — small stake only.")
    return {"picks":strs,"combined_odds":odds,"confidence":conf,"note":f"{len(picks)}-leg parlay — small stake only."}

# PayPal
async def _pp_token():
    base="https://api-m.paypal.com" if PAYPAL_MODE=="live" else "https://api-m.sandbox.paypal.com"
    async with httpx.AsyncClient(timeout=15.0) as c:
        r=await c.post(f"{base}/v1/oauth2/token",auth=(PAYPAL_CLIENT_ID,PAYPAL_SECRET),headers={"Accept":"application/json"},data={"grant_type":"client_credentials"})
    if r.status_code==200: return r.json().get("access_token",""),base
    raise Exception(f"PayPal auth failed")

async def create_pp_order(amount,currency="USD"):
    tok,base=await _pp_token()
    async with httpx.AsyncClient(timeout=15.0) as c:
        r=await c.post(f"{base}/v2/checkout/orders",headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"},
            json={"intent":"CAPTURE","purchase_units":[{"amount":{"currency_code":currency,"value":amount},"description":"OddsGPT Premium"}],
                  "application_context":{"return_url":f"{APP_URL}/payment-success","cancel_url":f"{APP_URL}"}})
    if r.status_code in(200,201):
        data=r.json()
        url=next((l["href"] for l in data.get("links",[]) if l.get("rel")=="approve"),"")
        return data.get("id",""),url
    raise Exception(f"PayPal order failed: {r.text}")

# App
init_db()
app=FastAPI(title="OddsGPT",version="7.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
app.mount("/static",StaticFiles(directory=FRONTEND_PATH),name="static")

class RegReq(BaseModel):    username:str; email:str; password:str; device_id:str=""
class LoginReq(BaseModel):  username:str; password:str; device_id:str=""
class ResetReq(BaseModel):  token:str; password:str
class CodeReq(BaseModel):   code:str
class ChatReq(BaseModel):   message:str; session_id:str="default"; sport_tab:str="all"
class SessReq(BaseModel):   sport_tab:str="all"
class BetReq(BaseModel):    bet_id:int; result:str
class EmailReq(BaseModel):  email:str
class BanReq(BaseModel):    username:str=""; device_id:str=""; reason:str; expires_at:str=""
class UnbanReq(BaseModel):  username:str=""; device_id:str=""
class RoleReq(BaseModel):   username:str; role:str
class CodeAddReq(BaseModel):code:str; grants_role:str="premium"; type:str="monthly"; expires_at:str=""
class SettingReq(BaseModel):key:str; value:str

@app.get("/")
async def root(): return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))
@app.get("/reset")
async def reset_page(): return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))
@app.get("/payment-success")
async def pay_ok(): return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))

@app.post("/api/auth/register")
async def register(req:RegReq,bg:BackgroundTasks):
    if len(req.username)<3: raise HTTPException(400,"Username min 3 characters")
    if len(req.password)<6 and req.password!=OPERATOR_PASSWORD: raise HTTPException(400,"Password min 6 characters")
    if get_user_by_username(req.username): raise HTTPException(400,"Username already taken")
    if get_user_by_email(req.email): raise HTTPException(400,"Email already registered")
    if req.device_id:
        if is_device_banned(req.device_id): raise HTTPException(403,"This device has been banned.")
        if get_device_count(req.device_id)>=MAX_DEVICES: raise HTTPException(400,f"This device already has {MAX_DEVICES} accounts.")
    try:
        user=create_user(req.username,req.email,req.password)
        if req.device_id: register_device(req.device_id,user["id"])
        tok=make_jwt(user["id"],user["username"],user["role"])
        if GMAIL_APP_PASS: bg.add_task(send_verify,req.email,user["verify_token"])
        return {"token":tok,"username":user["username"],"role":user["role"],"email_sent":bool(GMAIL_APP_PASS)}
    except Exception as e:
        if "UNIQUE" in str(e): raise HTTPException(400,"Username or email already exists")
        raise HTTPException(500,str(e))

@app.post("/api/auth/login")
async def login(req:LoginReq):
    user=get_user_by_username(req.username)
    if not user: raise HTTPException(401,"Invalid username or password")
    op_login=(user.get("role")=="operator" and req.password==OPERATOR_PASSWORD)
    if not op_login and not check_pw(req.password,user["password_hash"]): raise HTTPException(401,"Invalid username or password")
    ban_msg=check_ban(user)
    if ban_msg: raise HTTPException(403,f"Account banned: {ban_msg}")
    if req.device_id:
        if is_device_banned(req.device_id): raise HTTPException(403,"This device has been banned.")
        register_device(req.device_id,user["id"])
    user=refresh_user(user); update_user(user["id"],last_login=datetime.now().isoformat())
    tok=make_jwt(user["id"],user["username"],user["role"])
    return {"token":tok,"username":user["username"],"role":user["role"],"is_verified":bool(user["is_verified"])}

@app.get("/api/auth/verify/{token}")
async def verify_email(token:str):
    user=get_user_by_token(token,"verify_token")
    if not user: raise HTTPException(400,"Invalid link")
    update_user(user["id"],is_verified=1,verify_token=None)
    return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))

@app.post("/api/auth/resend-verify")
async def resend_verify(bg:BackgroundTasks,user=Depends(get_user)):
    if user.get("is_verified"): return {"message":"Already verified"}
    vtok=rand_tok(); update_user(user["id"],verify_token=vtok)
    bg.add_task(send_verify,user["email"],vtok)
    return {"message":"Verification email sent"}

@app.post("/api/auth/forgot")
async def forgot(req:EmailReq,bg:BackgroundTasks):
    user=get_user_by_email(req.email)
    if user:
        tok=rand_tok(); exp=(datetime.now()+timedelta(hours=1)).isoformat()
        update_user(user["id"],reset_token=tok,reset_expires=exp)
        bg.add_task(send_reset,req.email,tok)
    return {"message":"If that email exists, a reset link was sent."}

@app.post("/api/auth/reset")
async def reset_pw(req:ResetReq):
    user=get_user_by_token(req.token,"reset_token")
    if not user: raise HTTPException(400,"Invalid or expired link")
    if datetime.fromisoformat(user["reset_expires"])<datetime.now(): raise HTTPException(400,"Link expired")
    if len(req.password)<6: raise HTTPException(400,"Password min 6 characters")
    update_user(user["id"],password_plain=req.password,password_hash=hash_pw(req.password),reset_token=None,reset_expires=None)
    return {"message":"Password reset successfully."}

@app.post("/api/auth/apply-code")
async def apply_code_route(req:CodeReq,user=Depends(get_user)):
    ok,msg=apply_code(user,req.code)
    if not ok: raise HTTPException(400,msg)
    fresh=get_user_by_id(user["id"])
    return {"success":True,"message":msg,"new_token":make_jwt(fresh["id"],fresh["username"],fresh["role"]),"role":fresh["role"]}

@app.get("/api/auth/me")
async def me(user=Depends(get_user)):
    today=datetime.now().strftime("%Y-%m-%d")
    used=0 if user.get("last_reset_date","")!=today else user.get("daily_count",0)
    return {"username":user["username"],"role":user["role"],"is_verified":bool(user["is_verified"]),
            "daily_used":used,"daily_limit":FREE_LIMIT,"total_searches":user.get("total_searches",0),
            "premium_expires":user.get("premium_expires",""),"email":user.get("email","")}

@app.post("/api/chat")
async def chat(req:ChatReq,user=Depends(get_user)):
    try:
        if not check_usage(user):
            return JSONResponse({"response":f"⛔ Daily limit reached ({FREE_LIMIT}/day).\nEnter a promo code in Profile for Premium.\n\n⛔ Kufiri ditor u arrit ({FREE_LIMIT}/ditë).","confidence":None,"bets":[],"parlay":{},"limit_reached":True})
        history=db_get_history(req.session_id,user["id"])
        t1,t2,sport=extract_match(req.message)
        q=await needs_clarify(req.message,t1,t2)
        if q:
            db_save_message(req.session_id,user["id"],"user",req.message,req.sport_tab)
            db_save_message(req.session_id,user["id"],"assistant",f"❓ {q}",req.sport_tab)
            return JSONResponse({"response":f"❓ {q}","confidence":None,"bets":[],"parlay":{}})
        db_log_search(user["id"],req.message,sport)
        live=await search_match(t1,t2,sport)
        has_live=len(live)>100 and "No Tavily" not in live
        prompt=SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d %H:%M UTC"),sport=sport.upper(),live_data=live,sport_section=get_sport_section(sport))
        msgs=[{"role":"system","content":prompt}]
        for m in history[-4:]: msgs.append({"role":m["role"],"content":m["content"]})
        msgs.append({"role":"user","content":req.message})
        full=await groq_call(msgs,max_tokens=2500)
        bets=parse_bets(full)
        clean="\n".join(l for l in full.split("\n") if not l.strip().upper().startswith("BET:")).strip()
        prefix=f"🌐 *Live data searched — {t1} vs {t2}*\n\n" if has_live else "📚 *Training knowledge only*\n\n"
        final=prefix+clean
        db_save_message(req.session_id,user["id"],"user",req.message,req.sport_tab)
        db_save_message(req.session_id,user["id"],"assistant",final,req.sport_tab)
        if bets: db_save_bets(req.session_id,user["id"],bets)
        parlay=await build_parlay(bets,user["id"]) if len(bets)>=2 else {}
        return JSONResponse({"response":final,"confidence":max((b["confidence"] for b in bets),default=None),"bets":bets,"parlay":parlay})
    except Exception as e:
        print(f"[ERR] {e}")
        return JSONResponse({"response":f"⚠️ Error: {str(e)}","confidence":None,"bets":[],"parlay":{}})

@app.get("/api/sessions")
async def get_sessions(user=Depends(get_user)): return {"sessions":db_get_sessions(user["id"])}

@app.get("/api/sessions/{sid}")
async def get_session(sid:str,user=Depends(get_user)): return {"history":db_get_history(sid,user["id"]),"bets":db_get_bets(sid)}

@app.post("/api/sessions/new")
async def new_session(req:SessReq,user=Depends(get_user)): return {"session_id":db_new_session(user["id"],req.sport_tab)}

@app.delete("/api/sessions/{sid}")
async def delete_session(sid:str,user=Depends(get_user)):
    db_soft_delete(sid,user["id"]); return {"status":"deleted"}

@app.get("/api/dashboard")
async def dashboard(user=Depends(get_user)):
    bets=db_get_user_bets(user["id"]); parlays=db_get_parlays(user["id"])
    won=sum(1 for b in bets if b["result"]=="won"); lost=sum(1 for b in bets if b["result"]=="lost")
    return {"bets":bets,"parlays":parlays,"stats":{"total":len(bets),"won":won,"lost":lost,"pending":sum(1 for b in bets if b["result"]=="pending"),"winrate":round(won/max(won+lost,1)*100,1)}}

@app.post("/api/bets/result")
async def bet_result(req:BetReq,user=Depends(get_user)):
    if req.result not in("won","lost","pending"): raise HTTPException(400,"Invalid")
    db_update_result(req.bet_id,req.result,user["id"]); return {"status":"updated"}

@app.get("/api/hot-games")
async def hot_games(sport:str="all"): return {"games":await get_hot_games(sport)}

@app.get("/api/payment/settings")
async def pay_settings():
    return {"enabled":get_setting("payments_enabled")=="true","price":get_setting("premium_price"),"currency":get_setting("premium_currency"),"paypal_client_id":PAYPAL_CLIENT_ID}

@app.post("/api/payment/create-order")
async def create_order(user=Depends(get_user)):
    if get_setting("payments_enabled")!="true": raise HTTPException(400,"Payments disabled.")
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET: raise HTTPException(500,"PayPal not configured.")
    amount=get_setting("premium_price") or "9.99"; currency=get_setting("premium_currency") or "USD"
    try:
        order_id,approve_url=await create_pp_order(amount,currency)
        with get_conn() as c: c.execute("INSERT INTO payments (user_id,paypal_order_id,amount,currency,status,created_at) VALUES (?,?,?,?,?,?)",(user["id"],order_id,amount,currency,"pending",datetime.now().isoformat()))
        return {"order_id":order_id,"approve_url":approve_url,"amount":amount,"currency":currency}
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/api/payment/status")
async def pay_status(user=Depends(get_user)):
    with get_conn() as c: rows=c.execute("SELECT * FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 5",(user["id"],)).fetchall()
    return {"payments":[dict(r) for r in rows]}

@app.get("/api/staff/users")
async def sf_users(u=Depends(staff_user)):
    with get_conn() as c: rows=c.execute("SELECT id,username,email,role,is_verified,daily_count,total_searches,premium_expires,is_banned,ban_reason,created_at,last_login FROM users ORDER BY created_at DESC").fetchall()
    return {"users":[dict(r) for r in rows]}

@app.get("/api/staff/searches/{uid}")
async def sf_searches(uid:str,u=Depends(staff_user)):
    with get_conn() as c: rows=c.execute("SELECT query,sport,created_at FROM search_log WHERE user_id=? ORDER BY created_at DESC LIMIT 300",(uid,)).fetchall()
    return {"searches":[dict(r) for r in rows]}

@app.get("/api/staff/sessions")
async def sf_sessions(u=Depends(staff_user)):
    with get_conn() as c: rows=c.execute("SELECT s.id,s.sport_tab,s.title,s.updated_at,s.hidden,u.username,u.role FROM sessions s JOIN users u ON s.user_id=u.id ORDER BY s.updated_at DESC LIMIT 300").fetchall()
    return {"sessions":[dict(r) for r in rows]}

@app.get("/api/staff/session/{sid}")
async def sf_session(sid:str,u=Depends(staff_user)):
    with get_conn() as c:
        msgs=c.execute("SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY created_at",(sid,)).fetchall()
        bets=c.execute("SELECT * FROM saved_bets WHERE session_id=?",(sid,)).fetchall()
    return {"messages":[dict(m) for m in msgs],"bets":[dict(b) for b in bets]}

@app.get("/api/staff/hot-searched")
async def sf_hot(sport:str="all",u=Depends(staff_user)):
    return {"hot":db_get_hot_searched(sport if sport!="all" else None,20)}

@app.get("/api/staff/payments")
async def sf_payments(u=Depends(staff_user)):
    with get_conn() as c: rows=c.execute("SELECT p.*,u.username,u.email FROM payments p JOIN users u ON p.user_id=u.id ORDER BY p.created_at DESC LIMIT 100").fetchall()
    return {"payments":[dict(r) for r in rows]}

@app.get("/api/owner/users")
async def ow_users(u=Depends(owner_user)):
    with get_conn() as c: rows=c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return {"users":[dict(r) for r in rows]}

@app.post("/api/owner/set-role")
async def set_role(req:RoleReq,u=Depends(owner_user)):
    target=get_user_by_username(req.username)
    if not target: raise HTTPException(404,"User not found")
    if target["username"].lower()==OWNER_USERNAME.lower(): raise HTTPException(400,"Cannot modify the protected owner")
    if req.role not in("free","premium","operator","owner"): raise HTTPException(400,"Invalid role")
    update_user(target["id"],role=req.role); return {"status":f"Role '{req.role}' → {target['username']}"}

@app.post("/api/owner/ban")
async def ban_route(req:BanReq,u=Depends(owner_user)):
    exp=req.expires_at or None
    if req.username:
        target=get_user_by_username(req.username)
        if not target: raise HTTPException(404,"User not found")
        if target["username"].lower()==OWNER_USERNAME.lower(): raise HTTPException(400,"Cannot ban the owner")
        ban_user(target["id"],req.reason,exp,u["username"])
    if req.device_id: ban_device(req.device_id,req.reason,exp,u["username"])
    return {"status":"Banned"}

@app.post("/api/owner/unban")
async def unban_route(req:UnbanReq,u=Depends(owner_user)):
    if req.username:
        target=get_user_by_username(req.username)
        if not target: raise HTTPException(404,"User not found")
        unban_user(target["id"])
        return {"status":f"Unbanned {target['username']}"}
    if req.device_id:
        unban_device(req.device_id)
        return {"status":f"Device unbanned"}
    raise HTTPException(400,"Provide username or device_id")

@app.post("/api/owner/add-code")
async def add_code_r(req:CodeAddReq,u=Depends(owner_user)):
    code=req.code.strip().upper()
    eom=(datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1).isoformat()
    exp=req.expires_at or (eom if req.type=="monthly" else None)
    with get_conn() as c: c.execute("INSERT OR REPLACE INTO promo_codes VALUES (?,?,?,?,?)",(code,req.type,req.grants_role,exp,datetime.now().isoformat()))
    return {"status":f"Code '{code}' added"}

@app.get("/api/owner/codes")
async def ow_codes(u=Depends(owner_user)):
    with get_conn() as c: rows=c.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()
    return {"codes":[dict(r) for r in rows]}

@app.get("/api/owner/bans")
async def ow_bans(u=Depends(owner_user)):
    with get_conn() as c: rows=c.execute("SELECT bl.*,u.username FROM ban_log bl LEFT JOIN users u ON bl.user_id=u.id ORDER BY bl.created_at DESC LIMIT 200").fetchall()
    return {"bans":[dict(r) for r in rows]}

@app.post("/api/owner/setting")
async def upd_setting(req:SettingReq,u=Depends(owner_user)):
    if req.key not in("payments_enabled","premium_price","premium_currency"): raise HTTPException(400,"Invalid key")
    set_setting(req.key,req.value); return {"status":f"{req.key}={req.value}"}

@app.get("/api/owner/settings")
async def ow_settings(u=Depends(owner_user)):
    with get_conn() as c: rows=c.execute("SELECT * FROM settings").fetchall()
    return {"settings":{r["key"]:r["value"] for r in rows}}

@app.post("/api/owner/confirm-payment")
async def confirm_payment(data:dict,bg:BackgroundTasks,u=Depends(owner_user)):
    pid=data.get("payment_id")
    if not pid: raise HTTPException(400,"payment_id required")
    with get_conn() as c: row=c.execute("SELECT * FROM payments WHERE id=?",(pid,)).fetchone()
    if not row: raise HTTPException(404,"Payment not found")
    p=dict(row); target=get_user_by_id(p["user_id"])
    if not target: raise HTTPException(404,"User not found")
    eom=(datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1).isoformat()
    update_user(target["id"],role="premium",premium_source="paypal",premium_expires=eom)
    with get_conn() as c: c.execute("UPDATE payments SET status='confirmed',confirmed_at=?,confirmed_by=? WHERE id=?",(datetime.now().isoformat(),u["username"],pid))
    if GMAIL_APP_PASS: bg.add_task(send_payment_confirm,target["email"],target["username"],p["amount"])
    return {"status":f"Premium activated for {target['username']} until {eom[:10]}"}

@app.get("/api/health")
async def health(): return {"status":"online","model":GROQ_MODEL,"version":"7.0","db":os.path.exists(DB_PATH)}