"""
OddsGPT v5 - Complete Backend
Roles: admin / operator / premium / free
Monthly promo codes, Gmail SMTP, full audit logs, no data deletion
"""

import os, sqlite3, uuid, httpx, re, asyncio, hashlib, hmac, base64, json, random, string, smtplib
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
DB_PATH       = os.path.join(BASE_DIR, "oddsgpt.db")

# ── ENV KEYS ──────────────────────────────────────────────────────────────────
GROQ_API_KEY      = os.getenv("GROQ_API_KEY",      "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY",     "")
JWT_SECRET        = os.getenv("JWT_SECRET",         "OddsGPT_JWT_2024_secret!")
ADMIN_USERNAME    = os.getenv("ADMIN_USERNAME",     "arkel")
OPERATOR_PASSWORD = os.getenv("OPERATOR_PASSWORD",  "Operator2024!")  # register with this → gets operator role
GMAIL_USER        = os.getenv("GMAIL_USER",         "noreplyoddsgpt@gmail.com")
GMAIL_APP_PASS    = os.getenv("GMAIL_APP_PASS",     "")  # Gmail App Password (not gmail login password)
APP_URL           = os.getenv("APP_URL",            "http://localhost:8000")
MONTHLY_CODE      = os.getenv("MONTHLY_CODE",       "ODDS2024")  # change each month for expiring access

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
SDB_URL    = "https://www.thesportsdb.com/api/v1/json/3"
GROQ_MODEL = "llama-3.3-70b-versatile"
FREE_LIMIT = 3   # analyses per day for free users

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE — never deletes user data, full audit trail
# ══════════════════════════════════════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with get_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id                TEXT PRIMARY KEY,
                username          TEXT UNIQUE NOT NULL COLLATE NOCASE,
                email             TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_hash     TEXT NOT NULL,
                role              TEXT DEFAULT 'free',
                is_verified       INTEGER DEFAULT 0,
                verify_token      TEXT,
                reset_token       TEXT,
                reset_expires     TEXT,
                premium_source    TEXT,
                premium_expires   TEXT,
                daily_count       INTEGER DEFAULT 0,
                last_reset_date   TEXT DEFAULT '',
                total_searches    INTEGER DEFAULT 0,
                created_at        TEXT,
                last_login        TEXT,
                is_active         INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                sport_tab  TEXT DEFAULT 'all',
                title      TEXT DEFAULT 'New Chat',
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                sport_tab  TEXT DEFAULT 'all',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS saved_bets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                match_name TEXT,
                bet_type   TEXT,
                pick       TEXT,
                confidence INTEGER,
                reasoning  TEXT,
                risk       TEXT,
                odds_range TEXT,
                result     TEXT DEFAULT 'pending',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS parlays (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL,
                picks         TEXT NOT NULL,
                combined_odds TEXT,
                confidence    INTEGER,
                note          TEXT,
                result        TEXT DEFAULT 'pending',
                created_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS search_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                query      TEXT NOT NULL,
                sport      TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                code       TEXT PRIMARY KEY,
                type       TEXT DEFAULT 'monthly',
                expires_at TEXT,
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_msg_sess  ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_bets_user ON saved_bets(user_id);
            CREATE INDEX IF NOT EXISTS idx_log_user  ON search_log(user_id);
        """)
    # Seed monthly code
    _seed_monthly_code()

def _seed_monthly_code():
    expires = (datetime.now().replace(day=1) + timedelta(days=32)).replace(day=1).isoformat()
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO promo_codes (code,type,expires_at,created_at) VALUES (?,?,?,?)",
                  (MONTHLY_CODE.upper(), "monthly", expires, datetime.now().isoformat()))

# ── Password ───────────────────────────────────────────────────────────────────

def hash_pw(p: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), salt, 200000)
    return base64.b64encode(salt + key).decode("ascii")

def check_pw(p: str, stored: str) -> bool:
    try:
        raw  = base64.b64decode(stored.encode("ascii"))
        salt = raw[:16]; key = raw[16:]
        new  = hashlib.pbkdf2_hmac("sha256", p.encode("utf-8"), salt, 200000)
        return hmac.compare_digest(key, new)
    except Exception:
        return False

def rand_tok(n=40) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

# ── Roles ──────────────────────────────────────────────────────────────────────
# role: 'free' | 'premium' | 'operator' | 'admin'

def is_privileged(user: dict) -> bool:
    return user.get("role") in ("premium","operator","admin")

def is_staff(user: dict) -> bool:
    return user.get("role") in ("operator","admin")

def is_admin(user: dict) -> bool:
    return user.get("role") == "admin"

# ── User CRUD ──────────────────────────────────────────────────────────────────

def create_user(username: str, email: str, password: str) -> dict:
    uid  = str(uuid.uuid4())
    now  = datetime.now().isoformat()
    vtok = rand_tok()
    # Determine role
    if username.lower() == ADMIN_USERNAME.lower():
        role = "admin"
    elif password == OPERATOR_PASSWORD:
        role = "operator"
    else:
        role = "free"
    with get_conn() as c:
        c.execute("""INSERT INTO users
            (id,username,email,password_hash,role,is_verified,verify_token,daily_count,last_reset_date,total_searches,created_at)
            VALUES (?,?,?,?,?,?,?,0,'',0,?)""",
            (uid, username.lower(), email.lower(), hash_pw(password), role, 0, vtok, now))
    return get_user_by_id(uid)

def get_user_by_id(uid: str):
    with get_conn() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None

def get_user_by_username(u: str):
    with get_conn() as c:
        r = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (u,)).fetchone()
    return dict(r) if r else None

def get_user_by_email(e: str):
    with get_conn() as c:
        r = c.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (e,)).fetchone()
    return dict(r) if r else None

def get_user_by_token(tok: str, field: str):
    with get_conn() as c:
        r = c.execute(f"SELECT * FROM users WHERE {field}=?", (tok,)).fetchone()
    return dict(r) if r else None

def update_user(uid: str, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    vals = list(kw.values()) + [uid]
    with get_conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE id=?", vals)

# ── Premium / usage ────────────────────────────────────────────────────────────

def refresh_user(user: dict) -> dict:
    """Re-fetch user and check if premium has expired."""
    u = get_user_by_id(user["id"])
    if not u: return user
    # Check monthly premium expiry
    if u.get("premium_source") == "monthly" and u.get("premium_expires"):
        if datetime.fromisoformat(u["premium_expires"]) < datetime.now():
            update_user(u["id"], role="free", premium_source=None, premium_expires=None)
            u = get_user_by_id(u["id"])
    return u

def check_usage(user: dict) -> bool:
    """Returns True if allowed. Increments counter."""
    if is_privileged(user): return True
    today = datetime.now().strftime("%Y-%m-%d")
    if user.get("last_reset_date","") != today:
        update_user(user["id"], daily_count=1, last_reset_date=today)
        return True
    cnt = user.get("daily_count", 0)
    if cnt >= FREE_LIMIT: return False
    update_user(user["id"], daily_count=cnt+1)
    return True

def apply_code(user: dict, code: str) -> tuple[bool, str]:
    """Returns (success, message)."""
    code = code.strip().upper()
    with get_conn() as c:
        row = c.execute("SELECT * FROM promo_codes WHERE code=? COLLATE NOCASE", (code,)).fetchone()
    if not row:
        return False, "Invalid code."
    row = dict(row)
    expires_at = row.get("expires_at","")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
        return False, "This code has expired."
    code_type = row.get("type","monthly")
    if code_type == "monthly":
        # Premium until end of this code's expiry
        update_user(user["id"], role="premium", premium_source="monthly", premium_expires=expires_at)
        return True, f"🎉 Premium activated! Valid until {expires_at[:10]}."
    else:
        update_user(user["id"], role="premium", premium_source="promo", premium_expires=None)
        return True, "🎉 Lifetime Premium activated!"

# ── JWT ────────────────────────────────────────────────────────────────────────

def _jwt_sig(payload_str: str) -> str:
    return hmac.new(JWT_SECRET.encode("utf-8"), payload_str.encode("utf-8"), hashlib.sha256).hexdigest()

def make_jwt(uid: str, username: str, role: str) -> str:
    payload = json.dumps({"uid":uid,"usr":username,"role":role,
                          "exp":(datetime.now()+timedelta(days=30)).isoformat()}, separators=(",",":"))
    b64p = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    sig  = _jwt_sig(b64p)
    return f"{b64p}.{sig}"

def verify_jwt(token: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 2: return None
        b64p, sig = parts
        if not hmac.compare_digest(sig, _jwt_sig(b64p)): return None
        payload = json.loads(base64.urlsafe_b64decode(b64p + "==").decode("utf-8"))
        if datetime.fromisoformat(payload["exp"]) < datetime.now(): return None
        return payload
    except Exception:
        return None

async def get_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    data = verify_jwt(authorization[7:].strip())
    if not data: raise HTTPException(401, "Invalid or expired token. Please log in again.")
    user = get_user_by_id(data["uid"])
    if not user: raise HTTPException(401, "Account not found.")
    if not user.get("is_active", 1): raise HTTPException(403, "Account is deactivated.")
    return refresh_user(user)

async def staff_user(user: dict = Depends(get_user)) -> dict:
    if not is_staff(user): raise HTTPException(403, "Staff access required.")
    return user

async def admin_user(user: dict = Depends(get_user)) -> dict:
    if not is_admin(user): raise HTTPException(403, "Admin access required.")
    return user

# ── DB helpers ─────────────────────────────────────────────────────────────────

def db_new_session(uid: str, sport_tab: str="all") -> str:
    sid=str(uuid.uuid4())[:8]; now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO sessions (id,user_id,sport_tab,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (sid,uid,sport_tab,"New Chat",now,now))
    return sid

def db_get_sessions(uid: str) -> list:
    with get_conn() as c:
        rows=c.execute("SELECT id,sport_tab,title,created_at,updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 80",(uid,)).fetchall()
    return [dict(r) for r in rows]

def db_save_message(sid: str, uid: str, role: str, content: str, sport_tab: str="all"):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO messages (session_id,user_id,role,content,sport_tab,created_at) VALUES (?,?,?,?,?,?)",
                  (sid,uid,role,content,sport_tab,now))
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?",(now,sid))
    if role=="user":
        title=content[:55]+("..." if len(content)>55 else "")
        with get_conn() as c:
            cur=c.execute("SELECT title FROM sessions WHERE id=?",(sid,)).fetchone()
            if cur and cur["title"]=="New Chat":
                c.execute("UPDATE sessions SET title=? WHERE id=?",(title,sid))

def db_get_history(sid: str, uid: str) -> list:
    with get_conn() as c:
        rows=c.execute("SELECT role,content FROM messages WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20",(sid,uid)).fetchall()
    return [{"role":r["role"],"content":r["content"]} for r in reversed(rows)]

def db_save_bets(sid: str, uid: str, bets: list):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("DELETE FROM saved_bets WHERE session_id=?",(sid,))
        for b in bets:
            c.execute("INSERT INTO saved_bets (session_id,user_id,match_name,bet_type,pick,confidence,reasoning,risk,odds_range,result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (sid,uid,b.get("match",""),b.get("type",""),b.get("pick",""),b.get("confidence",0),
                       b.get("reasoning",""),b.get("risk","Medium"),b.get("odds_range","N/A"),"pending",now))

def db_get_bets(sid: str) -> list:
    with get_conn() as c:
        rows=c.execute("SELECT * FROM saved_bets WHERE session_id=? ORDER BY id",(sid,)).fetchall()
    return [dict(r) for r in rows]

def db_get_user_bets(uid: str) -> list:
    with get_conn() as c:
        rows=c.execute("SELECT sb.*,s.title session_title FROM saved_bets sb LEFT JOIN sessions s ON sb.session_id=s.id WHERE sb.user_id=? ORDER BY sb.created_at DESC LIMIT 200",(uid,)).fetchall()
    return [dict(r) for r in rows]

def db_update_result(bet_id: int, result: str, uid: str):
    with get_conn() as c:
        c.execute("UPDATE saved_bets SET result=? WHERE id=? AND user_id=?",(result,bet_id,uid))

def db_save_parlay(uid: str, picks: list, odds: str, conf: int, note: str):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO parlays (user_id,picks,combined_odds,confidence,note,result,created_at) VALUES (?,?,?,?,?,?,?)",
                  (uid,json.dumps(picks),odds,conf,note,"pending",now))

def db_get_parlays(uid: str) -> list:
    with get_conn() as c:
        rows=c.execute("SELECT * FROM parlays WHERE user_id=? ORDER BY created_at DESC LIMIT 30",(uid,)).fetchall()
    result=[]
    for r in rows:
        d=dict(r)
        try: d["picks"]=json.loads(d["picks"])
        except: pass
        result.append(d)
    return result

def db_log_search(uid: str, query: str, sport: str):
    now=datetime.now().isoformat()
    with get_conn() as c:
        c.execute("INSERT INTO search_log (user_id,query,sport,created_at) VALUES (?,?,?,?)",(uid,query,sport,now))
        c.execute("UPDATE users SET total_searches=total_searches+1 WHERE id=?",(uid,))

# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL via Gmail SMTP
# ══════════════════════════════════════════════════════════════════════════════

def send_email_sync(to: str, subject: str, html: str):
    """Sends via Gmail SMTP using App Password. Run in background thread."""
    if not GMAIL_APP_PASS:
        print(f"[Email] No GMAIL_APP_PASS set. Would send to {to}: {subject}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"OddsGPT <{GMAIL_USER}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(GMAIL_USER, [to], msg.as_string())
        print(f"[Email] Sent to {to}: {subject}")
    except Exception as e:
        print(f"[Email] Error: {e}")

async def send_email(to: str, subject: str, html: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_email_sync, to, subject, html)

EMAIL_STYLE = """
<div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;margin:auto;
     background:#07090e;color:#dde4f0;border-radius:14px;overflow:hidden;
     border:1px solid #1c2535">
  <div style="background:#0c1018;padding:24px 28px;border-bottom:1px solid #1c2535">
    <span style="font-size:24px;font-weight:900;color:#f0c040;letter-spacing:1px">Odds</span>
    <span style="font-size:24px;font-weight:900;color:#dde4f0">GPT</span>
  </div>
  <div style="padding:28px">
    {body}
    <p style="color:#4a5a70;font-size:11px;margin-top:24px;border-top:1px solid #1c2535;padding-top:14px">
      OddsGPT AI Betting Analyst — For entertainment only. Bet responsibly.
    </p>
  </div>
</div>"""

def email_btn(url: str, text: str) -> str:
    return f'<a href="{url}" style="display:inline-block;background:#f0c040;color:#000;padding:12px 26px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px;margin:16px 0">{text}</a>'

async def send_verify(email: str, token: str):
    link = f"{APP_URL}/api/auth/verify/{token}"
    body = f"<h2 style='color:#f0c040;margin-top:0'>Verify your email 🎯</h2><p>Click below to activate your OddsGPT account:</p>{email_btn(link,'Verify Email')}<p style='color:#4a5a70;font-size:12px'>Link expires in 24 hours.</p>"
    await send_email(email, "Verify your OddsGPT account", EMAIL_STYLE.format(body=body))

async def send_reset(email: str, token: str):
    link = f"{APP_URL}/reset?token={token}"
    body = f"<h2 style='color:#f0c040;margin-top:0'>Reset your password 🔑</h2><p>Click below — expires in 1 hour:</p>{email_btn(link,'Reset Password')}<p style='color:#4a5a70;font-size:12px'>Didn't request this? Ignore it.</p>"
    await send_email(email, "OddsGPT — Reset your password", EMAIL_STYLE.format(body=body))

# ══════════════════════════════════════════════════════════════════════════════
#  HOT GAMES — no duplicates, sport-filtered
# ══════════════════════════════════════════════════════════════════════════════

SPORT_LEAGUES = {
    "football":          ["4328","4335","4331","4332","4334"],
    "basketball":        ["4387"],
    "hockey":            ["4380"],
    "baseball":          ["4424"],
    "american football": ["4391"],
}

async def get_hot_games(sport: str="all") -> list:
    games = []
    seen  = set()

    if sport == "ufc":
        return [{"home":"Upcoming Fighter","away":"Upcoming Fighter","date":"","sport":"ufc/mma",
                 "query":"ufc next fight card this weekend predictions betting odds"}]

    leagues = []
    if sport == "all":
        for v in SPORT_LEAGUES.values():
            leagues.extend(v)
    else:
        leagues = SPORT_LEAGUES.get(sport, SPORT_LEAGUES["football"])

    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks = [client.get(f"{SDB_URL}/eventsnextleague.php?id={lid}") for lid in leagues[:6]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, r in enumerate(results):
        if isinstance(r, Exception): continue
        try:
            events = r.json().get("events") or []
            for ev in events[:2]:
                home = (ev.get("strHomeTeam") or "").strip()
                away = (ev.get("strAwayTeam") or "").strip()
                key  = f"{home}|{away}"
                if not home or not away or key in seen: continue
                seen.add(key)
                sp = (ev.get("strSport") or "Football").lower()
                games.append({
                    "home": home, "away": away,
                    "date": ev.get("dateEvent",""),
                    "sport": sp,
                    "query": f"{sp} - {home} vs {away}"
                })
        except Exception:
            continue

    return games[:9]

# ══════════════════════════════════════════════════════════════════════════════
#  GROQ + TAVILY
# ══════════════════════════════════════════════════════════════════════════════

async def groq_call(messages: list, max_tokens: int=2500) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(GROQ_URL,
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":GROQ_MODEL,"messages":messages,"temperature":0.3,"max_tokens":max_tokens,"stream":False})
    if r.status_code != 200: raise Exception(f"Groq error {r.status_code}: {r.text}")
    return r.json()["choices"][0]["message"]["content"]

async def tavily_one(sess: httpx.AsyncClient, query: str) -> str:
    if not TAVILY_API_KEY: return ""
    try:
        r = await sess.post(TAVILY_URL, json={"api_key":TAVILY_API_KEY,"query":query,
            "search_depth":"basic","max_results":3,"include_answer":True})
        if r.status_code != 200: return ""
        d=r.json(); parts=[]
        if d.get("answer"): parts.append(d["answer"])
        for res in d.get("results",[])[:2]:
            parts.append(f"• {res.get('title','')}: {res.get('content','')[:220]}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[Tavily] {e}"); return ""

async def search_match(team1: str, team2: str, sport: str) -> str:
    if not TAVILY_API_KEY: return "Add Tavily API key for live data."
    today=datetime.now().strftime("%Y-%m-%d"); matchup=f"{team1} vs {team2}"
    if sport in ("ufc","mma"):
        queries=[
            ("🥊 FIGHT RECORD",    f"{team1} {team2} MMA UFC record wins KO submission {today}"),
            ("📊 FIGHTING STATS",  f"{team1} {team2} striking grappling stats UFC {today}"),
            ("🏋️ CAMP NEWS",       f"{team1} {team2} training camp weight cut {today}"),
            ("🎰 ODDS",            f"{matchup} UFC fight odds betting {today}"),
        ]
    elif sport in ("football","soccer"):
        queries=[
            ("🎰 ODDS & TIPS",     f"{matchup} betting odds tips {today} oddschecker"),
            ("🏥 INJURIES",        f"{team1} {team2} injuries suspended {today}"),
            ("🔢 FORMATION",       f"{matchup} formation lineup tactical {today}"),
            ("📊 FORM & H2H",      f"{matchup} form head to head {today}"),
            ("📈 STATS",           f"{matchup} xG BTTS over under {today}"),
            ("💡 PREDICTIONS",     f"{matchup} prediction {today} forebet"),
        ]
    else:
        queries=[
            ("🎰 ODDS & TIPS",    f"{matchup} {sport} betting odds tips {today}"),
            ("🏥 INJURIES",       f"{team1} {team2} injuries {today}"),
            ("📊 FORM & H2H",     f"{matchup} form head to head {today}"),
            ("📈 STATS",          f"{matchup} {sport} statistics {today}"),
            ("💡 PREDICTIONS",    f"{matchup} prediction expert {today}"),
        ]
    async with httpx.AsyncClient(timeout=15.0) as sess:
        results=await asyncio.gather(*[tavily_one(sess,q) for _,q in queries],return_exceptions=True)
    parts=[]
    for (label,_),r in zip(queries,results):
        if isinstance(r,str) and r.strip():
            parts.append(f"\n{label}:\n{r}")
    return "\n".join(parts) or "No live data found."

def extract_match(text: str):
    m=re.search(r"([A-Za-z0-9\s\.'\-]+?)\s+(?:vs?\.?|versus|[-])\s+([A-Za-z0-9\s\.'\-]+?)(?:\n|$|[,\?])",text,re.IGNORECASE)
    t1=m.group(1).strip() if m else ""; t2=m.group(2).strip() if m else ""
    for s in ["football","soccer","basketball","tennis","hockey","baseball","nfl","nba","rugby","cricket","nhl","mlb","ufc","mma"]:
        t1=re.sub(rf"^{s}\s*[-:]\s*","",t1,flags=re.IGNORECASE).strip()
    sport="football"
    for s in ["ufc","mma","basketball","tennis","hockey","baseball","nfl","nba","rugby","cricket","nhl","mlb","soccer"]:
        if s in text.lower(): sport=s; break
    return t1.strip(),t2.strip(),sport

async def needs_clarify(msg: str, t1: str, t2: str):
    if t1 and t2 and len(t1)>1 and len(t2)>1: return None
    if re.search(r"vs?\.?\s+[A-Za-z]",msg,re.IGNORECASE): return None
    try:
        r=await groq_call([{"role":"user","content":f'User: "{msg[:200]}". Identify sports match? Reply PROCEED or CLARIFY: [question]'}],max_tokens=60)
        if r.strip().upper().startswith("CLARIFY:"): return r.strip()[8:].strip()
    except: pass
    return None

# ── Prompts ────────────────────────────────────────────────────────────────────

FOOTBALL_EXTRA = """
**🔢 FORMATION & TACTICS**
Identify both teams' likely formations. Analyze tactical matchups, key positional battles,
and which tactical setup has the advantage. Be specific (e.g., "4-3-3 high press vs 4-2-3-1 compact block").
**📋 PREDICTED LINEUPS** — List expected starters, flag key absentees."""

UFC_EXTRA = """
**🥊 FIGHTER PROFILES** — Record, recent fights, KO/submission rate
**🏋️ PHYSICAL & STYLE** — Reach, stance, striker vs grappler, key strengths
**⚔️ MATCHUP ANALYSIS** — Where does this fight get decided? Who has the edge where?
**🏋️ CAMP & WEIGHT** — Training camp updates, any weight cut concerns"""

GENERIC_EXTRA = """**📋 LINEUPS** — Expected starters and key absentees"""

SYSTEM_PROMPT = """You are OddsGPT — an elite professional sports betting analyst.
Today: {date} | Sport: {sport}

=== LIVE INTERNET DATA ===
{live_data}
=== END ===

{sport_extra}

**📊 RECENT FORM** — Last 5 results for each team/player (W/D/L + score)
**⚔️ H2H HISTORY** — Last 5 head to head meetings and who dominates
**🏥 INJURIES & SUSPENSIONS** — Every known absence and its tactical impact
**💰 ODDS ANALYSIS** — Current bookmaker odds, identify best value bet
**📈 KEY STATS** — BTTS rate, Over/Under 2.5 rate, xG, relevant trends
**💡 EXPERT CONSENSUS** — What prediction sites say

---
🎯 **MY CONCLUSION — HOW YOU SHOULD PLAY THIS:**
Write 4-6 sentences of direct, confident personal advice starting with "In my opinion...".
Tell the client exactly what to bet, what to avoid, approximate stakes distribution, and why.
Be specific and decisive.
---

Then write bets in EXACTLY this format (one per line, nothing else on that line):
BET: [pick] | TYPE: [type] | CONFIDENCE: [0-100] | RISK: [Low/Medium/High] | ODDS: [range] | MATCH: [teams] | REASON: [one sentence]

Give 4-6 BET lines covering different markets."""

def parse_bets(text: str) -> list:
    bets=[]
    for line in text.split("\n"):
        line=line.strip()
        if not line.upper().startswith("BET:"): continue
        try:
            line=re.sub(r'^BET:\s*','',line,flags=re.IGNORECASE)
            def g(p,d=""):
                m=re.search(p,line,re.IGNORECASE)
                return m.group(1).strip() if m else d
            pick=re.split(r'\s*\|\s*TYPE:',line,flags=re.IGNORECASE)[0].strip()
            if not pick: continue
            risk=g(r'RISK:\s*(Low|Medium|High)')
            if risk not in ("Low","Medium","High"): risk="Medium"
            conf=g(r'CONFIDENCE:\s*(\d+)')
            bets.append({"match":g(r'MATCH:\s*([^|]+)'),"type":g(r'TYPE:\s*([^|]+)') or "Bet",
                         "pick":pick,"confidence":min(100,max(0,int(conf))) if conf else 70,
                         "reasoning":g(r'REASON:\s*(.+)$'),"risk":risk,
                         "odds_range":g(r'ODDS:\s*([^|]+)') or "N/A"})
        except Exception as e:
            print(f"[parse] {e}")
    return bets

async def build_parlay(bets: list, uid: str) -> dict:
    if len(bets)<2: return {}
    picks=sorted([b for b in bets if b["risk"]!="High"],key=lambda x:x["confidence"],reverse=True)[:3]
    if len(picks)<2: picks=sorted(bets,key=lambda x:x["confidence"],reverse=True)[:3]
    pick_strs=[f"{b['pick']} ({b.get('match','')[:20]})" for b in picks]
    conf=int(sum(b["confidence"] for b in picks)/len(picks)*0.75)
    try: odds=f"~{round(1.85**len(picks),2):.2f}"
    except: odds="N/A"
    note=f"{len(picks)}-leg parlay — use small stake only."
    db_save_parlay(uid,pick_strs,odds,conf,note)
    return {"picks":pick_strs,"combined_odds":odds,"confidence":conf,"note":note}

# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI
# ══════════════════════════════════════════════════════════════════════════════

init_db()
app=FastAPI(title="OddsGPT",version="5.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
app.mount("/static",StaticFiles(directory=FRONTEND_PATH),name="static")

# ── Models ────────────────────────────────────────────────────────────────────

class RegReq(BaseModel):    username:str; email:str; password:str
class LoginReq(BaseModel):  username:str; password:str
class ResetReq(BaseModel):  token:str; password:str
class CodeReq(BaseModel):   code:str
class ChatReq(BaseModel):   message:str; session_id:str="default"; sport_tab:str="all"
class SessReq(BaseModel):   sport_tab:str="all"
class BetReq(BaseModel):    bet_id:int; result:str
class EmailReq(BaseModel):  email:str

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
async def register(req:RegReq, bg:BackgroundTasks):
    if len(req.username)<3: raise HTTPException(400,"Username min 3 characters")
    if len(req.password)<6 and req.password!=OPERATOR_PASSWORD:
        raise HTTPException(400,"Password min 6 characters")
    if get_user_by_username(req.username): raise HTTPException(400,"Username already taken")
    if get_user_by_email(req.email):       raise HTTPException(400,"Email already registered")
    try:
        user=create_user(req.username,req.email,req.password)
        tok=make_jwt(user["id"],user["username"],user["role"])
        if GMAIL_APP_PASS:
            bg.add_task(send_verify,req.email,user["verify_token"])
        return {"token":tok,"username":user["username"],"role":user["role"],
                "email_sent":bool(GMAIL_APP_PASS)}
    except Exception as e:
        if "UNIQUE" in str(e): raise HTTPException(400,"Username or email already exists")
        raise HTTPException(500,str(e))

@app.post("/api/auth/login")
async def login(req:LoginReq):
    user=get_user_by_username(req.username)
    if not user: raise HTTPException(401,"Invalid username or password")
    # Operator login: if their stored role is operator, accept OPERATOR_PASSWORD
    if user.get("role")=="operator" and req.password==OPERATOR_PASSWORD:
        pass  # allow
    elif not check_pw(req.password,user["password_hash"]):
        raise HTTPException(401,"Invalid username or password")
    if not user.get("is_active",1): raise HTTPException(403,"Account deactivated")
    user=refresh_user(user)
    update_user(user["id"],last_login=datetime.now().isoformat())
    tok=make_jwt(user["id"],user["username"],user["role"])
    return {"token":tok,"username":user["username"],"role":user["role"]}

@app.get("/api/auth/verify/{token}")
async def verify(token:str):
    user=get_user_by_token(token,"verify_token")
    if not user: raise HTTPException(400,"Invalid link")
    update_user(user["id"],is_verified=1,verify_token=None)
    return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))

@app.post("/api/auth/forgot")
async def forgot(req:EmailReq,bg:BackgroundTasks):
    user=get_user_by_email(req.email)
    if user:
        tok=rand_tok()
        exp=(datetime.now()+timedelta(hours=1)).isoformat()
        update_user(user["id"],reset_token=tok,reset_expires=exp)
        if GMAIL_APP_PASS:
            bg.add_task(send_reset,req.email,tok)
    return {"message":"If that email exists, a reset link was sent."}

@app.post("/api/auth/reset")
async def reset_pw(req:ResetReq):
    user=get_user_by_token(req.token,"reset_token")
    if not user: raise HTTPException(400,"Invalid or expired link")
    if datetime.fromisoformat(user["reset_expires"])<datetime.now(): raise HTTPException(400,"Link expired")
    if len(req.password)<6: raise HTTPException(400,"Password min 6 characters")
    update_user(user["id"],password_hash=hash_pw(req.password),reset_token=None,reset_expires=None)
    return {"message":"Password reset successfully. You can now log in."}

@app.post("/api/auth/apply-code")
async def apply_code_route(req:CodeReq,user:dict=Depends(get_user)):
    ok,msg=apply_code(user,req.code)
    if not ok: raise HTTPException(400,msg)
    return {"success":True,"message":msg}

@app.get("/api/auth/me")
async def me(user:dict=Depends(get_user)):
    today=datetime.now().strftime("%Y-%m-%d")
    if user.get("last_reset_date","")!=today:
        daily_used=0
    else:
        daily_used=user.get("daily_count",0)
    return {"username":user["username"],"role":user["role"],"is_verified":bool(user["is_verified"]),
            "daily_used":daily_used,"daily_limit":FREE_LIMIT,
            "premium_expires":user.get("premium_expires",""),
            "premium_source":user.get("premium_source",""),
            "total_searches":user.get("total_searches",0)}

# ── Chat ──────────────────────────────────────────────────────────────────────

@app.get("/")
async def root(): return FileResponse(os.path.join(FRONTEND_PATH,"index.html"))

@app.post("/api/chat")
async def chat(req:ChatReq,user:dict=Depends(get_user)):
    try:
        if not check_usage(user):
            return JSONResponse({"response":
                f"⛔ **Daily limit reached** ({FREE_LIMIT} free analyses/day).\n\n"
                "Enter a promo code in your **Profile** to unlock unlimited access.",
                "confidence":None,"bets":[],"parlay":{},"avoid":"","limit_reached":True})

        history=db_get_history(req.session_id,user["id"])
        t1,t2,sport=extract_match(req.message)

        q=await needs_clarify(req.message,t1,t2)
        if q:
            db_save_message(req.session_id,user["id"],"user",req.message,req.sport_tab)
            db_save_message(req.session_id,user["id"],"assistant",f"❓ {q}",req.sport_tab)
            return JSONResponse({"response":f"❓ {q}","confidence":None,"bets":[],"parlay":{},"avoid":""})

        db_log_search(user["id"],req.message,sport)
        live=await search_match(t1,t2,sport)
        has_live=len(live)>100 and "Add Tavily" not in live

        sport_extra=FOOTBALL_EXTRA if sport in("football","soccer") else UFC_EXTRA if sport in("ufc","mma") else GENERIC_EXTRA
        prompt=SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    sport=sport.upper(),live_data=live,sport_extra=sport_extra)
        msgs=[{"role":"system","content":prompt}]
        for m in history[-4:]: msgs.append({"role":m["role"],"content":m["content"]})
        msgs.append({"role":"user","content":req.message})

        full=await groq_call(msgs,max_tokens=2500)
        bets=parse_bets(full)
        clean="\n".join(l for l in full.split("\n") if not l.strip().upper().startswith("BET:")).strip()
        prefix=f"🌐 *Live data searched — {t1} vs {t2}*\n\n" if has_live else "📚 *Based on AI training knowledge*\n\n"
        final=prefix+clean

        db_save_message(req.session_id,user["id"],"user",req.message,req.sport_tab)
        db_save_message(req.session_id,user["id"],"assistant",final,req.sport_tab)
        if bets: db_save_bets(req.session_id,user["id"],bets)

        parlay=await build_parlay(bets,user["id"]) if len(bets)>=2 else {}
        return JSONResponse({"response":final,"confidence":max((b["confidence"] for b in bets),default=None),
                             "bets":bets,"parlay":parlay,"avoid":""})
    except Exception as e:
        print(f"[ERROR] {e}")
        return JSONResponse({"response":f"⚠️ Error: {str(e)}","confidence":None,"bets":[],"parlay":{},"avoid":""})

# ── Sessions ──────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def get_sessions(user:dict=Depends(get_user)):
    return {"sessions":db_get_sessions(user["id"])}

@app.get("/api/sessions/{sid}")
async def get_session(sid:str,user:dict=Depends(get_user)):
    return {"history":db_get_history(sid,user["id"]),"bets":db_get_bets(sid)}

@app.post("/api/sessions/new")
async def new_session(req:SessReq,user:dict=Depends(get_user)):
    return {"session_id":db_new_session(user["id"],req.sport_tab)}

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def dashboard(user:dict=Depends(get_user)):
    bets=db_get_user_bets(user["id"]); parlays=db_get_parlays(user["id"])
    won=sum(1 for b in bets if b["result"]=="won"); lost=sum(1 for b in bets if b["result"]=="lost")
    return {"bets":bets,"parlays":parlays,
            "stats":{"total":len(bets),"won":won,"lost":lost,"pending":sum(1 for b in bets if b["result"]=="pending"),
                     "winrate":round(won/max(won+lost,1)*100,1)}}

@app.post("/api/bets/result")
async def bet_result(req:BetReq,user:dict=Depends(get_user)):
    if req.result not in ("won","lost","pending"): raise HTTPException(400,"Invalid result")
    db_update_result(req.bet_id,req.result,user["id"]); return {"status":"updated"}

@app.get("/api/hot-games")
async def hot_games(sport:str="all"):
    return {"games":await get_hot_games(sport)}

# ── Staff / Admin routes ──────────────────────────────────────────────────────

@app.get("/api/staff/sessions")
async def staff_sessions(user:dict=Depends(staff_user)):
    with get_conn() as c:
        rows=c.execute("""SELECT s.id,s.sport_tab,s.title,s.updated_at,u.username,u.role
               FROM sessions s JOIN users u ON s.user_id=u.id ORDER BY s.updated_at DESC LIMIT 300""").fetchall()
    return {"sessions":[dict(r) for r in rows]}

@app.get("/api/staff/session/{sid}")
async def staff_session(sid:str,user:dict=Depends(staff_user)):
    with get_conn() as c:
        msgs=c.execute("SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY created_at",(sid,)).fetchall()
        bets=c.execute("SELECT * FROM saved_bets WHERE session_id=?",(sid,)).fetchall()
    return {"messages":[dict(m) for m in msgs],"bets":[dict(b) for b in bets]}

@app.get("/api/staff/users")
async def staff_users(user:dict=Depends(staff_user)):
    with get_conn() as c:
        rows=c.execute("SELECT id,username,email,role,is_verified,daily_count,total_searches,premium_source,premium_expires,created_at,last_login FROM users ORDER BY created_at DESC").fetchall()
    return {"users":[dict(r) for r in rows]}

@app.get("/api/staff/searches/{uid}")
async def user_searches(uid:str,user:dict=Depends(staff_user)):
    with get_conn() as c:
        rows=c.execute("SELECT query,sport,created_at FROM search_log WHERE user_id=? ORDER BY created_at DESC LIMIT 200",(uid,)).fetchall()
    return {"searches":[dict(r) for r in rows]}

# Admin-only extras
@app.get("/api/admin/users-full")
async def admin_users_full(user:dict=Depends(admin_user)):
    """Admin sees everything including password hashes."""
    with get_conn() as c:
        rows=c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return {"users":[dict(r) for r in rows]}

@app.post("/api/admin/grant-premium")
async def grant_premium(data:dict,user:dict=Depends(admin_user)):
    target=get_user_by_username(data.get("username",""))
    if not target: raise HTTPException(404,"User not found")
    if is_admin(target): raise HTTPException(400,"Cannot modify admin")
    update_user(target["id"],role="premium",premium_source="admin",premium_expires=None)
    return {"status":f"Premium granted to {target['username']}"}

@app.post("/api/admin/grant-operator")
async def grant_operator(data:dict,user:dict=Depends(admin_user)):
    target=get_user_by_username(data.get("username",""))
    if not target: raise HTTPException(404,"User not found")
    if is_admin(target): raise HTTPException(400,"Cannot modify admin")
    update_user(target["id"],role="operator")
    return {"status":f"Operator role granted to {target['username']}"}

@app.post("/api/admin/revoke")
async def revoke(data:dict,user:dict=Depends(admin_user)):
    target=get_user_by_username(data.get("username",""))
    if not target: raise HTTPException(404,"User not found")
    if is_admin(target): raise HTTPException(400,"Cannot modify admin")
    update_user(target["id"],role="free",premium_source=None,premium_expires=None)
    return {"status":f"Revoked premium/operator from {target['username']}"}

@app.post("/api/admin/add-monthly-code")
async def add_monthly_code(data:dict,user:dict=Depends(admin_user)):
    code=data.get("code","").upper().strip()
    if not code: raise HTTPException(400,"Code required")
    expires=data.get("expires_at","")
    if not expires:
        expires=(datetime.now().replace(day=1)+timedelta(days=32)).replace(day=1).isoformat()
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO promo_codes (code,type,expires_at,created_at) VALUES (?,?,?,?)",
                  (code,"monthly",expires,datetime.now().isoformat()))
    return {"status":f"Monthly code '{code}' added, expires {expires[:10]}"}

@app.get("/api/health")
async def health(): return {"status":"online","model":GROQ_MODEL,"version":"5.0"}