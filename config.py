import os

# =====================================================
#  FVG BOT 1H - CONFIG v11 (fixed + calibrat 175 USDT)
# =====================================================
# Backtest v4: WR 65.4% | PF 2.13 | MaxDD 3.17%
# FIX v11:
#   - MAX_OPEN_TRADES: 25 -> 7 (nu blocam 100% capital)
#   - RSI_BULL_MAX: 100 -> 85 (evitam intrari extreme)
#   - SCAN_INTERVAL_SEC: 90 -> 900 (aliniat cu main.py)
#   - Adaugate: SL_PCT, BE_TRIGGER_PCT, TRAIL_PCT, TRAIL_STEP_PCT
#   - LOG_MAX_BYTES + LOG_BACKUP_COUNT pentru RotatingFileHandler

# --- API ---
API_KEY    = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")

# --- STRATEGIE ---
TIMEFRAME          = "1h"
LEVERAGE           = 10
USDT_PER_TRADE     = 7          # margin per trade
MAX_OPEN_TRADES    = 25          # FIX: era 25 (100% capital!) -> 7 (49 USDT = 28%)
ORDER_EXPIRY_HOURS = 8

# --- GUARDIAN (Trailing + Breakeven) ---
SL_PCT          = 25            # Stop Loss: -25% din notional
BE_TRIGGER_PCT  = 3             # Mut SL la entry la +3% profit
TRAIL_PCT       = 5             # Activez trailing la +5% profit
TRAIL_STEP_PCT  = 3             # Trailing step: 3% sub maxim

# --- FVG PARAMETRI (DETECTOR) ---
MIN_GAP_PCT       = 0.009       # gap minim 0.9%
MIN_GAP_ATR_MULT  = 0.8         # gap minim relativ la ATR
MAX_WICK_RATIO    = 0.30
AGGR_FACTOR       = 2.0
AVG_BODY_PERIOD   = 20
ATR_PERIOD        = 14

# --- RSI ---
RSI_PERIOD   = 14
RSI_BULL_MIN = 55               # prag minim BULL
RSI_BULL_MAX = 85               # FIX: era 100 (dezactivat) -> 85
RSI_BULL     = RSI_BULL_MIN     # alias compatibilitate
RSI_BEAR     = 100 - RSI_BULL_MIN  # = 45

# --- EMA ---
EMA_FAST         = 50
EMA_SLOW         = 100
EMA_SLOPE_BARS   = 4
EMA_MIN_SLOPE    = 0.002        # 0.20%
EMA_PARALLEL_MIN = 0.25
EMA_PARALLEL_MAX = 4.0
MAX_CONSEC_AGGR  = 3

# --- ENTRY ---
ENTRY_FILL_RATIO = 0.5          # mid-gap: WR 65% in backtest

# --- DAILY LOSS LIMIT ---
DAILY_LOSS_LIMIT_PCT = 0.20     # 20% din capital/zi = ~35 USDT

# --- SCANNING ---
# IMPORTANT: aceasta valoare trebuie sa fie IDENTICA cu
# SCAN_INTERVAL_SEC din main.py (momentan hardcodat la 900 acolo)
SCAN_INTERVAL_SEC = 900

# --- BLACKLIST ---
BLACKLIST = [
    "BTCDOMUSDT", "DEFIUSDT", "XPDUSDT",
    "1000WHYUSDT", "USDCUSDT", "INTCUSDT",
    "PARTIUSDT", "TNSRUSDT", "DYMUSDT",
    "HIPPOUSDT", "CROSSUSDT",
]

# --- TELEGRAM ---
TELEGRAM_ENABLED      = True
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_HOURS = 4

# --- FISIERE STATE ---
STATE_FILE       = "bot_state_1h.json"
JOURNAL_FILE     = "trading_journal_1h.csv"
LOG_FILE         = "fvg_bot_1h.log"
LOG_MAX_BYTES    = 5_000_000    # 5 MB per fisier
LOG_BACKUP_COUNT = 3            # max 3 backup-uri = 15 MB total
