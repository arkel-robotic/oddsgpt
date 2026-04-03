"""
OddsGPT v4 - Full Feature Backend
- Email confirmation + password reset (Resend)
- Deep formation & lineup analysis
- Personal bet history dashboard (mark wins/losses)
- Match alerts by email
- Live odds comparison table
- Parlay builder
- Premium tier (Stripe)
"""

import os, sqlite3, uuid, httpx, re, asyncio, hashlib, hmac, base64, json
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
FRONTEND_PATH = os.path.join(BASE_DIR, "..", "frontend")
DB_PATH       = os.path.join(BASE_DIR, "oddsgpt.db")

# ── API KEYS ──────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.getenv("GROQ_API_KEY",    "")
TAVILY_API_KEY  = os.getenv("TAVILY_API_KEY",  "")
JWT_SECRET      = os.getenv("JWT_SECRET",      "changeme_secret_123!")
ADMIN_USERNAME  = os.getenv("ADMIN_USERNAME",  "arkel")
RESEND_API_KEY  = os.getenv("RESEND_API_KEY",  "")   # resend.com - free 3000/month
STRIPE_SECRET   = os.getenv("STRIPE_SECRET",   "")   # stripe.com
STRIPE_WEBHOOK  = os.getenv("STRIPE_WEBHOOK",  "")   # stripe webhook secret
APP_URL         = os.getenv("APP_URL",         "http://localhost:8000")
FROM_EMAIL      = os.getenv("FROM_EMAIL",      "noreply@oddsgpt.com")
PREMIUM_PRICE   = os.getenv("STRIPE_PRICE_ID", "")   # Stripe price ID

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
SDB_URL    = "https://www.thesportsdb.com/api/v1/json/3"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                email         TEXT UNIQUE NOT NULL,
                password      TEXT NOT NULL,
                is_admin      INTEGER DEFAULT 0,
                is_premium    INTEGER DEFAULT 0,
                email_verified INTEGER DEFAULT 0,
                stripe_customer TEXT DEFAULT '',
                created_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS email_tokens (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                type       TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used       INTEGER DEFAULT 0
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

            CREATE TABLE IF NOT EXISTS alerts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                team       TEXT NOT NULL,
                sport      TEXT DEFAULT 'football',
                email      TEXT NOT NULL,
                active     INTEGER DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS parlays (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                matches    TEXT NOT NULL,
                analysis   TEXT,
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_bets_user        ON saved_bets(user_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_user      ON alerts(user_id);
        """)

# ── Auth helpers ──────────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100000)
    return base64.b64encode(salt + key).decode()

def verify_password(pw: str, stored: str) -> bool:
    try:
        raw  = base64.b64decode(stored.encode())
        salt, key = raw[:16], raw[16:]
        new  = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100000)
        return hmac.compare_digest(key, new)
    except Exception:
        return False

def make_token(uid: str, uname: str, is_admin: bool, is_premium: bool) -> str:
    payload = json.dumps({
        "uid": uid, "usr": uname, "adm": is_admin, "prm": is_premium,
        "exp": (datetime.now() + timedelta(days=30)).isoformat()
    })
    sig = hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(f"{payload}|||{sig}".encode()).decode()

def verify_token(token: str) -> dict | None:
    try:
        decoded     = base64.b64decode(token.encode()).decode()
        payload_str, sig = decoded.rsplit("|||", 1)
        expected    = hmac.new(JWT_SECRET.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(payload_str)
        if datetime.fromisoformat(payload["exp"]) < datetime.now():
            return None
        return payload
    except Exception:
        return None

async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    data = verify_token(authorization[7:])
    if not data:
        raise HTTPException(401, "Invalid or expired token")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (data["uid"],)).fetchone()
    if not row:
        raise HTTPException(401, "User not found")
    return dict(row)

async def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    return user

# ── User CRUD ──────────────────────────────────────────────────────────────────

def create_user(username: str, email: str, password: str) -> dict:
    uid      = str(uuid.uuid4())
    now      = datetime.now().isoformat()
    is_admin = 1 if username.lower() == ADMIN_USERNAME.lower() else 0
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (id,username,email,password,is_admin,created_at) VALUES (?,?,?,?,?,?)",
            (uid, username.lower(), email.lower(), hash_password(password), is_admin, now)
        )
    return {"id": uid, "username": username, "email": email,
            "is_admin": bool(is_admin), "is_premium": False, "email_verified": False}

def get_user_by(field: str, value: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(f"SELECT * FROM users WHERE {field}=?", (value,)).fetchone()
    return dict(row) if row else None

# ── Email tokens ───────────────────────────────────────────────────────────────

def create_email_token(user_id: str, token_type: str) -> str:
    token   = str(uuid.uuid4())
    expires = (datetime.now() + timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO email_tokens (token,user_id,type,expires_at) VALUES (?,?,?,?)",
            (token, user_id, token_type, expires)
        )
    return token

def use_email_token(token: str, token_type: str) -> str | None:
    """Returns user_id if valid, None if expired/used/wrong."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM email_tokens WHERE token=? AND type=? AND used=0",
            (token, token_type)
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return None
        conn.execute("UPDATE email_tokens SET used=1 WHERE token=?", (token,))
        return row["user_id"]

# ── Sessions / Messages / Bets ────────────────────────────────────────────────

def db_new_session(user_id: str, sport_tab: str = "all") -> str:
    sid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id,user_id,sport_tab,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, user_id, sport_tab, "New Chat", now, now)
        )
    return sid

def db_get_sessions(user_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,sport_tab,title,created_at,updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 60",
            (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]

def db_delete_session(sid: str, user_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM messages   WHERE session_id=? AND user_id=?", (sid, user_id))
        conn.execute("DELETE FROM saved_bets WHERE session_id=? AND user_id=?", (sid, user_id))
        conn.execute("DELETE FROM sessions   WHERE id=?         AND user_id=?", (sid, user_id))

def db_save_message(sid: str, user_id: str, role: str, content: str, sport_tab: str = "all"):
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (session_id,user_id,role,content,sport_tab,created_at) VALUES (?,?,?,?,?,?)",
            (sid, user_id, role, content, sport_tab, now)
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
    if role == "user":
        title = content[:55] + ("..." if len(content) > 55 else "")
        with get_conn() as conn:
            cur = conn.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
            if cur and cur["title"] == "New Chat":
                conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))

def db_get_history(sid: str, user_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role,content FROM messages WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20",
            (sid, user_id)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def db_save_bets(sid: str, user_id: str, bets: list):
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM saved_bets WHERE session_id=?", (sid,))
        for bet in bets:
            conn.execute(
                "INSERT INTO saved_bets (session_id,user_id,match_name,bet_type,pick,confidence,reasoning,risk,odds_range,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, user_id, bet.get("match",""), bet.get("type",""), bet.get("pick",""),
                 bet.get("confidence",0), bet.get("reasoning",""), bet.get("risk","Medium"),
                 bet.get("odds_range","N/A"), now)
            )

def db_get_bets(sid: str) -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM saved_bets WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    return [dict(r) for r in rows]

def db_get_all_user_bets(user_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT sb.*, s.title as session_title
               FROM saved_bets sb JOIN sessions s ON sb.session_id = s.id
               WHERE sb.user_id=? ORDER BY sb.created_at DESC LIMIT 200""",
            (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]

# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL — Resend.com (free 3000/month)
# ══════════════════════════════════════════════════════════════════════════════

async def send_email(to: str, subject: str, html: str):
    if not RESEND_API_KEY:
        print(f"[Email] No RESEND_API_KEY — would send to {to}: {subject}")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html}
            )
        if r.status_code not in [200, 201]:
            print(f"[Email] Error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[Email] {e}")

def email_verify_html(username: str, link: str) -> str:
    return f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;background:#07090e;color:#dde4f0;padding:32px;border-radius:12px">
      <h1 style="color:#f0c040;font-size:28px;margin-bottom:4px">OddsGPT</h1>
      <p style="color:#6a7a90;font-size:12px;margin-bottom:24px">AI BETTING ANALYST</p>
      <h2 style="color:#dde4f0">Verify your email, {username}!</h2>
      <p style="color:#6a7a90;line-height:1.6">Click the button below to activate your account.</p>
      <a href="{link}" style="display:inline-block;margin-top:20px;background:#f0c040;color:#000;font-weight:700;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:15px">Verify Email</a>
      <p style="color:#4a5a70;font-size:11px;margin-top:24px">Link expires in 24 hours. If you didn't register, ignore this email.</p>
    </div>"""

def email_reset_html(username: str, link: str) -> str:
    return f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;background:#07090e;color:#dde4f0;padding:32px;border-radius:12px">
      <h1 style="color:#f0c040;font-size:28px;margin-bottom:4px">OddsGPT</h1>
      <h2 style="color:#dde4f0">Reset your password</h2>
      <p style="color:#6a7a90;line-height:1.6">Hi {username}, click below to set a new password.</p>
      <a href="{link}" style="display:inline-block;margin-top:20px;background:#e05c20;color:#fff;font-weight:700;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:15px">Reset Password</a>
      <p style="color:#4a5a70;font-size:11px;margin-top:24px">Link expires in 24 hours. If you didn't request this, ignore it.</p>
    </div>"""

def email_alert_html(team: str, match: str, analysis: str) -> str:
    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:auto;background:#07090e;color:#dde4f0;padding:32px;border-radius:12px">
      <h1 style="color:#f0c040;font-size:24px">🔔 Match Alert — {match}</h1>
      <p style="color:#6a7a90">Your team <strong style="color:#f0c040">{team}</strong> has a game coming up!</p>
      <div style="background:#101520;border:1px solid #1c2535;border-radius:8px;padding:16px;margin-top:16px;white-space:pre-wrap;font-size:13px;line-height:1.6">{analysis}</div>
      <p style="color:#4a5a70;font-size:11px;margin-top:20px">OddsGPT — For entertainment only. Bet responsibly.</p>
    </div>"""

# ══════════════════════════════════════════════════════════════════════════════
#  STRIPE — Premium subscription
# ══════════════════════════════════════════════════════════════════════════════

async def create_stripe_session(user: dict) -> str:
    """Creates a Stripe checkout session and returns the URL."""
    if not STRIPE_SECRET:
        raise Exception("Stripe not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET, ""),
            data={
                "mode":                   "subscription",
                "line_items[0][price]":   PREMIUM_PRICE,
                "line_items[0][quantity]":"1",
                "success_url":            f"{APP_URL}/premium/success?session_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url":             f"{APP_URL}/?premium=cancelled",
                "customer_email":         user["email"],
                "metadata[user_id]":      user["id"],
            }
        )
    data = r.json()
    if r.status_code != 200:
        raise Exception(f"Stripe error: {data.get('error',{}).get('message','Unknown')}")
    return data["url"]

# ══════════════════════════════════════════════════════════════════════════════
#  GROQ
# ══════════════════════════════════════════════════════════════════════════════

async def groq_call(messages: list, max_tokens: int = 2500) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.3,
                  "max_tokens": max_tokens, "stream": False}
        )
    if r.status_code != 200:
        raise Exception(f"Groq error {r.status_code}: {r.text}")
    return r.json()["choices"][0]["message"]["content"]

