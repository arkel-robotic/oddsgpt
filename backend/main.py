"""
OddsGPT v3 - Full Backend
- User accounts (register/login with JWT)
- Chat isolation per user
- Admin access to all chats
- Better database (bets saved permanently)
- Hot/upcoming games from TheSportsDB
- Groq AI + Tavily live search
"""

import os, sqlite3, uuid, httpx, re, asyncio, hashlib, hmac, base64, json
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
FRONTEND_PATH = os.path.join(BASE_DIR, "..", "frontend")
DB_PATH       = os.path.join(BASE_DIR, "oddsgpt.db")

# ── API KEYS ──────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.getenv("GROQ_API_KEY",   "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY",  "")
JWT_SECRET     = os.getenv("JWT_SECRET",      "changeme123")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME",  "arkel")  # your admin username

GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL  = "https://api.tavily.com/search"
SDB_URL     = "https://www.thesportsdb.com/api/v1/json/3"
GROQ_MODEL  = "llama-3.3-70b-versatile"

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE — full schema with users, sessions, messages, saved bets
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
                id         TEXT PRIMARY KEY,
                username   TEXT UNIQUE NOT NULL,
                email      TEXT UNIQUE NOT NULL,
                password   TEXT NOT NULL,
                is_admin   INTEGER DEFAULT 0,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                sport_tab  TEXT DEFAULT 'all',
                title      TEXT DEFAULT 'New Chat',
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                sport_tab  TEXT DEFAULT 'all',
                created_at TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
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
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_bets_user        ON saved_bets(user_id);
        """)

# ── Users ──────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return base64.b64encode(salt + key).decode()

def verify_password(password: str, stored: str) -> bool:
    try:
        raw  = base64.b64decode(stored.encode())
        salt = raw[:16]
        key  = raw[16:]
        new  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
        return hmac.compare_digest(key, new)
    except Exception:
        return False

def create_user(username: str, email: str, password: str) -> dict:
    uid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    is_admin = 1 if username.lower() == ADMIN_USERNAME.lower() else 0
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (id,username,email,password,is_admin,created_at) VALUES (?,?,?,?,?,?)",
            (uid, username.lower(), email.lower(), hash_password(password), is_admin, now)
        )
    return {"id": uid, "username": username, "email": email, "is_admin": bool(is_admin)}

def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username.lower(),)
        ).fetchone()
    return dict(row) if row else None

def get_user_by_id(uid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None

# ── JWT (simple, no external lib) ─────────────────────────────────────────────

def make_token(user_id: str, username: str, is_admin: bool) -> str:
    payload = json.dumps({
        "uid": user_id, "usr": username, "adm": is_admin,
        "exp": (datetime.now() + timedelta(days=30)).isoformat()
    })
    sig = hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(f"{payload}|||{sig}".encode()).decode()

def verify_token(token: str) -> dict | None:
    try:
        decoded = base64.b64decode(token.encode()).decode()
        payload_str, sig = decoded.rsplit("|||", 1)
        expected = hmac.new(JWT_SECRET.encode(), payload_str.encode(), hashlib.sha256).hexdigest()
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
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    data  = verify_token(token)
    if not data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = get_user_by_id(data["uid"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

async def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ── Sessions ───────────────────────────────────────────────────────────────────

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

def db_all_sessions_admin() -> list:
    """Admin only — returns all sessions with username."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.id, s.sport_tab, s.title, s.created_at, s.updated_at, u.username
               FROM sessions s JOIN users u ON s.user_id = u.id
               ORDER BY s.updated_at DESC LIMIT 200"""
        ).fetchall()
    return [dict(r) for r in rows]

def db_delete_session(session_id: str, user_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM messages   WHERE session_id=? AND user_id=?", (session_id, user_id))
        conn.execute("DELETE FROM saved_bets WHERE session_id=? AND user_id=?", (session_id, user_id))
        conn.execute("DELETE FROM sessions   WHERE id=?         AND user_id=?", (session_id, user_id))

def db_save_message(session_id: str, user_id: str, role: str, content: str, sport_tab: str = "all"):
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (session_id,user_id,role,content,sport_tab,created_at) VALUES (?,?,?,?,?,?)",
            (session_id, user_id, role, content, sport_tab, now)
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
    if role == "user":
        title = content[:55] + ("..." if len(content) > 55 else "")
        with get_conn() as conn:
            cur = conn.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
            if cur and cur["title"] == "New Chat":
                conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))

def db_get_history(session_id: str, user_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role,content FROM messages WHERE session_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20",
            (session_id, user_id)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def db_save_bets(session_id: str, user_id: str, bets: list):
    """Save bets permanently so they never disappear."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        # Remove old bets for this session first
        conn.execute("DELETE FROM saved_bets WHERE session_id=?", (session_id,))
        for bet in bets:
            conn.execute(
                """INSERT INTO saved_bets
                   (session_id,user_id,match_name,bet_type,pick,confidence,reasoning,risk,odds_range,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (session_id, user_id,
                 bet.get("match",""), bet.get("type",""), bet.get("pick",""),
                 bet.get("confidence",0), bet.get("reasoning",""),
                 bet.get("risk","Medium"), bet.get("odds_range","N/A"), now)
            )

def db_get_bets(session_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM saved_bets WHERE session_id=? ORDER BY id",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]

# ══════════════════════════════════════════════════════════════════════════════
#  HOT / UPCOMING GAMES from TheSportsDB (free, no key needed)
# ══════════════════════════════════════════════════════════════════════════════

POPULAR_LEAGUES = {
    "football":   ["4328","4335","4331","4332","4334"],  # EPL, La Liga, Bundesliga, Serie A, Ligue 1
    "basketball": ["4387"],                               # NBA
    "hockey":     ["4380"],                               # NHL
    "baseball":   ["4424"],                               # MLB
    "tennis":     [],
}

async def get_hot_games(sport: str = "all") -> list:
    """Fetches upcoming real games from TheSportsDB."""
    games  = []
    leagues = []

    if sport == "all":
        for v in POPULAR_LEAGUES.values():
            leagues.extend(v)
    else:
        leagues = POPULAR_LEAGUES.get(sport, POPULAR_LEAGUES["football"])

    async with httpx.AsyncClient(timeout=10.0) as client:
        for league_id in leagues[:4]:
            try:
                r = await client.get(f"{SDB_URL}/eventsnextleague.php?id={league_id}")
                if r.status_code == 200:
                    events = r.json().get("events") or []
                    for ev in events[:2]:
                        home  = ev.get("strHomeTeam","")
                        away  = ev.get("strAwayTeam","")
                        date  = ev.get("dateEvent","")
                        sport_name = ev.get("strSport","Football").lower()
                        if home and away:
                            games.append({
                                "home":  home,
                                "away":  away,
                                "date":  date,
                                "sport": sport_name,
                                "query": f"{sport_name} - {home} vs {away}"
                            })
            except Exception as e:
                print(f"[SDB] league {league_id}: {e}")

    return games[:8]

# ══════════════════════════════════════════════════════════════════════════════
#  GROQ
# ══════════════════════════════════════════════════════════════════════════════

async def groq_call(messages: list, max_tokens: int = 2000) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": messages,
                  "temperature": 0.3, "max_tokens": max_tokens, "stream": False}
        )
    if r.status_code != 200:
        raise Exception(f"Groq error {r.status_code}: {r.text}")
    return r.json()["choices"][0]["message"]["content"]

# ══════════════════════════════════════════════════════════════════════════════
#  TAVILY — parallel searches
# ══════════════════════════════════════════════════════════════════════════════

async def tavily_one(session: httpx.AsyncClient, query: str) -> str:
    if not TAVILY_API_KEY or TAVILY_API_KEY == "YOUR_TAVILY_API_KEY_HERE":
        return ""
    try:
        r = await session.post(TAVILY_URL, json={
            "api_key": TAVILY_API_KEY, "query": query,
            "search_depth": "basic", "max_results": 3, "include_answer": True
        })
        if r.status_code != 200:
            return ""
        data  = r.json()
        parts = []
        if data.get("answer"):
            parts.append(data["answer"])
        for res in data.get("results", [])[:2]:
            parts.append(f"• {res.get('title','')}: {res.get('content','')[:200]}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[Tavily] {e}")
        return ""

async def search_everything(team1: str, team2: str, sport: str) -> str:
    if not TAVILY_API_KEY or TAVILY_API_KEY == "YOUR_TAVILY_API_KEY_HERE":
        return "⚠️ No Tavily key. Add yours from app.tavily.com."
    today   = datetime.now().strftime("%Y-%m-%d")
    matchup = f"{team1} vs {team2}"
    queries = [
        ("🎰 ODDS & TIPS",        f"{matchup} betting odds tips {today} oddschecker"),
        ("🏥 INJURIES",           f"{team1} {team2} injuries suspended {today}"),
        ("📊 FORM & H2H",         f"{matchup} recent form head to head {today}"),
        ("📋 LINEUPS & NEWS",     f"{matchup} predicted lineup news {today}"),
        ("📈 STATS",              f"{matchup} xG BTTS over under stats {today}"),
        ("💡 PREDICTIONS",        f"{matchup} prediction {today} forebet"),
    ]
    async with httpx.AsyncClient(timeout=15.0) as session:
        results = await asyncio.gather(
            *[tavily_one(session, q) for _, q in queries],
            return_exceptions=True
        )
    parts = []
    for (label, _), r in zip(queries, results):
        if isinstance(r, str) and r.strip():
            parts.append(f"\n{label}:\n{r}")
    return "\n".join(parts) or "No live data found."

def extract_match_info(text: str):
    m = re.search(
        r"([A-Za-z0-9\s\.]+?)\s+(?:vs?\.?|versus|-)\s+([A-Za-z0-9\s\.]+?)(?:\n|$|[,\?])",
        text, re.IGNORECASE)
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
            f'User sent: "{message}". Can you identify a sports match? Reply PROCEED or CLARIFY: [question]'}],
            max_tokens=60)
        if resp.strip().upper().startswith("CLARIFY:"):
            return resp.strip()[8:].strip()
    except Exception:
        pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ANALYSIS PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are OddsGPT — a professional sports betting analyst.
Today: {date}

=== LIVE DATA ===
{live_data}
=== END ===

Write a clear analysis covering:
**📊 FORM** — Last 5 results each team
**⚔️ H2H** — Head to head history
**🏥 INJURIES** — All missing/doubtful players
**📈 STATS** — xG, BTTS, Over 2.5, key trends
**💰 ODDS** — Current odds and value

---
🎯 **MY CONCLUSION — HOW YOU SHOULD PLAY THIS:**
Give 3-5 sentences of direct personal advice to the client. Start with "In my opinion..." Tell them exactly what to bet, what to avoid, and the reasoning. Be confident and clear.
---

Then write bet recommendations EXACTLY in this format (one per line):
BET: [pick] | TYPE: [type] | CONFIDENCE: [0-100] | RISK: [Low/Medium/High] | ODDS: [range] | MATCH: [teams] | REASON: [sentence]

Give 4-5 BET lines."""

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
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

init_db()
app = FastAPI(title="OddsGPT", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=FRONTEND_PATH), name="static")

