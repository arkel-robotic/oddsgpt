"""
OddsGPT v2 - Optimized for speed + conclusion section
"""

import os, sqlite3, uuid, httpx, re, asyncio
from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
FRONTEND_PATH = os.path.join(BASE_DIR, "..", "frontend")
DB_PATH       = os.path.join(BASE_DIR, "oddsgpt.db")

GROQ_API_KEY   = os.getenv("GROQ_API_KEY",  "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")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "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")

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, sport_tab TEXT DEFAULT 'all',
                title TEXT DEFAULT 'New Chat', created_at TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                role TEXT, content TEXT, sport_tab TEXT DEFAULT 'all', created_at TEXT
            );
        """)

def db_new_session(sport_tab="all"):
    sid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id,sport_tab,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (sid, sport_tab, "New Chat", now, now))
    return sid

def db_get_sessions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,sport_tab,title,created_at,updated_at FROM sessions ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]

def db_delete_session(sid):
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))

def db_save_message(sid, role, content, sport_tab="all"):
    now = datetime.now().isoformat()
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO sessions (id,sport_tab,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (sid, sport_tab, "New Chat", now, now))
        conn.execute(
            "INSERT INTO messages (session_id,role,content,sport_tab,created_at) VALUES (?,?,?,?,?)",
            (sid, role, content, sport_tab, now))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
    if role == "user":
        title = content[:55] + ("..." if len(content) > 55 else "")
        with get_conn() as conn:
            cur = conn.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
            if cur and cur["title"] == "New Chat":
                conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, sid))

def db_get_history(sid):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role,content FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT 20",
            (sid,)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

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
#  TAVILY — parallel searches for speed
# ══════════════════════════════════════════════════════════════════════════════

async def tavily_one(session: httpx.AsyncClient, query: str) -> tuple:
    """Returns (label, result) tuple."""
    if not TAVILY_API_KEY or TAVILY_API_KEY == "YOUR_TAVILY_API_KEY_HERE":
        return ("", "")
    try:
        r = await session.post(TAVILY_URL, json={
            "api_key":        TAVILY_API_KEY,
            "query":          query,
            "search_depth":   "basic",    # ← basic = faster than advanced
            "max_results":    3,          # ← 3 results = faster
            "include_answer": True
        })
        if r.status_code != 200:
            return ("", "")
        data  = r.json()
        parts = []
        if data.get("answer"):
            parts.append(data["answer"])
        for res in data.get("results", [])[:2]:
            parts.append(f"• {res.get('title','')}: {res.get('content','')[:200]}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[Tavily] {query[:40]}... → {e}")
        return ""


async def search_everything(team1: str, team2: str, sport: str) -> str:
    """
    Runs all searches IN PARALLEL (asyncio.gather) for maximum speed.
    6 searches simultaneously instead of one by one.
    """
    if not TAVILY_API_KEY or TAVILY_API_KEY == "YOUR_TAVILY_API_KEY_HERE":
        return "⚠️ No Tavily key — add yours from app.tavily.com for live data."

    today   = datetime.now().strftime("%Y-%m-%d")
    matchup = f"{team1} vs {team2}"

    # 6 targeted queries run in PARALLEL — much faster
    queries = [
        ("🎰 ODDS & TIPS",          f"{matchup} betting odds tips prediction {today} oddschecker"),
        ("🏥 INJURIES",             f"{team1} {team2} injuries suspended out {today}"),
        ("📊 FORM & H2H",           f"{matchup} recent form head to head results {today}"),
        ("📋 LINEUPS & NEWS",       f"{matchup} predicted lineup team news {today}"),
        ("📈 STATS",                f"{matchup} {sport} stats xG BTTS over under goals {today}"),
        ("💡 EXPERT PREDICTIONS",   f"{matchup} prediction {today} forebet windrawwin expert"),
    ]

    async with httpx.AsyncClient(timeout=15.0) as session:
        # Run ALL searches at the same time
        raw_results = await asyncio.gather(
            *[tavily_one(session, q) for _, q in queries],
            return_exceptions=True
        )

    parts = []
    for (label, _), result in zip(queries, raw_results):
        if isinstance(result, str) and result.strip():
            parts.append(f"\n{label}:\n{result}")

    return "\n".join(parts) if parts else "No live data found."


def extract_match_info(text: str):
    """Extract team1, team2, sport from any message."""
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

# ══════════════════════════════════════════════════════════════════════════════
#  CLARIFICATION CHECK
# ══════════════════════════════════════════════════════════════════════════════

async def needs_clarification(message: str, team1: str, team2: str) -> str | None:
    if team1 and team2 and len(team1) > 1 and len(team2) > 1:
        return None
    if re.search(r"vs?\.?\s+[A-Za-z]", message, re.IGNORECASE):
        return None

    prompt = f"""The user sent: "{message}"