# ══════════════════════════════════════════════════════════════════════════════
#  TAVILY — parallel searches
# ══════════════════════════════════════════════════════════════════════════════

async def tavily_one(session: httpx.AsyncClient, query: str) -> str:
    if not TAVILY_API_KEY:
        return ""
    try:
        r = await session.post(TAVILY_URL, json={
            "api_key": TAVILY_API_KEY, "query": query,
            "search_depth": "basic", "max_results": 4, "include_answer": True
        })
        if r.status_code != 200:
            return ""
        data  = r.json()
        parts = []
        if data.get("answer"):
            parts.append(data["answer"])
        for res in data.get("results", [])[:3]:
            parts.append(f"• {res.get('title','')}: {res.get('content','')[:250]}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[Tavily] {e}")
        return ""

async def search_match(team1: str, team2: str, sport: str) -> dict:
    """Returns categorised search results."""
    today   = datetime.now().strftime("%Y-%m-%d")
    matchup = f"{team1} vs {team2}"

    queries = {
        "odds":      f"{matchup} betting odds {today} site:oddschecker.com OR bet365 OR betway",
        "injuries":  f"{team1} {team2} injuries suspended out unavailable {today}",
        "lineups":   f"{matchup} predicted lineup formation {today}",
        "form":      f"{matchup} recent form last 5 results head to head {today}",
        "stats":     f"{matchup} xG BTTS over under stats {today}",
        "tips":      f"{matchup} prediction expert tips {today} forebet",
        "news":      f"{team1} {team2} team news {today}",
    }

    async with httpx.AsyncClient(timeout=15.0) as session:
        results = await asyncio.gather(
            *[tavily_one(session, q) for q in queries.values()],
            return_exceptions=True
        )

    return {k: (r if isinstance(r, str) else "") for k, r in zip(queries.keys(), results)}

