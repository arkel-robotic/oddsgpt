"""
OddsGPT v6 - Complete Backend
- Owner / Operator / Premium / Free roles
- Device fingerprint limiting (max 2 accounts per device)
- Ban system (account / device / timed)
- Monthly expiring promo codes + permanent OWNER/OPERATOR codes
- Gmail SMTP email confirmation
- Live data (Tavily) - lineups, injuries, form, match date/time
- Persistent SQLite
- Passwords visible to owner
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
JWT_SECRET        = os.getenv("JWT_SECRET",         "OddsGPT_v6_secret_2025!")
OWNER_USERNAME    = os.getenv("OWNER_USERNAME",     "arkel")
OPERATOR_PASSWORD = os.getenv("OPERATOR_PASSWORD",  "Operator2025!")
GMAIL_USER        = os.getenv("GMAIL_USER",         "noreplyoddsgpt@gmail.com")
GMAIL_APP_PASS    = os.getenv("GMAIL_APP_PASS",     "")
APP_URL           = os.getenv("APP_URL",            "http://localhost:8000")
MONTHLY_CODE      = os.getenv("MONTHLY_CODE",       "ODDS2025")
MAX_DEVICES       = int(os.getenv("MAX_DEVICES",    "2"))

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
SDB_URL    = "https://www.thesportsdb.com/api/v1/json/3"
GROQ_MODEL = "llama-3.3-70b-versatile"
FREE_LIMIT = 3

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

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
                created_at TEXT, updated_at TEXT
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
                reason TEXT, expires_at TEXT, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_msg_sess  ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_bets_user ON saved_bets(user_id);
            CREATE INDEX IF NOT EXISTS idx_log_user  ON search_log(user_id);
        """)
    _seed_codes()

def _seed_codes():
    now = datetime.now().isoformat()
    eom = (datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1,hour=0,minute=0,second=0).isoformat()
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO promo_codes VALUES (?,?,?,?,?)", (MONTHLY_CODE.upper(),"monthly","premium",eom,now))
        c.execute("INSERT OR IGNORE INTO promo_codes VALUES (?,?,?,?,?)", ("OWNER","permanent","owner",None,now))
        c.execute("INSERT OR IGNORE INTO promo_codes VALUES (?,?,?,?,?)", ("OPERATOR","permanent","operator",None,now))

def hash_pw(p):
    salt=os.urandom(16); key=hashlib.pbkdf2_hmac("sha256",p.encode(),salt,200000)
    return base64.b64encode(salt+key).decode()

def check_pw(p,stored):
    try:
        raw=base64.b64decode(stored.encode()); salt=raw[:16]; key=raw[16:]
        return hmac.compare_digest(key,hashlib.pbkdf2_hmac("sha256",p.encode(),salt,200000))
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
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO device_accounts VALUES (?,?,?)",(device_id,user_id,datetime.now().isoformat()))
    return True

def get_device_count(device_id):
    if not device_id: return 0
    with get_conn() as c: return c.execute("SELECT COUNT(DISTINCT user_id) FROM device_accounts WHERE device_id=?",(device_id,)).fetchone()[0]

def is_device_banned(device_id):
    if not device_id: return False
    now=datetime.now().isoformat()
    with get_conn() as c:
        r=c.execute("SELECT 1 FROM ban_log WHERE device_id=? AND (expires_at IS NULL OR expires_at>?) LIMIT 1",(device_id,now)).fetchone()
    return bool(r)

def ban_user(uid,reason,expires_at,by):
    update_user(uid,is_banned=1,ban_reason=reason,ban_expires=expires_at or "")
    with get_conn() as c:
        c.execute("INSERT INTO ban_log (user_id,banned_by,reason,expires_at,created_at) VALUES (?,?,?,?,?)",(uid,by,reason,expires_at,datetime.now().isoformat()))

def ban_device(device_id,reason,expires_at,by):
    with get_conn() as c:
        c.execute("INSERT INTO ban_log (device_id,banned_by,reason,expires_at,created_at) VALUES (?,?,?,?,?)",(device_id,by,reason,expires_at,datetime.now().isoformat()))

