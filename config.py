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

# ─── FVG PARAMETRI ──────────────────────────────────────────
MIN_GAP_PCT      = 0.009
MAX_WICK_RATIO   = 0.20
AGGR_FACTOR      = 1.5
AVG_BODY_PERIOD  = 20

# ─── RSI ────────────────────────────────────────────────────
RSI_PERIOD = 14
RSI_BULL   = 50
RSI_BEAR   = 50

# ─── EMA ────────────────────────────────────────────────────
EMA_FAST         = 50
EMA_SLOW         = 100
EMA_SLOPE_BARS   = 4
EMA_MIN_SLOPE    = 0.002
EMA_PARALLEL_MIN = 0.25
EMA_PARALLEL_MAX = 4.0
MAX_CONSEC_AGGR  = 1

# ─── DAILY LOSS LIMIT ───────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.20   # 20% din capital/zi

# ─── SCANNING ───────────────────────────────────────────────
SCAN_INTERVAL_SEC = 90   # 90s — alterneaza cu 4H (60s)

# ─── BLACKLIST ──────────────────────────────────────────────
# MAX_LOSS_PCT_EMERGENCY eliminat — gestionat de Guardian extern
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
