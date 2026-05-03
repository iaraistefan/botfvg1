"""
═══════════════════════════════════════════════════════════
  FVG BOT 1H — CONFIG v10 (după backtest v4 final)
═══════════════════════════════════════════════════════════
Parametri obținuți din backtest profesional v4:
  - 365 zile, 435 simboluri USDT futures
  - 7,776 simulări Faza 1 + 5,250 simulări Faza 2 v2
  - Walk-forward 4/4 ferestre profitabile (PF mediu 2.03)
  - Stress tests: ROBUST la slippage 0.10% și perturbații ±10%

Performance așteptată live:
  - Win Rate: 65.4% | Profit Factor: 2.13
  - PNL anual estimat: +2,886 USDT pe 500 capital (~+577%)
  - Max DD: 3.17%

NOTĂ: Strategia folosește Trailing Stop + Breakeven prin Guardian extern.
TP fix DEZACTIVAT — Guardian decide ieșirea dinamic.
"""
import os

# ─── API ────────────────────────────────────────────────────
API_KEY    = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")

# ─── STRATEGIE ──────────────────────────────────────────────
TIMEFRAME          = "1h"
LEVERAGE           = 10
USDT_PER_TRADE     = 7
MAX_OPEN_TRADES    = 25
ORDER_EXPIRY_HOURS = 8

# ─── FVG PARAMETRI (DETECTOR) ───────────────────────────────
MIN_GAP_PCT       = 0.009     # gap minim % (din backtest top 1)
MIN_GAP_ATR_MULT  = 0.8       # NOU — gap minim relativ la ATR
MAX_WICK_RATIO    = 0.30
AGGR_FACTOR       = 2.0       # din backtest top 1
AVG_BODY_PERIOD   = 20
ATR_PERIOD        = 14        # NOU — pentru filtru ATR

# ─── RSI BANDS ──────────────────────────────────────────────
RSI_PERIOD   = 14
RSI_BULL_MIN = 55             # NOU — prag minim BULL
RSI_BULL_MAX = 100            # NOU — prag overbought (100 = dezactivat)
# Pentru BEAR: RSI ∈ [100-RSI_BULL_MAX, 100-RSI_BULL_MIN] = [0, 45]
# Compatibilitate cu detector vechi:
RSI_BULL = RSI_BULL_MIN
RSI_BEAR = 100 - RSI_BULL_MIN  # = 45

# ─── EMA ────────────────────────────────────────────────────
EMA_FAST         = 50
EMA_SLOW         = 100
EMA_SLOPE_BARS   = 4
EMA_MIN_SLOPE    = 0.002
EMA_PARALLEL_MIN = 0.25
EMA_PARALLEL_MAX = 4.0
MAX_CONSEC_AGGR  = 3          # din backtest top 1 (vechi era 1)

# ─── ENTRY ──────────────────────────────────────────────────
ENTRY_FILL_RATIO = 0.5        # NOU — entry la mid-gap (KEY upgrade)
                              # 0.0 = gap_top (agresiv, fill ușor)
                              # 0.5 = mid-gap (conservator, fill 54%, WR 65%)
                              # 1.0 = gap_bot (foarte conservator)

# ─── DAILY LOSS LIMIT ───────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.20   # 20% din capital/zi

# ─── SCANNING ───────────────────────────────────────────────
SCAN_INTERVAL_SEC = 90        # 90s — alternează cu 4H (60s)

# ─── BLACKLIST ──────────────────────────────────────────────
BLACKLIST = [
    "BTCDOMUSDT", "DEFIUSDT", "XPDUSDT",
    "1000WHYUSDT", "USDCUSDT", "INTCUSDT",
    "PARTIUSDT", "TNSRUSDT", "DYMUSDT",
    "HIPPOUSDT", "CROSSUSDT",
]

# ─── TELEGRAM ───────────────────────────────────────────────
TELEGRAM_ENABLED      = True
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_HOURS = 4

# ─── FISIERE STATE ──────────────────────────────────────────
STATE_FILE   = "bot_state_1h.json"
JOURNAL_FILE = "trading_journal_1h.csv"
LOG_FILE     = "fvg_bot_1h.log"