def check_ban(user):
    if not user.get("is_banned"): return None
    exp=user.get("ban_expires","")
    if exp and datetime.fromisoformat(exp)<datetime.now():
        update_user(user["id"],is_banned=0,ban_reason=None,ban_expires=None); return None
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
    if user.get("role")=="owner": return True,"✅ You already have the highest access."
    eom=(datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1).isoformat()
    exp_date=eom if row.get("type")=="monthly" else None
    update_user(user["id"],role=grants,premium_source=code.lower(),premium_expires=exp_date)
    msgs={"premium":f"🎉 Premium activated!{' Valid until '+exp_date[:10] if exp_date else ''}",
          "operator":"🔧 Operator access granted!","owner":"👑 Owner access granted!"}
    return True,msgs.get(grants,"✅ Access granted!")

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

def db_new_session(uid,sport_tab="all"):
    sid=str(uuid.uuid4())[:8]; now=datetime.now().isoformat()
    with get_conn() as c: c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",(sid,uid,sport_tab,"New Chat",now,now))
    return sid

def db_get_sessions(uid):
    with get_conn() as c: rows=c.execute("SELECT id,sport_tab,title,created_at,updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 80",(uid,)).fetchall()
    return [dict(r) for r in rows]

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
    with get_conn() as c: rows=c.execute("SELECT role,content FROM messages WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20",(sid,uid)).fetchall()
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
    with get_conn() as c: rows=c.execute("SELECT sb.*,s.title s_title FROM saved_bets sb LEFT JOIN sessions s ON sb.session_id=s.id WHERE sb.user_id=? ORDER BY sb.created_at DESC LIMIT 200",(uid,)).fetchall()
    return [dict(r) for r in rows]

def db_log_search(uid,query,sport):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO search_log (user_id,query,sport,created_at) VALUES (?,?,?,?)",(uid,query,sport,now))
        c.execute("UPDATE users SET total_searches=total_searches+1 WHERE id=?",(uid,))

def db_update_result(bet_id,result,uid):
    with get_conn() as c: c.execute("UPDATE saved_bets SET result=? WHERE id=? AND user_id=?",(result,bet_id,uid))

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

def _send_email_sync(to,subject,html):
    if not GMAIL_APP_PASS: print(f"[Email] No GMAIL_APP_PASS. Would send: {subject} to {to}"); return
    try:
        msg=MIMEMultipart("alternative"); msg["Subject"]=subject; msg["From"]=f"OddsGPT <{GMAIL_USER}>"; msg["To"]=to
        msg.attach(MIMEText(html,"html"))
        with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
            s.login(GMAIL_USER,GMAIL_APP_PASS); s.sendmail(GMAIL_USER,[to],msg.as_string())
    except Exception as e: print(f"[Email] {e}")

async def send_email(to,subject,html):
    loop=asyncio.get_event_loop()
    await loop.run_in_executor(None,_send_email_sync,to,subject,html)

ETPL="""<div style="font-family:'Segoe UI',sans-serif;max-width:520px;margin:auto;background:#07090e;color:#dde4f0;border-radius:14px;overflow:hidden;border:1px solid #1c2535">
<div style="background:#0c1018;padding:18px 24px;border-bottom:1px solid #1c2535"><span style="font-size:22px;font-weight:900;color:#f0c040">Odds</span><span style="font-size:22px;font-weight:900;color:#dde4f0">GPT</span></div>
<div style="padding:24px">{body}<p style="color:#4a5a70;font-size:11px;margin-top:18px;padding-top:12px;border-top:1px solid #1c2535">OddsGPT AI Betting Analyst. For entertainment only. / Vetëm për argëtim.</p></div></div>"""
def ebtn(u,t): return f'<a href="{u}" style="display:inline-block;background:#f0c040;color:#000;padding:11px 24px;border-radius:8px;text-decoration:none;font-weight:700;margin:14px 0">{t}</a>'