# ── Auth Models ───────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    email:    str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class ChatRequest(BaseModel):
    message:    str
    session_id: str = "default"
    sport_tab:  str = "all"

class NewSessionRequest(BaseModel):
    sport_tab: str = "all"

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    if len(req.username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if get_user_by_username(req.username):
        raise HTTPException(400, "Username already taken")
    try:
        user  = create_user(req.username, req.email, req.password)
        token = make_token(user["id"], user["username"], user["is_admin"])
        return {"token": token, "username": user["username"], "is_admin": user["is_admin"]}
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(400, "Username or email already exists")
        raise HTTPException(500, str(e))

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    user = get_user_by_username(req.username)
    if not user or not verify_password(req.password, user["password"]):
        raise HTTPException(401, "Invalid username or password")
    token = make_token(user["id"], user["username"], bool(user["is_admin"]))
    return {"token": token, "username": user["username"], "is_admin": bool(user["is_admin"])}

@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"username": user["username"], "is_admin": bool(user["is_admin"])}

# ── Chat Routes ───────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(os.path.join(FRONTEND_PATH, "index.html"))

@app.post("/api/chat")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    try:
        history             = db_get_history(req.session_id, user["id"])
        team1, team2, sport = extract_match_info(req.message)

        question = await needs_clarification(req.message, team1, team2)
        if question:
            db_save_message(req.session_id, user["id"], "user",      req.message,       req.sport_tab)
            db_save_message(req.session_id, user["id"], "assistant", f"❓ {question}",  req.sport_tab)
            return JSONResponse({"response": f"❓ {question}", "confidence": None,
                                 "bets": [], "parlay": {}, "avoid": ""})

        print(f"[chat] {user['username']}: {team1} vs {team2} ({sport})")

        live_data = await search_everything(team1, team2, sport)
        has_live  = len(live_data) > 100 and "⚠️" not in live_data

        prompt   = SYSTEM_PROMPT.format(
            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
            live_data=live_data
        )
        messages = [{"role": "system", "content": prompt}]
        for msg in history[-4:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": req.message})

        full_response = await groq_call(messages, max_tokens=2000)

        bets  = parse_bets(full_response)
        clean = "\n".join(l for l in full_response.split("\n")
                          if not l.strip().upper().startswith("BET:")).strip()

        prefix = (f"🌐 *Live data searched for {team1} vs {team2}*\n\n"
                  if has_live else "📚 *Based on training knowledge*\n\n")
        final  = prefix + clean

        db_save_message(req.session_id, user["id"], "user",      req.message, req.sport_tab)
        db_save_message(req.session_id, user["id"], "assistant", final,       req.sport_tab)

        # Save bets permanently to DB
        if bets:
            db_save_bets(req.session_id, user["id"], bets)

        return JSONResponse({
            "response":   final,
            "confidence": max((b["confidence"] for b in bets), default=None),
            "bets":       bets,
            "parlay":     {},
            "avoid":      ""
        })

    except Exception as e:
        print(f"[ERROR] {e}")
        return JSONResponse({
            "response":   f"⚠️ Error: {str(e)}",
            "confidence": None, "bets": [], "parlay": {}, "avoid": ""
        })

# ── Session Routes ────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def get_sessions(user: dict = Depends(get_current_user)):
    return {"sessions": db_get_sessions(user["id"])}

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str, user: dict = Depends(get_current_user)):
    history = db_get_history(session_id, user["id"])
    bets    = db_get_bets(session_id)
    return {"history": history, "bets": bets}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    db_delete_session(session_id, user["id"])
    return {"status": "deleted"}

