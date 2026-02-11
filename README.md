# Trading Floor ZG Nets - Autonomous AI Trading System

A multi-agent, self-improving algorithmic trading system designed to simulate a professional trading floor. It combines technical analysis, fundamental sentiment (AI), risk management, and portfolio state awareness to execute trades autonomously.

## 🏗 System Architecture

The system mimics a real trading firm with specialized "Agents" handling distinct responsibilities.

### 🤖 The Agents

| Agent | Role | Responsibility |
|-------|------|----------------|
| **Scout** | *Market Scanner* | Scans the universe (13 symbols) to rank assets by **Trend** and **Volatility**. Filters out noise so the PM focuses on active movers. |
| **Signal: Momentum** | *Technical Analyst* | Calculates trend strength using moving average divergences. Bets on "the trend is your friend." |
| **Signal: MeanRev** | *Contrarian Analyst* | Looks for overextended prices to bet on a reversion to the mean. |
| **Signal: Breakout** | *Momentum Analyst* | Identifies prices breaking above recent highs (N-day lookback). |
| **Signal: News (AI)** | *Fundamental Analyst* | Fetches live news headlines and uses NLP (`TextBlob`) to score sentiment (-1.0 to +1.0). Protects against trading into bad news. |
| **PM (Portfolio Mgr)** | *Decision Maker* | Aggregates all signal scores (weighted) and decides **Buy/Sell**. Uses **Volatility Sizing** to adjust position sizes (smaller size for risky assets). |
| **Risk Manager** | *Gatekeeper* | Enforces hard constraints: Max positions (2), Max daily loss (2%), and Stop Loss rules. Rejects trades that violate limits. |
| **Compliance** | *Rule Enforcer* | Ensures trades are only for allowed symbols (Universe list). Prevents "rogue trading." |
| **Exit Manager** | *Execution Trader* | Monitors open positions intraday. Forces exits if **Stop Loss (-2%)** or **Trailing Stop (+5%)** levels are hit. |
| **Optimizer** | *Quant Researcher* | (Nightly Script) Analyzes trade logs to correlate signals with PnL. Automatically adjusts signal weights for the next day. |

---

## 📈 Trading Strategy (The "Alpha")

The current strategy is a **Multi-Factor Ensemble** model.

### 1. Signal Generation
Each symbol receives a composite score (-1.0 to +1.0) based on 4 weighted inputs:
- **25% Momentum:** Is it trending?
- **25% Mean Reversion:** Is it overbought/oversold?
- **25% Breakout:** Did it just break a high?
- **25% News Sentiment:** Is the news good or bad?

### 2. Execution Logic
- **Long & Short:** The system plays both sides. It buys positive scores and shorts negative scores.
- **Sizing:** Position sizes are **Volatility Adjusted**. Stable stocks get larger allocations; volatile stocks get smaller ones.
- **Slippage & Fees:** Simulates real-world friction (5bps slippage + $0.005/share comms).

### 3. Risk Management
- **Stop Loss:** Hard cut at -2% loss.
- **Trailing Stop:** Locks in profit if price reverses 5% from peak.
- **Max Alloc:** Never holds more than 2 positions at once.

---

## 🚀 Setup & Usage

### Prerequisites
- Python 3.10+
- `pip install -r requirements.txt`

### 1. Run the Trading Floor
Executes one cycle: fetches data, generates signals, executes trades.
```bash
scripts\run_workflow.cmd
```

### 2. View Dashboard
Live view of logs, current positions, and PnL.
```bash
python scripts\serve_report.py
# Open http://localhost:8000
```
*(Or use the background `scripts\run_watchdog.cmd` to keep it running)*

### 3. Run Optimizer (Self-Improvement)
Analyzes history and tunes strategy weights.
```bash
python scripts\optimize_weights.py
```

---

## 📂 Key Files

- `configs/workflow.yaml` - The brain (parameters, universe, weights).
- `portfolio.json` - The ledger (Cash, Equity, Positions).
- `trading_logs/` - CSV records of every trade and signal.
- `src/trading_floor/` - Source code for all agents.

---

**Developed by:** Moltbot (OpenClaw)