async def send_verify(email,token):
    link=f"{APP_URL}/api/auth/verify/{token}"
    body=f"<h2 style='color:#f0c040;margin-top:0'>Verify your email 🎯 / Verifiko email-in</h2><p>Click to activate your OddsGPT account:</p>{ebtn(link,'Verify / Verifiko')}<p style='color:#4a5a70;font-size:12px'>Expires 24h / Skadon 24h</p>"
    await send_email(email,"Verify your OddsGPT account",ETPL.format(body=body))

async def send_reset(email,token):
    link=f"{APP_URL}/reset?token={token}"
    body=f"<h2 style='color:#f0c040;margin-top:0'>Reset password 🔑 / Rivendos fjalëkalimin</h2>{ebtn(link,'Reset / Rivendos')}<p style='color:#4a5a70;font-size:12px'>Expires 1h</p>"
    await send_email(email,"OddsGPT - Reset password",ETPL.format(body=body))

SPORT_LEAGUES={"football":["4328","4335","4331","4332","4334"],"basketball":["4387"],"hockey":["4380"],"baseball":["4424"],"american football":["4391"]}

async def get_hot_games(sport="all"):
    if sport=="ufc": return [{"home":"Next UFC Fight","away":"TBD","date":"","time":"","sport":"ufc","query":"ufc next fight card predictions betting odds this weekend"}]
    leagues=[]
    if sport=="all":
        for v in SPORT_LEAGUES.values(): leagues.extend(v[:2])
    else: leagues=SPORT_LEAGUES.get(sport,SPORT_LEAGUES["football"])
    seen=set(); games=[]
    async with httpx.AsyncClient(timeout=10.0) as c:
        results=await asyncio.gather(*[c.get(f"{SDB_URL}/eventsnextleague.php?id={lid}") for lid in leagues[:6]],return_exceptions=True)
    for r in results:
        if isinstance(r,Exception): continue
        try:
            for ev in (r.json().get("events") or [])[:2]:
                h=(ev.get("strHomeTeam") or "").strip(); a=(ev.get("strAwayTeam") or "").strip()
                k=f"{h}|{a}"
                if not h or not a or k in seen: continue
                seen.add(k)
                sp=(ev.get("strSport") or "Football").lower()
                games.append({"home":h,"away":a,"date":ev.get("dateEvent",""),"time":ev.get("strTime",""),"sport":sp,"query":f"{sp} - {h} vs {a}"})
        except: continue
    return games[:9]