async def search_odds_comparison(team1: str, team2: str) -> str:
    """Dedicated odds search from multiple bookmakers."""
    if not TAVILY_API_KEY:
        return ""
    today = datetime.now().strftime("%Y-%m-%d")
    queries = [
        f"{team1} vs {team2} odds bet365 {today}",
        f"{team1} vs {team2} odds betway william hill {today}",
        f"{team1} vs {team2} odds comparison oddschecker {today}",
    ]
    async with httpx.AsyncClient(timeout=15.0) as session:
        results = await asyncio.gather(*[tavily_one(session, q) for q in queries], return_exceptions=True)
    return "\n\n".join(r for r in results if isinstance(r, str) and r)

def extract_match_info(text: str):
    m = re.search(r"([A-Za-z0-9\s\.]+?)\s+(?:vs?\.?|versus|-)\s+([A-Za-z0-9\s\.]+?)(?:\n|$|[,\?])", text, re.IGNORECASE)
    team1 = m.group(1).strip() if m else ""
    team2 = m.group(2).strip() if m else ""
    for s in ["football","soccer","basketball","tennis","hockey","baseball","nfl","nba","rugby","cricket","nhl","mlb"]:
        team1 = re.sub(rf"^{s}\s*[-:]\s*", "", team1, flags=re.IGNORECASE).strip()
    sport = "football"
    for s in ["basketball","tennis","hockey","baseball","nfl","nba","rugby","cricket","nhl","mlb","soccer"]:
        if s in text.lower():
            sport = s
            break
    return team1.strip(), team2.strip(), sport