@app.post("/api/sessions/new")
async def new_session(req: NewSessionRequest, user: dict = Depends(get_current_user)):
    return {"session_id": db_new_session(user["id"], req.sport_tab)}

# ── Hot Games Route ───────────────────────────────────────────────────────────

@app.get("/api/hot-games")
async def hot_games(sport: str = "all"):
    games = await get_hot_games(sport)
    return {"games": games}

# ── Admin Routes ──────────────────────────────────────────────────────────────

@app.get("/api/admin/sessions")
async def admin_sessions(user: dict = Depends(get_admin_user)):
    return {"sessions": db_all_sessions_admin()}

@app.get("/api/admin/session/{session_id}")
async def admin_get_session(session_id: str, user: dict = Depends(get_admin_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY created_at",
            (session_id,)
        ).fetchall()
        bets = conn.execute(
            "SELECT * FROM saved_bets WHERE session_id=?", (session_id,)
        ).fetchall()
    return {
        "messages": [dict(r) for r in rows],
        "bets":     [dict(b) for b in bets]
    }

@app.get("/api/admin/users")
async def admin_users(user: dict = Depends(get_admin_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,username,email,is_admin,created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    return {"users": [dict(r) for r in rows]}

@app.get("/api/health")
async def health():
    return {"status": "online", "model": GROQ_MODEL, "version": "3.0"}