async def groq_call(messages,max_tokens=2500):
    async with httpx.AsyncClient(timeout=55.0) as c:
        r=await c.post(GROQ_URL,headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":GROQ_MODEL,"messages":messages,"temperature":0.25,"max_tokens":max_tokens,"stream":False})
    if r.status_code!=200: raise Exception(f"Groq error {r.status_code}: {r.text}")
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

async def search_match(team1,team2,sport):
    if not TAVILY_API_KEY: return "No Tavily key. Add at app.tavily.com for live data."
    today=datetime.now().strftime("%Y-%m-%d"); matchup=f"{team1} vs {team2}"
    if sport in("ufc","mma"):
        queries=[("📅 EVENT",f"{matchup} UFC event date time location {today}"),("🥊 RECORDS",f"{team1} {team2} UFC record history {today}"),("💪 STATS",f"{team1} {team2} striking grappling reach {today}"),("🏋️ CAMP",f"{team1} {team2} training camp weight cut {today}"),("🎰 ODDS",f"{matchup} UFC betting odds {today}")]
    elif sport in("football","soccer"):
        queries=[("📅 DATE/TIME",f"{matchup} match date time kickoff venue {today}"),("📋 LINEUPS",f"{matchup} confirmed lineup predicted starting XI {today}"),("🏥 INJURIES",f"{team1} {team2} injuries suspended OUT doubtful {today}"),("🔢 FORMATION",f"{matchup} formation tactics system {today}"),("📊 FORM & H2H",f"{matchup} form last 5 results head to head {today}"),("🎰 ODDS & TIPS",f"{matchup} betting odds tips value {today} oddschecker"),("📈 STATS",f"{matchup} xG BTTS over 2.5 goals corners stats {today}"),("📰 TEAM NEWS",f"{team1} {team2} latest squad news transfers current players {today}")]
    else:
        queries=[("📅 DATE/TIME",f"{matchup} {sport} date time {today}"),("🎰 ODDS & TIPS",f"{matchup} {sport} betting odds {today}"),("🏥 INJURIES",f"{team1} {team2} injuries {today}"),("📊 FORM & H2H",f"{matchup} form results {today}"),("📈 STATS",f"{matchup} {sport} stats {today}")]
    async with httpx.AsyncClient(timeout=15.0) as sess:
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
        r=await groq_call([{"role":"user","content":f'User: "{msg[:200]}". Identify sports match? Reply PROCEED or CLARIFY: [question]'}],max_tokens=60)
        if r.strip().upper().startswith("CLARIFY:"): return r.strip()[8:].strip()
    except: pass
    return None

SYSTEM_PROMPT="""You are OddsGPT — an elite professional sports betting analyst. Respond in ENGLISH.
Today: {date} | Sport: {sport}

⚠️ CRITICAL: Your training data about current squads is OUTDATED (players move clubs often).
USE ONLY the live data below for current rosters, injuries, lineups.
DO NOT rely on training knowledge for who plays where NOW.

=== LIVE DATA ===
{live_data}
=== END ===

{sport_section}

**📅 MATCH DATE & TIME** — Exact date, time, venue from live data. If not found: "Date not yet confirmed."
**📊 CURRENT FORM** — Last 5 results for each team/player (from live data only)
**⚔️ H2H** — Last 5 head to head meetings
**🏥 INJURIES & SUSPENSIONS** — ONLY players confirmed OUT/doubtful from live data. Do NOT guess.
**📋 LINEUPS** — Use confirmed/predicted lineups from live data. State if confirmed by the club or predicted.
**💰 ODDS** — Current odds. Identify best value bet.
**📈 KEY STATS** — BTTS rate, Over 2.5 rate, average goals/game, xG if available.
**💡 EXPERT TIPS** — What prediction sites/tipsters say.

---
🎯 MY CONCLUSION:
4-6 sentences of direct personal advice. Start with "In my opinion..." Be specific about markets, stakes, and reasoning.
---

Bet format (one per line at end):
BET: [pick] | TYPE: [type] | CONFIDENCE: [0-100] | RISK: [Low/Medium/High] | ODDS: [range] | MATCH: [teams] | REASON: [sentence]

Give 4-6 BET lines covering 1X2, BTTS, Over/Under, Handicap."""

FOOTBALL_EXTRA="""**🔢 FORMATION & TACTICS**
State both teams' formation from live data. Analyze tactical matchup, key positional battles, which system has the advantage.
**📋 LINEUPS** — List from live data. Note if confirmed by club or just predicted."""

UFC_EXTRA="""**🥊 FIGHTER RECORDS** — Current record from live data
**💪 MATCHUP** — Striker vs grappler advantage, where this fight gets decided
**🏋️ CAMP NEWS** — Weight cut, training updates from live data"""

GENERIC_EXTRA="""**📋 LINEUP** — Expected starters from live data. Note key absences."""

def get_sport_section(sport):
    if sport in("football","soccer"): return FOOTBALL_EXTRA
    if sport in("ufc","mma"):        return UFC_EXTRA
    return GENERIC_EXTRA

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
    pick_strs=[f"{b['pick']} ({b.get('match','')[:18]})" for b in picks]
    conf=int(sum(b["confidence"] for b in picks)/len(picks)*0.75)
    try: odds=f"~{round(1.85**len(picks),2):.2f}"
    except: odds="N/A"
    db_save_parlay(uid,pick_strs,odds,conf,f"{len(picks)}-leg parlay — small stake only.")
    return {"picks":pick_strs,"combined_odds":odds,"confidence":conf,"note":f"{len(picks)}-leg parlay — small stake only."}

init_db()
app=FastAPI(title="OddsGPT",version="6.0")
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
class RoleReq(BaseModel):   username:str; role:str
class CodeAddReq(BaseModel):code:str; grants_role:str="premium"; type:str="monthly"; expires_at:str=""

@app.get("/"); async def root(): return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))
@app.get("/reset"); async def reset_page(): return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))

