"""
Trading Journal — salveaza fiecare trade intr-un CSV
pentru analiza si optimizare ulterioara.

Date salvate per trade:
- Timestamp, simbol, directie, entry, SL, TP
- Rezultat (TP/SL/EXPIRED), PNL net, durata (ore)
- RSI la intrare, EMA slope la intrare
- Ora din zi (pentru analiza temporala)
"""
import csv
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger("FVGBot")

JOURNAL_FILE = "trading_journal.csv"

HEADERS = [
    "date",
    "time_utc",
    "symbol",
    "direction",
    "entry",
    "sl",
    "tp",
    "result",        # TP / SL / EXPIRED / OPEN
    "pnl_usdt",
    "pnl_pct",       # % din USDT_PER_TRADE
    "duration_hours",
    "rsi",
    "ema_slope_pct",
    "hour_of_day",   # 0-23 UTC — pentru analiza temporala
    "open_time",
    "close_time",
]


def _ensure_file():
    """Creeaza fisierul CSV cu header daca nu exista."""
    if not os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writeheader()
        logger.info(f"Journal creat: {JOURNAL_FILE}")


def log_trade(
    symbol: str,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    result: str,
    pnl_usdt: float,
    usdt_per_trade: float,
    open_time: str,
    close_time: str,
    rsi: float = 0.0,
    ema_slope: float = 0.0,
):
    """
    Salveaza un trade in jurnal.
    Apelat din order_manager cand un trade se inchide sau expira.
    """
    _ensure_file()

    try:
        # Calculeaza durata
        try:
            t_open  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            t_close = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            duration_h = round((t_close - t_open).total_seconds() / 3600, 2)
        except Exception:
            duration_h = 0.0

        # PNL procentual din risk-ul per trade
        pnl_pct = round((pnl_usdt / usdt_per_trade * 100), 2) if usdt_per_trade > 0 else 0

        now = datetime.now(timezone.utc)

        row = {
            "date":           now.strftime("%Y-%m-%d"),
            "time_utc":       now.strftime("%H:%M:%S"),
            "symbol":         symbol,
            "direction":      direction,
            "entry":          entry,
            "sl":             sl,
            "tp":             tp,
            "result":         result,
            "pnl_usdt":       round(pnl_usdt, 4),
            "pnl_pct":        pnl_pct,
            "duration_hours": duration_h,
            "rsi":            round(rsi, 1),
            "ema_slope_pct":  round(ema_slope, 3),
            "hour_of_day":    now.hour,
            "open_time":      open_time,
            "close_time":     close_time,
        }

        with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writerow(row)

        emoji = "✅" if result == "TP" else ("❌" if result == "SL" else "⏰")
        sign  = "+" if pnl_usdt >= 0 else ""
        logger.info(
            f"[JOURNAL] {emoji} {symbol} {direction} | "
            f"{result} | {sign}{pnl_usdt:.4f} USDT | "
            f"{duration_h:.1f}h | RSI={rsi:.1f}"
        )

    except Exception as e:
        logger.error(f"[JOURNAL] Eroare salvare trade: {e}")


def get_stats() -> dict:
    """
    Citeste jurnalul si returneaza statistici rapide.
    Folosit pentru raportul Telegram.
    """
    _ensure_file()
    trades = []

    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    except Exception as e:
        logger.error(f"[JOURNAL] Eroare citire: {e}")
        return {}

    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "expired": 0,
                "pnl_total": 0.0, "win_rate": 0.0, "best": 0.0, "worst": 0.0}

    closed = [t for t in trades if t["result"] in ("TP", "SL")]
    wins   = [t for t in closed if t["result"] == "TP"]
    losses = [t for t in closed if t["result"] == "SL"]
    expired= [t for t in trades if t["result"] == "EXPIRED"]

    pnl_list  = [float(t["pnl_usdt"]) for t in closed]
    pnl_total = sum(pnl_list)
    win_rate  = len(wins) / len(closed) * 100 if closed else 0
    best      = max(pnl_list) if pnl_list else 0
    worst     = min(pnl_list) if pnl_list else 0

    # Best performing symbols
    sym_pnl = {}
    for t in closed:
        s = t["symbol"]
        sym_pnl[s] = sym_pnl.get(s, 0) + float(t["pnl_usdt"])
    top_sym = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:3]

    # Best hours
    hour_wr = {}
    for t in closed:
        h = t["hour_of_day"]
        if h not in hour_wr:
            hour_wr[h] = {"w": 0, "total": 0}
        hour_wr[h]["total"] += 1
        if t["result"] == "TP":
            hour_wr[h]["w"] += 1
    best_hours = sorted(
        [(h, d["w"]/d["total"]*100) for h, d in hour_wr.items() if d["total"] >= 3],
        key=lambda x: x[1], reverse=True
    )[:3]

    # Avg duration TP vs SL
    avg_dur_tp = (sum(float(t["duration_hours"]) for t in wins) / len(wins)
                  if wins else 0)
    avg_dur_sl = (sum(float(t["duration_hours"]) for t in losses) / len(losses)
                  if losses else 0)

    return {
        "total":       len(trades),
        "wins":        len(wins),
        "losses":      len(losses),
        "expired":     len(expired),
        "pnl_total":   round(pnl_total, 4),
        "win_rate":    round(win_rate, 1),
        "best":        round(best, 4),
        "worst":       round(worst, 4),
        "top_symbols": top_sym,
        "best_hours":  best_hours,
        "avg_dur_tp":  round(avg_dur_tp, 1),
        "avg_dur_sl":  round(avg_dur_sl, 1),
    }