async def needs_clarification(message: str, team1: str, team2: str) -> str | None:
    if team1 and team2 and len(team1) > 1 and len(team2) > 1:
        return None
    if re.search(r"vs?\.?\s+[A-Za-z]", message, re.IGNORECASE):
        return None
    try:
        resp = await groq_call([{"role": "user", "content":
            f'User: "{message}". Identify sports match? Reply PROCEED or CLARIFY: [question]'}],
            max_tokens=60)
        if resp.strip().upper().startswith("CLARIFY:"):
            return resp.strip()[8:].strip()
    except Exception:
        pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  FORMATION & LINEUP ANALYSIS (premium feature)
# ══════════════════════════════════════════════════════════════════════════════

FORMATION_PROMPT = """You are an elite tactical football analyst.

LINEUP DATA:
{lineup_data}

Analyze the tactical matchup:

**🔲 FORMATIONS** — What formation is each team likely using? (4-3-3, 4-2-3-1, 3-5-2 etc)

**⚙️ TACTICAL STRENGTHS** — How does each team's formation exploit the opponent's weaknesses?

**🎯 KEY BATTLES** — Identify 2-3 crucial individual matchups that will decide the game (e.g. right winger vs left back)

**📐 SET PIECES** — Which team has the advantage from corners and free kicks?

**🔄 IN-GAME CHANGES** — What tactical adjustments should each manager make if losing?

**💡 TACTICAL VERDICT** — Based purely on formation and tactical analysis, who has the edge?

Be specific. Use player names if available from the lineup data."""

