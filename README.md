# 🎯 OddsGPT — AI Sports Betting Predictor

An AI-powered sports analysis tool that predicts match outcomes and recommends bets using **real-time web data** and **Claude AI** as the reasoning engine.

---

## 🧠 How It Works

```
User Query  ──►  FastAPI Backend  ──►  Claude AI + Web Search
                                              │
                                    Fetches live data:
                                    • Team form (last 5-10 matches)
                                    • Head-to-head records
                                    • Injuries & suspensions
                                    • Current standings
                                    • Bookmaker odds
                                              │
                                    Analyzes & generates:
                                    • Statistical breakdown
                                    • 3-5 bet recommendations
                                    • Confidence % per bet
                                    • Risk classification
                                              │
User  ◄──────── Chat Interface  ◄─────────────┘
```

---

## 📁 Project Structure

```
sports_ai/
├── backend/
│   ├── main.py          # FastAPI server & routes
│   └── ai_predictor.py  # Claude AI engine + bet parser
├── frontend/
│   └── index.html       # Chat web interface
├── requirements.txt     # Python dependencies
├── run.sh               # One-click launch script
└── README.md
```

---

## 🚀 Setup & Run

### Prerequisites
- Python 3.10+
- Internet connection (for live web search)

### Installation

```bash
# 1. Clone or download the project
cd sports_ai

# 2. Run everything with one command
chmod +x run.sh
./run.sh

# OR manually:
pip install -r requirements.txt
cd backend
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Open in browser
```
http://localhost:8000
```

---

## 💬 Query Examples

| Sport | Example Query |
|-------|--------------|
| ⚽ Football | `football - Ukraine vs Albania, how should I play it?` |
| 🏀 Basketball | `basketball - Lakers vs Celtics, best bets tonight` |
| 🎾 Tennis | `tennis - Djokovic vs Alcaraz, who wins and what odds?` |
| 🏈 American Football | `NFL - Chiefs vs Eagles, Super Bowl prediction` |
| 🏒 Hockey | `NHL - Bruins vs Rangers, betting breakdown` |
| ⚾ Baseball | `MLB - Yankees vs Red Sox, over/under analysis` |

---

## 🎲 Bet Types Analyzed

The AI covers all major bet markets:
- **1X2** — Home / Draw / Away
- **Both Teams to Score (BTTS)**
- **Over/Under Goals** (1.5, 2.5, 3.5)
- **Asian Handicap**
- **Double Chance**
- **Correct Score**
- **First Goal Scorer**
- **Half-time/Full-time**
- **Accumulator tips**

---

## ⚙️ Architecture Details

### Backend (`FastAPI`)
- `POST /api/chat` — Main prediction endpoint
- Accepts user message + conversation history
- Returns: response text + structured bet array

### AI Engine (`Claude claude-sonnet-4`)
- Uses `web_search_20250305` tool for real-time data
- Multi-turn tool-calling loop (up to 8 iterations)
- Parses structured JSON bet recommendations from AI response
- Extracts: type, pick, confidence %, risk level, odds range

### Frontend
- Pure HTML/CSS/JS (no framework needed)
- Real-time chat interface
- Visual bet cards with confidence bars
- Risk color coding: 🟢 Low / 🟡 Medium / 🔴 High

---

## 🔧 Customization

### Change the AI persona (backend/ai_predictor.py)
Edit `SYSTEM_PROMPT` to adjust:
- Analysis style and depth
- Sports focus areas
- Bet recommendation format
- Risk thresholds

### Add more bet markets
Modify the JSON schema in `SYSTEM_PROMPT`:
```python
# In the SYSTEM_PROMPT, the bets array schema can be extended:
{
  "bets": [
    {
      "type": "...",
      "pick": "...",
      "confidence": 0-100,
      "reasoning": "...",
      "risk": "Low|Medium|High",
      "odds_range": "1.80 - 2.10",
      "stake_recommendation": "1-5 units"  # ← add new fields
    }
  ]
}
```

---

## ⚠️ Disclaimer

This tool is for **entertainment and informational purposes only**.
- Sports betting involves significant financial risk
- AI predictions are probabilistic, not guaranteed
- Never bet more than you can afford to lose
- Check local laws regarding sports betting in your region

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, Uvicorn |
| AI | Claude claude-sonnet-4 (Anthropic) |
| Data | Web Search (real-time) |
| Frontend | HTML5, CSS3, Vanilla JS |
| HTTP Client | HTTPX (async) |