@app.post("/api/auth/register")
async def register(req:RegReq,bg:BackgroundTasks):
    if len(req.username)<3: raise HTTPException(400,"Username min 3 characters")
    if len(req.password)<6 and req.password!=OPERATOR_PASSWORD: raise HTTPException(400,"Password min 6 characters")
    if get_user_by_username(req.username): raise HTTPException(400,"Username already taken")
    if get_user_by_email(req.email): raise HTTPException(400,"Email already registered")
    if req.device_id:
        if is_device_banned(req.device_id): raise HTTPException(403,"This device has been banned.")
        if get_device_count(req.device_id)>=MAX_DEVICES: raise HTTPException(400,f"This device already has {MAX_DEVICES} accounts. Maximum reached.")
    try:
        user=create_user(req.username,req.email,req.password)
        if req.device_id: register_device(req.device_id,user["id"])
        tok=make_jwt(user["id"],user["username"],user["role"])
        if GMAIL_APP_PASS: bg.add_task(send_verify,req.email,user["verify_token"])
        return {"token":tok,"username":user["username"],"role":user["role"],"email_sent":bool(GMAIL_APP_PASS),"needs_verify":bool(GMAIL_APP_PASS)}
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

@app.post("/api/auth/forgot")
async def forgot(req:EmailReq,bg:BackgroundTasks):
    user=get_user_by_email(req.email)
    if user:
        tok=rand_tok(); exp=(datetime.now()+timedelta(hours=1)).isoformat()
        update_user(user["id"],reset_token=tok,reset_expires=exp)
        if GMAIL_APP_PASS: bg.add_task(send_reset,req.email,tok)
    return {"message":"If that email exists, a reset link was sent."}

@app.post("/api/auth/reset")
async def reset_pw(req:ResetReq):
    user=get_user_by_token(req.token,"reset_token")
    if not user: raise HTTPException(400,"Invalid or expired link")
    if datetime.fromisoformat(user["reset_expires"])<datetime.now(): raise HTTPException(400,"Link expired")
    if len(req.password)<6: raise HTTPException(400,"Password min 6 characters")
    update_user(user["id"],password_plain=req.password,password_hash=hash_pw(req.password),reset_token=None,reset_expires=None)
    return {"message":"Password reset successfully. You can now log in."}

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

@app.get("/")
async def root2(): return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))

@app.post("/api/chat")
async def chat(req:ChatReq,user=Depends(get_user)):
    try:
        if not check_usage(user):
            return JSONResponse({"response":f"⛔ **Daily limit reached** ({FREE_LIMIT} analyses/day).\n\nEnter a promo code in your **Profile** to unlock Premium.\n\n**⛔ Kufiri ditor u arrit** ({FREE_LIMIT} analiza/ditë). Fut kodin promo në **Profil**.","confidence":None,"bets":[],"parlay":{},"limit_reached":True})
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
        prefix=f"🌐 *Live data searched — {t1} vs {t2}*\n\n" if has_live else "📚 *Training knowledge only — add Tavily key for live data*\n\n"
        final=prefix+clean
        db_save_message(req.session_id,user["id"],"user",req.message,req.sport_tab)
        db_save_message(req.session_id,user["id"],"assistant",final,req.sport_tab)
        if bets: db_save_bets(req.session_id,user["id"],bets)
        parlay=await build_parlay(bets,user["id"]) if len(bets)>=2 else {}
        return JSONResponse({"response":final,"confidence":max((b["confidence"] for b in bets),default=None),"bets":bets,"parlay":parlay})
    except Exception as e:
        print(f"[ERR] {e}"); return JSONResponse({"response":f"⚠️ Error: {str(e)}","confidence":None,"bets":[],"parlay":{}})