async def get_formation_analysis(lineup_data: str) -> str:
    if not lineup_data or len(lineup_data) < 50:
        return ""
    try:
        prompt   = FORMATION_PROMPT.format(lineup_data=lineup_data)
        messages = [{"role": "user", "content": prompt}]
        return await groq_call(messages, max_tokens=1200)
    except Exception as e:
        print(f"[Formation] {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  PARLAY BUILDER
# ══════════════════════════════════════════════════════════════════════════════

PARLAY_PROMPT = """You are OddsGPT, a professional parlay analyst.

The user wants to combine these matches into a parlay:
{matches}

LIVE DATA:
{live_data}

For each match, give a quick 2-3 line analysis and your best pick.

Then build the BEST parlay combination:

**🎰 RECOMMENDED PARLAY:**
List the picks in order from most to least confident.

**📊 COMBINED ANALYSIS:**
- Combined confidence: [X]%
- Combined odds estimate: [X.XX]
- Risk level: Low/Medium/High
- Why this parlay makes sense

**⚠️ SAFER ALTERNATIVE:**
If the full parlay is too risky, suggest a 2-3 leg version.

**❌ AVOID:**
Which match is the biggest risk in the parlay and why.

At the end, for each match list the pick:
BET: [pick] | TYPE: [type] | CONFIDENCE: [0-100] | RISK: [Low/Medium/High] | ODDS: [range] | MATCH: [teams] | REASON: [sentence]"""

async def build_parlay(matches: list, live_data: str) -> str:
    matches_text = "\n".join(f"- {m}" for m in matches)
    prompt       = PARLAY_PROMPT.format(matches=matches_text, live_data=live_data[:3000])
    messages     = [{"role": "user", "content": prompt}]
    return await groq_call(messages, max_tokens=2000)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ANALYSIS PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are OddsGPT — a professional sports betting analyst.
Today: {date}
User tier: {tier}

=== LIVE DATA ===
{live_data}
=== END ===

{formation_section}

Write a clear structured analysis:

**📊 FORM** — Last 5 results each team/player
**⚔️ H2H** — Head to head record
**🏥 INJURIES & SUSPENSIONS** — ALL missing players and impact
**📈 STATS** — xG, BTTS, Over 2.5, clean sheets, avg goals
**💰 ODDS** — Current best odds and value bet identified
**💡 EXPERT CONSENSUS** — What prediction sites say

{premium_section}

---
🎯 **MY CONCLUSION — HOW YOU SHOULD PLAY THIS:**
Give 4-6 sentences of direct personal advice. Start with "In my opinion...". Tell them exactly what to bet, what stake sizing to consider, what to avoid, and why. Be confident and specific.
---

Then list bets using EXACTLY this format (one per line, nothing else):
BET: [pick] | TYPE: [type] | CONFIDENCE: [0-100] | RISK: [Low/Medium/High] | ODDS: [range] | MATCH: [teams] | REASON: [sentence]

Give 4-5 BET lines covering different markets."""

def parse_bets(text: str) -> list:
    bets = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.upper().startswith("BET:"):
            continue
        try:
            line = re.sub(r'^BET:\s*', '', line, flags=re.IGNORECASE)
            def get(p, d=""):
                m = re.search(p, line, re.IGNORECASE)
                return m.group(1).strip() if m else d
            pick = re.split(r'\s*\|\s*TYPE:', line, flags=re.IGNORECASE)[0].strip()
            if not pick:
                continue
            risk = get(r'RISK:\s*(Low|Medium|High)')
            if risk not in ["Low","Medium","High"]:
                risk = "Medium"
            conf = get(r'CONFIDENCE:\s*(\d+)')
            bets.append({
                "match":      get(r'MATCH:\s*([^|]+)'),
                "type":       get(r'TYPE:\s*([^|]+)') or "Bet",
                "pick":       pick,
                "confidence": min(100, max(0, int(conf))) if conf else 70,
                "reasoning":  get(r'REASON:\s*(.+)$'),
                "risk":       risk,
                "odds_range": get(r'ODDS:\s*([^|]+)') or "N/A"
            })
        except Exception as e:
            print(f"[parse_bets] {e}")
    return bets

# ══════════════════════════════════════════════════════════════════════════════
#  HOT GAMES
# ══════════════════════════════════════════════════════════════════════════════

POPULAR_LEAGUES = {
    "football":   ["4328","4335","4331","4332","4334"],
    "basketball": ["4387"],
    "hockey":     ["4380"],
    "baseball":   ["4424"],
}

async def get_hot_games(sport: str = "all") -> list:
    games   = []
    leagues = []
    if sport == "all":
        for v in POPULAR_LEAGUES.values():
            leagues.extend(v)
    else:
        leagues = POPULAR_LEAGUES.get(sport, POPULAR_LEAGUES["football"])

    async with httpx.AsyncClient(timeout=10.0) as client:
        for lid in leagues[:4]:
            try:
                r = await client.get(f"{SDB_URL}/eventsnextleague.php?id={lid}")
                if r.status_code == 200:
                    for ev in (r.json().get("events") or [])[:2]:
                        home = ev.get("strHomeTeam","")
                        away = ev.get("strAwayTeam","")
                        if home and away:
                            games.append({
                                "home":  home, "away": away,
                                "date":  ev.get("dateEvent",""),
                                "sport": ev.get("strSport","Football").lower(),
                                "query": f"{ev.get('strSport','football').lower()} - {home} vs {away}"
                            })
            except Exception as e:
                print(f"[SDB] {e}")
    return games[:8]

# ══════════════════════════════════════════════════════════════════════════════
#  ALERTS — check and send
# ══════════════════════════════════════════════════════════════════════════════

async def check_and_send_alerts():
    """Called on demand or by a cron. Searches for matches for subscribed teams."""
    with get_conn() as conn:
        alerts = conn.execute("SELECT * FROM alerts WHERE active=1").fetchall()
    if not alerts:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    async with httpx.AsyncClient(timeout=15.0) as session:
        for alert in alerts:
            team  = alert["team"]
            query = f"{team} next match {today}"
            data  = await tavily_one(session, query)
            if data and len(data) > 50:
                # Quick AI summary
                try:
                    summary = await groq_call([{"role":"user","content":
                        f"In 3 sentences, summarize this match info for a bettor interested in {team}:\n{data[:800]}"}],
                        max_tokens=200)
                    await send_email(
                        alert["email"],
                        f"🔔 OddsGPT Alert — {team} upcoming match",
                        email_alert_html(team, f"{team} next game", summary)
                    )
                except Exception as e:
                    print(f"[Alert] {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

init_db()
app = FastAPI(title="OddsGPT", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=FRONTEND_PATH), name="static")

# ── Pydantic models ───────────────────────────────────────────────────────────

class RegisterReq(BaseModel):
    username: str
    email:    str
    password: str

class LoginReq(BaseModel):
    username: str
    password: str

class ResetRequestReq(BaseModel):
    email: str

class ResetPasswordReq(BaseModel):
    token:    str
    password: str

class ChatReq(BaseModel):
    message:    str
    session_id: str = "default"
    sport_tab:  str = "all"

class NewSessionReq(BaseModel):
    sport_tab: str = "all"

class BetResultReq(BaseModel):
    bet_id: int
    result: str  # "won" / "lost" / "void"

class AlertReq(BaseModel):
    team:  str
    sport: str = "football"

class ParlayReq(BaseModel):
    matches: list  # list of match strings

class OddsReq(BaseModel):
    team1: str
    team2: str

# ── Static pages ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(os.path.join(FRONTEND_PATH, "index.html"))

@app.get("/verify-email")
async def verify_email_page(token: str):
    uid = use_email_token(token, "verify")
    if not uid:
        return HTMLResponse("<h2>❌ Link expired or invalid. Please register again.</h2>")
    with get_conn() as conn:
        conn.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
    return HTMLResponse("""
    <html><head><meta http-equiv="refresh" content="3;url=/"></head>
    <body style="font-family:sans-serif;background:#07090e;color:#dde4f0;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center">
    <div><h2 style="color:#f0c040">✅ Email verified!</h2><p>Redirecting to OddsGPT...</p></div></body></html>""")

@app.get("/reset-password")
async def reset_password_page(token: str):
    return FileResponse(os.path.join(FRONTEND_PATH, "index.html"))

@app.get("/premium/success")
async def premium_success(session_id: str = ""):
    """Stripe redirects here after payment."""
    if session_id and STRIPE_SECRET:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
                    auth=(STRIPE_SECRET, "")
                )
            data = r.json()
            uid  = data.get("metadata", {}).get("user_id")
            if uid:
                with get_conn() as conn:
                    conn.execute("UPDATE users SET is_premium=1 WHERE id=?", (uid,))
        except Exception as e:
            print(f"[Stripe] {e}")
    return HTMLResponse("""
    <html><head><meta http-equiv="refresh" content="3;url=/"></head>
    <body style="font-family:sans-serif;background:#07090e;color:#dde4f0;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center">
    <div><h2 style="color:#f0c040">⭐ Welcome to Premium!</h2><p>Redirecting...</p></div></body></html>""")

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
async def register(req: RegisterReq, bg: BackgroundTasks):
    if len(req.username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if get_user_by("username", req.username):
        raise HTTPException(400, "Username already taken")
    if get_user_by("email", req.email.lower()):
        raise HTTPException(400, "Email already registered")
    try:
        user  = create_user(req.username, req.email, req.password)
        token = make_token(user["id"], user["username"], user["is_admin"], False)
        # Send verification email in background
        ver_token = create_email_token(user["id"], "verify")
        link      = f"{APP_URL}/verify-email?token={ver_token}"
        bg.add_task(send_email, req.email, "Verify your OddsGPT email",
                    email_verify_html(req.username, link))
        return {"token": token, "username": user["username"],
                "is_admin": user["is_admin"], "is_premium": False, "email_verified": False}
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(400, "Username or email already exists")
        raise HTTPException(500, str(e))

@app.post("/api/auth/login")
async def login(req: LoginReq):
    user = get_user_by("username", req.username)
    if not user or not verify_password(req.password, user["password"]):
        raise HTTPException(401, "Invalid username or password")
    token = make_token(user["id"], user["username"], bool(user["is_admin"]), bool(user["is_premium"]))
    return {"token": token, "username": user["username"],
            "is_admin": bool(user["is_admin"]), "is_premium": bool(user["is_premium"]),
            "email_verified": bool(user["email_verified"])}

@app.post("/api/auth/forgot-password")
async def forgot_password(req: ResetRequestReq, bg: BackgroundTasks):
    user = get_user_by("email", req.email.lower())
    if user:
        rst_token = create_email_token(user["id"], "reset")
        link      = f"{APP_URL}/reset-password?token={rst_token}"
        bg.add_task(send_email, req.email, "Reset your OddsGPT password",
                    email_reset_html(user["username"], link))
    return {"message": "If that email exists, a reset link has been sent."}

@app.post("/api/auth/reset-password")
async def reset_password(req: ResetPasswordReq):
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    uid = use_email_token(req.token, "reset")
    if not uid:
        raise HTTPException(400, "Reset link expired or invalid")
    with get_conn() as conn:
        conn.execute("UPDATE users SET password=? WHERE id=?", (hash_password(req.password), uid))
    return {"message": "Password updated successfully"}

@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"username": user["username"], "is_admin": bool(user["is_admin"]),
            "is_premium": bool(user["is_premium"]), "email_verified": bool(user["email_verified"])}

# ── Chat routes ───────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat(req: ChatReq, user: dict = Depends(get_current_user)):
    try:
        history             = db_get_history(req.session_id, user["id"])
        team1, team2, sport = extract_match_info(req.message)
        is_premium          = bool(user.get("is_premium"))

        question = await needs_clarification(req.message, team1, team2)
        if question:
            db_save_message(req.session_id, user["id"], "user",      req.message,      req.sport_tab)
            db_save_message(req.session_id, user["id"], "assistant", f"❓ {question}", req.sport_tab)
            return JSONResponse({"response": f"❓ {question}", "confidence": None,
                                 "bets": [], "odds_table": [], "formation_analysis": ""})

        print(f"[chat] {user['username']} ({'premium' if is_premium else 'free'}): {team1} vs {team2}")

        # Search the internet
        search_data = await search_match(team1, team2, sport)

        # Formation analysis for premium users
        formation_analysis = ""
        if is_premium and search_data.get("lineups"):
            formation_analysis = await get_formation_analysis(search_data["lineups"])

        live_data_str = "\n\n".join(
            f"{k.upper()}:\n{v}" for k, v in search_data.items() if v
        )

        formation_section = ""
        if formation_analysis:
            formation_section = f"\n=== TACTICAL & FORMATION ANALYSIS ===\n{formation_analysis}\n=== END TACTICAL ==="

        premium_section = ""
        if is_premium:
            premium_section = "**🔲 FORMATION & TACTICS** — Include tactical analysis based on the formation data above."

        prompt   = SYSTEM_PROMPT.format(
            date              = datetime.now().strftime("%Y-%m-%d %H:%M"),
            tier              = "Premium ⭐" if is_premium else "Free",
            live_data         = live_data_str or "No live data.",
            formation_section = formation_section,
            premium_section   = premium_section
        )
        messages = [{"role": "system", "content": prompt}]
        for msg in history[-4:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": req.message})

        full_response = await groq_call(messages, max_tokens=2500)

        bets  = parse_bets(full_response)
        clean = "\n".join(l for l in full_response.split("\n")
                          if not l.strip().upper().startswith("BET:")).strip()

        has_live = bool(search_data) and any(search_data.values())
        prefix   = f"🌐 *Live data searched for {team1} vs {team2}*\n\n" if has_live else "📚 *Based on training knowledge*\n\n"
        if is_premium and formation_analysis:
            prefix = f"⭐ *Premium analysis — Live data + Formation breakdown*\n\n"

        final = prefix + clean

        db_save_message(req.session_id, user["id"], "user",      req.message, req.sport_tab)
        db_save_message(req.session_id, user["id"], "assistant", final,       req.sport_tab)
        if bets:
            db_save_bets(req.session_id, user["id"], bets)

        return JSONResponse({
            "response":           final,
            "confidence":         max((b["confidence"] for b in bets), default=None),
            "bets":               bets,
            "formation_analysis": formation_analysis,
            "odds_raw":           search_data.get("odds","")
        })

    except Exception as e:
        print(f"[ERROR] {e}")
        return JSONResponse({"response": f"⚠️ Error: {str(e)}", "confidence": None,
                             "bets": [], "formation_analysis": "", "odds_raw": ""})

# ── Sessions ──────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def get_sessions(user: dict = Depends(get_current_user)):
    return {"sessions": db_get_sessions(user["id"])}

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str, user: dict = Depends(get_current_user)):
    return {"history": db_get_history(session_id, user["id"]), "bets": db_get_bets(session_id)}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    db_delete_session(session_id, user["id"])
    return {"status": "deleted"}

@app.post("/api/sessions/new")
async def new_session(req: NewSessionReq, user: dict = Depends(get_current_user)):
    return {"session_id": db_new_session(user["id"], req.sport_tab)}

# ── Bet history / dashboard ───────────────────────────────────────────────────

@app.get("/api/dashboard")
async def dashboard(user: dict = Depends(get_current_user)):
    bets    = db_get_all_user_bets(user["id"])
    total   = len(bets)
    won     = sum(1 for b in bets if b.get("result") == "won")
    lost    = sum(1 for b in bets if b.get("result") == "lost")
    pending = sum(1 for b in bets if b.get("result") == "pending")
    win_rate = round((won / (won+lost) * 100), 1) if (won+lost) > 0 else 0
    return {"bets": bets, "stats": {"total": total, "won": won, "lost": lost,
                                     "pending": pending, "win_rate": win_rate}}

@app.post("/api/bets/result")
async def update_bet_result(req: BetResultReq, user: dict = Depends(get_current_user)):
    if req.result not in ["won","lost","void","pending"]:
        raise HTTPException(400, "result must be won/lost/void/pending")
    with get_conn() as conn:
        conn.execute("UPDATE saved_bets SET result=? WHERE id=? AND user_id=?",
                     (req.result, req.bet_id, user["id"]))
    return {"status": "updated"}

# ── Odds comparison ───────────────────────────────────────────────────────────

@app.post("/api/odds")
async def get_odds(req: OddsReq, user: dict = Depends(get_current_user)):
    raw = await search_odds_comparison(req.team1, req.team2)
    if not raw:
        return {"odds_text": "No odds data found.", "bookmakers": []}
    # Ask AI to structure the odds
    try:
        structured = await groq_call([{"role":"user","content":
            f"Extract bookmaker odds from this text for {req.team1} vs {req.team2}. "
            f"List each bookmaker with Home/Draw/Away odds in format: 'Bookmaker: H X A'. "
            f"Text:\n{raw[:1500]}"}], max_tokens=400)
    except Exception:
        structured = raw[:500]
    return {"odds_text": structured, "odds_raw": raw}

# ── Parlay builder ────────────────────────────────────────────────────────────

@app.post("/api/parlay")
async def parlay_builder(req: ParlayReq, user: dict = Depends(get_current_user)):
    if len(req.matches) < 2:
        raise HTTPException(400, "Need at least 2 matches for a parlay")
    if len(req.matches) > 8:
        raise HTTPException(400, "Maximum 8 matches per parlay")

    # Search data for all matches
    all_data = []
    async with httpx.AsyncClient(timeout=15.0) as session:
        for match in req.matches[:4]:
            t1, t2, _ = extract_match_info(match)
            if t1 and t2:
                data = await tavily_one(session, f"{t1} vs {t2} prediction {datetime.now().strftime('%Y-%m-%d')}")
                if data:
                    all_data.append(f"=== {match} ===\n{data}")

    live_data = "\n\n".join(all_data)
    analysis  = await build_parlay(req.matches, live_data)
    bets      = parse_bets(analysis)

    clean = "\n".join(l for l in analysis.split("\n")
                      if not l.strip().upper().startswith("BET:")).strip()

    # Save parlay
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO parlays (user_id,matches,analysis,created_at) VALUES (?,?,?,?)",
            (user["id"], json.dumps(req.matches), clean, datetime.now().isoformat())
        )

    return {"analysis": clean, "bets": bets}

# ── Alerts ────────────────────────────────────────────────────────────────────

@app.post("/api/alerts")
async def create_alert(req: AlertReq, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM alerts WHERE user_id=? AND team=?",
                                (user["id"], req.team)).fetchone()
        if existing:
            raise HTTPException(400, f"Already subscribed to {req.team}")
        conn.execute(
            "INSERT INTO alerts (user_id,team,sport,email,created_at) VALUES (?,?,?,?,?)",
            (user["id"], req.team, req.sport, user["email"], datetime.now().isoformat())
        )
    return {"message": f"Alert created for {req.team}"}

@app.get("/api/alerts")
async def get_alerts(user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM alerts WHERE user_id=? AND active=1", (user["id"],)).fetchall()
    return {"alerts": [dict(r) for r in rows]}

@app.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: int, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET active=0 WHERE id=? AND user_id=?", (alert_id, user["id"]))
    return {"status": "deleted"}

@app.post("/api/alerts/send-now")
async def trigger_alerts(user: dict = Depends(get_admin_user)):
    """Admin triggers alert sending manually."""
    asyncio.create_task(check_and_send_alerts())
    return {"message": "Alerts queued"}

# ── Premium / Stripe ──────────────────────────────────────────────────────────

@app.post("/api/premium/checkout")
async def premium_checkout(user: dict = Depends(get_current_user)):
    if user.get("is_premium"):
        raise HTTPException(400, "Already premium")
    url = await create_stripe_session(user)
    return {"checkout_url": url}

@app.post("/api/stripe/webhook")
async def stripe_webhook(request_body: bytes = b"", authorization: Optional[str] = Header(None)):
    """Stripe calls this after successful payment."""
    try:
        body = await request_body
        data = json.loads(body)
        if data.get("type") == "checkout.session.completed":
            meta = data["data"]["object"].get("metadata", {})
            uid  = meta.get("user_id")
            if uid:
                with get_conn() as conn:
                    conn.execute("UPDATE users SET is_premium=1 WHERE id=?", (uid,))
    except Exception as e:
        print(f"[Stripe webhook] {e}")
    return {"status": "ok"}

# ── Hot games ─────────────────────────────────────────────────────────────────

@app.get("/api/hot-games")
async def hot_games(sport: str = "all"):
    return {"games": await get_hot_games(sport)}

# ── Admin ─────────────────────────────────────────────────────────────────────

@app.get("/api/admin/sessions")
async def admin_sessions(user: dict = Depends(get_admin_user)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.id, s.sport_tab, s.title, s.updated_at, u.username, u.is_premium
               FROM sessions s JOIN users u ON s.user_id = u.id
               ORDER BY s.updated_at DESC LIMIT 200"""
        ).fetchall()
    return {"sessions": [dict(r) for r in rows]}