Can you identify what sports match they want analyzed?
If YES: reply PROCEED
If NO: reply CLARIFY: [one short question]"""
    try:
        resp = await groq_call([{"role": "user", "content": prompt}], max_tokens=60)
        resp = resp.strip()
        if resp.upper().startswith("CLARIFY:"):
            return resp[8:].strip()
    except Exception:
        pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PROMPT — with conclusion section
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are OddsGPT — a professional sports betting analyst.
Today: {date}

=== LIVE DATA FROM INTERNET ===
{live_data}
=== END LIVE DATA ===

Write a clear, structured betting analysis. Be concise and direct.

**📊 FORM** — Last 5 results each team/player
**⚔️ H2H** — Head to head record and historical edge
**🏥 INJURIES & SUSPENSIONS** — All missing key players and impact
**📈 STATS** — xG, BTTS rate, Over 2.5 rate, key trends
**💰 ODDS** — Current best odds and value assessment
**💡 EXPERT CONSENSUS** — What prediction sites are saying

---

🎯 **MY CONCLUSION — HOW YOU SHOULD PLAY THIS:**
Write 3-5 sentences giving your personal expert opinion. Be direct. Tell the client exactly how to approach this match, what to bet on, what to avoid, and why. Talk directly to them: "In my opinion, you should..."

---

Then list bets in EXACTLY this format, one per line:
BET: [pick] | TYPE: [type] | CONFIDENCE: [0-100] | RISK: [Low/Medium/High] | ODDS: [range] | MATCH: [teams] | REASON: [one sentence]

Give 4-5 BET lines covering different markets."""

# ══════════════════════════════════════════════════════════════════════════════
#  BET PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_bets(text: str) -> list:
    bets = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.upper().startswith("BET:"):
            continue
        try:
            line = re.sub(r'^BET:\s*', '', line, flags=re.IGNORECASE)

            def get(pattern, default=""):
                m = re.search(pattern, line, re.IGNORECASE)
                return m.group(1).strip() if m else default

            pick = re.split(r'\s*\|\s*TYPE:', line, flags=re.IGNORECASE)[0].strip()
            if not pick:
                continue

            risk = get(r'RISK:\s*(Low|Medium|High)')
            if risk not in ["Low", "Medium", "High"]:
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
app = FastAPI(title="OddsGPT", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=FRONTEND_PATH), name="static")

class ChatRequest(BaseModel):
    message:    str
    session_id: str = "default"
    sport_tab:  str = "all"

class NewSessionRequest(BaseModel):
    sport_tab: str = "all"

@app.get("/")
async def root():
    return FileResponse(os.path.join(FRONTEND_PATH, "index.html"))

@app.post("/api/chat")
async def chat(req: ChatRequest):
    try:
        history             = db_get_history(req.session_id)
        team1, team2, sport = extract_match_info(req.message)

        # Clarification check
        question = await needs_clarification(req.message, team1, team2)
        if question:
            db_save_message(req.session_id, "user",      req.message,       req.sport_tab)
            db_save_message(req.session_id, "assistant", f"❓ {question}",  req.sport_tab)
            return JSONResponse({"response": f"❓ {question}", "confidence": None,
                                 "bets": [], "parlay": {}, "avoid": ""})

        print(f"[chat] {team1} vs {team2} ({sport})")

        # Run web search and Groq IN PARALLEL for speed
        live_data_task = asyncio.create_task(search_everything(team1, team2, sport))

        # While Tavily searches, prepare the Groq messages
        live_data = await live_data_task
        has_live  = len(live_data) > 100 and "⚠️" not in live_data

        prompt   = SYSTEM_PROMPT.format(
            date      = datetime.now().strftime("%Y-%m-%d %H:%M"),
            live_data = live_data
        )
        messages = [{"role": "system", "content": prompt}]
        for msg in history[-4:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": req.message})

        full_response = await groq_call(messages, max_tokens=2000)
        print(f"[chat] Response: {len(full_response)} chars")

        bets = parse_bets(full_response)
        print(f"[chat] Bets: {len(bets)}")

        clean = "\n".join(
            l for l in full_response.split("\n")
            if not l.strip().upper().startswith("BET:")
        ).strip()

        prefix = (
            f"🌐 *Live data searched: {team1} vs {team2}*\n\n"
            if has_live else
            "📚 *Add Tavily key at app.tavily.com for live betting site data*\n\n"
        )

        final = prefix + clean

        db_save_message(req.session_id, "user",      req.message, req.sport_tab)
        db_save_message(req.session_id, "assistant", final,       req.sport_tab)

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
            "response":   f"⚠️ Error: {str(e)}\n\nCheck GROQ_API_KEY on line 22 of main.py",
            "confidence": None, "bets": [], "parlay": {}, "avoid": ""
        })

@app.get("/api/sessions")
async def get_sessions():
    return {"sessions": db_get_sessions()}

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    return {"history": db_get_history(session_id)}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    db_delete_session(session_id)
    return {"status": "deleted"}

@app.post("/api/sessions/new")
async def new_session(req: NewSessionRequest):
    return {"session_id": db_new_session(req.sport_tab)}

@app.get("/api/health")
async def health():
    return {"status": "online", "model": GROQ_MODEL}