@app.get("/api/sessions");  async def get_sessions(user=Depends(get_user)): return {"sessions":db_get_sessions(user["id"])}
@app.get("/api/sessions/{sid}"); async def get_session(sid:str,user=Depends(get_user)): return {"history":db_get_history(sid,user["id"]),"bets":db_get_bets(sid)}
@app.post("/api/sessions/new"); async def new_session(req:SessReq,user=Depends(get_user)): return {"session_id":db_new_session(user["id"],req.sport_tab)}

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

@app.get("/api/staff/users");   async def sf_users(u=Depends(staff_user)):
    with get_conn() as c: rows=c.execute("SELECT id,username,email,role,is_verified,daily_count,total_searches,premium_expires,is_banned,ban_reason,created_at,last_login FROM users ORDER BY created_at DESC").fetchall()
    return {"users":[dict(r) for r in rows]}

@app.get("/api/staff/searches/{uid}"); async def sf_searches(uid:str,u=Depends(staff_user)):
    with get_conn() as c: rows=c.execute("SELECT query,sport,created_at FROM search_log WHERE user_id=? ORDER BY created_at DESC LIMIT 300",(uid,)).fetchall()
    return {"searches":[dict(r) for r in rows]}

@app.get("/api/staff/sessions"); async def sf_sessions(u=Depends(staff_user)):
    with get_conn() as c: rows=c.execute("SELECT s.id,s.sport_tab,s.title,s.updated_at,u.username,u.role FROM sessions s JOIN users u ON s.user_id=u.id ORDER BY s.updated_at DESC LIMIT 300").fetchall()
    return {"sessions":[dict(r) for r in rows]}

@app.get("/api/staff/session/{sid}"); async def sf_session(sid:str,u=Depends(staff_user)):
    with get_conn() as c:
        msgs=c.execute("SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY created_at",(sid,)).fetchall()
        bets=c.execute("SELECT * FROM saved_bets WHERE session_id=?",(sid,)).fetchall()
    return {"messages":[dict(m) for m in msgs],"bets":[dict(b) for b in bets]}

@app.get("/api/owner/users"); async def ow_users(u=Depends(owner_user)):
    with get_conn() as c: rows=c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return {"users":[dict(r) for r in rows]}

@app.post("/api/owner/set-role")
async def set_role(req:RoleReq,u=Depends(owner_user)):
    target=get_user_by_username(req.username)
    if not target: raise HTTPException(404,"User not found")
    if target["username"].lower()==OWNER_USERNAME.lower(): raise HTTPException(400,"Cannot modify the protected owner account")
    if req.role not in("free","premium","operator","owner"): raise HTTPException(400,"Invalid role")
    update_user(target["id"],role=req.role); return {"status":f"Role '{req.role}' set for {target['username']}"}

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
async def unban_route(data:dict,u=Depends(owner_user)):
    target=get_user_by_username(data.get("username",""))
    if not target: raise HTTPException(404,"Not found")
    unban_user(target["id"]); return {"status":f"Unbanned {target['username']}"}

@app.post("/api/owner/add-code")
async def add_code_route(req:CodeAddReq,u=Depends(owner_user)):
    code=req.code.strip().upper()
    eom=(datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1).isoformat()
    exp=req.expires_at or (eom if req.type=="monthly" else None)
    with get_conn() as c: c.execute("INSERT OR REPLACE INTO promo_codes VALUES (?,?,?,?,?)",(code,req.type,req.grants_role,exp,datetime.now().isoformat()))
    return {"status":f"Code '{code}' added"}

@app.get("/api/owner/codes"); async def get_codes(u=Depends(owner_user)):
    with get_conn() as c: rows=c.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()
    return {"codes":[dict(r) for r in rows]}

@app.get("/api/owner/bans"); async def get_bans(u=Depends(owner_user)):
    with get_conn() as c: rows=c.execute("SELECT bl.*,u.username FROM ban_log bl LEFT JOIN users u ON bl.user_id=u.id ORDER BY bl.created_at DESC LIMIT 200").fetchall()
    return {"bans":[dict(r) for r in rows]}

@app.get("/api/health"); async def health(): return {"status":"online","model":GROQ_MODEL,"version":"6.0"}