@app.get("/api/admin/session/{session_id}")
async def admin_session(session_id: str, user: dict = Depends(get_admin_user)):
    with get_conn() as conn:
        msgs = conn.execute(
            "SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY created_at",
            (session_id,)).fetchall()
        bets = conn.execute("SELECT * FROM saved_bets WHERE session_id=?", (session_id,)).fetchall()
    return {"messages": [dict(r) for r in msgs], "bets": [dict(b) for b in bets]}

@app.get("/api/admin/stats")
async def admin_stats(user: dict = Depends(get_admin_user)):
    with get_conn() as conn:
        total_users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        premium_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
        total_sessions= conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        total_bets    = conn.execute("SELECT COUNT(*) FROM saved_bets").fetchone()[0]
        won_bets      = conn.execute("SELECT COUNT(*) FROM saved_bets WHERE result='won'").fetchone()[0]
    return {"total_users": total_users, "premium_users": premium_users,
            "total_sessions": total_sessions, "total_bets": total_bets, "won_bets": won_bets}

@app.get("/api/admin/users")
async def admin_users(user: dict = Depends(get_admin_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,username,email,is_admin,is_premium,email_verified,created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return {"users": [dict(r) for r in rows]}

@app.get("/api/health")
async def health():
    return {"status": "online", "model": GROQ_MODEL, "version": "4.0"}