"""
═══════════════════════════════════════════════════════════
  TRADING JOURNAL — v2 (după audit sistem v11-v13)
═══════════════════════════════════════════════════════════
Fix-uri față de v1:
  ✓ FIX CRITIC: hour_of_day = open_time.hour (nu now.hour)
  ✓ FIX CRITIC: get_stats() aliniat cu rezultatele order_manager v11
                (WIN/BE/SL/EC/TRAIL în loc de TP/SL)
  ✓ FIX CRITIC: date = data deschiderii trade-ului (nu data scrierii)
  ✓ FIX IMPORTANT: threading.Lock() — previne race condition la scriere CSV
  ✓ FIX IMPORTANT: _file_initialized flag — elimină os.path.exists() per trade
  ✓ FIX IMPORTANT: pnl_roi_pct adăugat (PNL față de capital efectiv cu leverage)
  ✓ FIX MINOR: emoji dict complet (WIN/BE/SL/EC/TRAIL/EXPIRED)
  ✓ FIX MINOR: flush + fsync explicit după scriere
  ✓ Coloane noi: date_open, date_close, leverage, usdt_per_trade
  ✓ Toate coloanele v1 păstrate pentru compatibilitate CSV existent
"""
import csv
import os
import logging
import threading
from datetime import datetime, timezone

import config

logger = logging.getLogger("FVGBot1H")

JOURNAL_FILE = getattr(config, "JOURNAL_FILE", "trading_journal.csv")

# Rezultate considerate "închise" — aliniate cu order_manager v11
CLOSED_RESULTS = {"WIN", "TP", "TRAIL", "BE", "SL", "EC"}
WIN_RESULTS    = {"WIN", "TP", "TRAIL"}
LOSS_RESULTS   = {"SL", "EC"}

HEADERS = [
    "date_open",        # data deschiderii (UTC) — FIX față de v1
    "date_close",       # data închiderii (UTC)
    "time_open_utc",    # ora deschiderii HH:MM:SS
    "time_close_utc",   # ora închiderii HH:MM:SS
    "symbol",
    "direction",
    "entry",
    "sl",
    "tp",
    "result",           # WIN / BE / SL / EC / EXPIRED / TRAIL
    "pnl_usdt",
    "pnl_pct",          # % față de usdt_per_trade (risk nominal)
    "pnl_roi_pct",      # % față de capital efectiv (usdt_per_trade × leverage)
    "duration_hours",
    "rsi",
    "ema_slope_pct",
    "hour_of_day",      # 0-23 UTC — ora DESCHIDERII (FIX critic față de v1)
    "open_time",        # ISO timestamp complet deschidere
    "close_time",       # ISO timestamp complet închidere
    "leverage",         # leverage folosit la trade
    "usdt_per_trade",   # capital alocat per trade
]

EMOJI_MAP = {
    "WIN":     "✅",
    "TP":      "✅",
    "TRAIL":   "✅",
    "BE":      "🟡",
    "SL":      "❌",
    "EC":      "🔴",
    "EXPIRED": "⏰",
}

# ── State modul ──────────────────────────────────────────
_lock             = threading.Lock()
_file_initialized = False


def _ensure_file():
    """
    Creează fișierul CSV cu header dacă nu există.
    Apelat O SINGURĂ DATĂ (flag modul) — elimină os.path.exists() per trade.
    """
    global _file_initialized
    if _file_initialized:
        return
    with _lock:
        if _file_initialized:   # double-check după lock
            return
        if not os.path.exists(JOURNAL_FILE):
            try:
                with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=HEADERS)
                    writer.writeheader()
                    f.flush()
                    os.fsync(f.fileno())
                logger.info(f"[JOURNAL] Creat: {JOURNAL_FILE}")
            except Exception as e:
                logger.error(f"[JOURNAL] Nu am putut crea fișierul: {e}")
                return
        else:
            # Fișierul există deja — verificăm dacă headerul e compatibil
            _migrate_headers_if_needed()
        _file_initialized = True


def _migrate_headers_if_needed():
    """
    Dacă fișierul CSV existent are un header v1 (lipsă coloane noi),
    adăugăm coloanele lipsă ca goale — nu ștergem datele existente.
    """
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_headers = reader.fieldnames or []

        missing = [h for h in HEADERS if h not in existing_headers]
        if not missing:
            return   # header complet, nimic de migrat

        logger.info(f"[JOURNAL] Migrare header — adaug coloane noi: {missing}")

        # Citim toate rândurile existente
        rows = []
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        # Rescriem cu headerul complet
        with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                # Coloanele lipsă → valori goale / compatibile
                if "date_open" not in row and "date" in row:
                    row["date_open"] = row.get("date", "")
                if "date_close" not in row and "date" in row:
                    row["date_close"] = row.get("date", "")
                if "time_open_utc" not in row and "time_utc" in row:
                    row["time_open_utc"] = row.get("time_utc", "")
                if "hour_of_day" not in row:
                    # Recalculăm din open_time dacă disponibil
                    try:
                        t = datetime.fromisoformat(
                            row.get("open_time", "").replace("Z", "+00:00")
                        )
                        row["hour_of_day"] = str(t.hour)
                    except Exception:
                        row["hour_of_day"] = ""
                for col in missing:
                    if col not in row:
                        row[col] = ""
                writer.writerow(row)

            f.flush()
            os.fsync(f.fileno())

        logger.info(f"[JOURNAL] Migrare completă: {len(rows)} rânduri migrate")
    except Exception as e:
        logger.error(f"[JOURNAL] Eroare migrare: {e}")


# ══════════════════════════════════════════════════════════
#  LOG TRADE
# ══════════════════════════════════════════════════════════

def log_trade(
    symbol:         str,
    direction:      str,
    entry:          float,
    sl:             float,
    tp:             float,
    result:         str,
    pnl_usdt:       float,
    usdt_per_trade: float,
    open_time:      str,
    close_time:     str,
    rsi:            float = 0.0,
    ema_slope:      float = 0.0,
):
    """
    Salvează un trade în jurnal CSV.
    Thread-safe — folosește lock de modul.
    """
    _ensure_file()

    try:
        # ── Parsare timestamp-uri ─────────────────────────
        t_open = t_close = None
        duration_h = 0.0

        try:
            t_open  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            t_close = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            duration_h = round((t_close - t_open).total_seconds() / 3600, 2)
        except Exception:
            pass

        # ── Ore deschidere / închidere ────────────────────
        hour_of_day    = t_open.hour  if t_open  else 0   # FIX CRITIC: ora deschiderii
        time_open_utc  = t_open.strftime("%H:%M:%S")  if t_open  else ""
        time_close_utc = t_close.strftime("%H:%M:%S") if t_close else ""
        date_open      = t_open.strftime("%Y-%m-%d")  if t_open  else ""
        date_close     = t_close.strftime("%Y-%m-%d") if t_close else ""

        # ── PNL calculations ──────────────────────────────
        pnl_pct = round(pnl_usdt / usdt_per_trade * 100, 2) if usdt_per_trade > 0 else 0.0
        leverage = getattr(config, "LEVERAGE", 10)
        capital_efectiv = usdt_per_trade * leverage
        pnl_roi_pct = round(pnl_usdt / capital_efectiv * 100, 2) if capital_efectiv > 0 else 0.0

        row = {
            "date_open":       date_open,
            "date_close":      date_close,
            "time_open_utc":   time_open_utc,
            "time_close_utc":  time_close_utc,
            "symbol":          symbol,
            "direction":       direction,
            "entry":           entry,
            "sl":              sl,
            "tp":              tp,
            "result":          result,
            "pnl_usdt":        round(pnl_usdt, 4),
            "pnl_pct":         pnl_pct,
            "pnl_roi_pct":     pnl_roi_pct,
            "duration_hours":  duration_h,
            "rsi":             round(float(rsi), 1),
            "ema_slope_pct":   round(float(ema_slope), 3),
            "hour_of_day":     hour_of_day,
            "open_time":       open_time,
            "close_time":      close_time,
            "leverage":        leverage,
            "usdt_per_trade":  usdt_per_trade,
        }

        # ── Scriere thread-safe ───────────────────────────
        with _lock:
            with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
                writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())

        # ── Log ───────────────────────────────────────────
        emoji = EMOJI_MAP.get(result, "⚪")
        sign  = "+" if pnl_usdt >= 0 else ""
        logger.info(
            f"[JOURNAL] {emoji} {symbol} {direction} | {result} | "
            f"{sign}{pnl_usdt:.4f} USDT ({sign}{pnl_roi_pct:.1f}% ROI) | "
            f"{duration_h:.1f}h | RSI={rsi:.1f} | ora={hour_of_day}:00 UTC"
        )

    except Exception as e:
        logger.error(f"[JOURNAL] Eroare salvare trade {symbol}: {e}")


# ══════════════════════════════════════════════════════════
#  GET STATS
# ══════════════════════════════════════════════════════════

def get_stats(days: int = 0) -> dict:
    """
    Citește jurnalul și returnează statistici.

    Args:
        days: dacă > 0, filtrează ultimele N zile. 0 = tot istoricul.

    Aliniat cu order_manager v11:
        WIN/TP/TRAIL → câștiguri
        SL/EC        → pierderi
        BE           → breakeven
        EXPIRED      → ordine neumplute
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
        return {
            "total": 0, "wins": 0, "losses": 0, "be": 0, "expired": 0,
            "pnl_total": 0.0, "win_rate": 0.0,
            "best": 0.0, "worst": 0.0,
            "top_symbols": [], "best_hours": [],
            "avg_dur_win": 0.0, "avg_dur_loss": 0.0,
            "data_days": 0,
        }

    # ── Filtrare pe zile ──────────────────────────────────
    if days > 0:
        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        cutoff -= timedelta(days=days - 1)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        trades = [
            t for t in trades
            if t.get("date_open", t.get("date", "")) >= cutoff_str
        ]

    # ── Clasificare ───────────────────────────────────────
    closed  = [t for t in trades if t.get("result", "") in CLOSED_RESULTS]
    wins    = [t for t in closed  if t.get("result", "") in WIN_RESULTS]
    losses  = [t for t in closed  if t.get("result", "") in LOSS_RESULTS]
    be_list = [t for t in closed  if t.get("result", "") == "BE"]
    expired = [t for t in trades  if t.get("result", "") == "EXPIRED"]

    def safe_float(val, default=0.0):
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    pnl_list  = [safe_float(t.get("pnl_usdt")) for t in closed]
    pnl_total = sum(pnl_list)
    win_rate  = len(wins) / len(closed) * 100 if closed else 0.0
    best      = max(pnl_list) if pnl_list else 0.0
    worst     = min(pnl_list) if pnl_list else 0.0

    # ── Top simboluri ─────────────────────────────────────
    sym_pnl: dict = {}
    for t in closed:
        s = t.get("symbol", "?")
        sym_pnl[s] = sym_pnl.get(s, 0.0) + safe_float(t.get("pnl_usdt"))
    top_symbols = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:5]

    # ── Cele mai bune ore (min 3 trade-uri per oră) ───────
    hour_stats: dict = {}
    for t in closed:
        h = str(t.get("hour_of_day", "?"))
        if h not in hour_stats:
            hour_stats[h] = {"w": 0, "total": 0}
        hour_stats[h]["total"] += 1
        if t.get("result", "") in WIN_RESULTS:
            hour_stats[h]["w"] += 1

    best_hours = sorted(
        [
            (int(h), round(d["w"] / d["total"] * 100, 1))
            for h, d in hour_stats.items()
            if d["total"] >= 3 and h.isdigit()
        ],
        key=lambda x: x[1], reverse=True
    )[:5]

    # ── Durată medie WIN vs LOSS ──────────────────────────
    avg_dur_win = (
        sum(safe_float(t.get("duration_hours")) for t in wins) / len(wins)
        if wins else 0.0
    )
    avg_dur_loss = (
        sum(safe_float(t.get("duration_hours")) for t in losses) / len(losses)
        if losses else 0.0
    )

    # ── Zile de date disponibile ──────────────────────────
    all_dates = {
        t.get("date_open", t.get("date", ""))
        for t in trades
        if t.get("date_open", t.get("date", ""))
    }
    data_days = len(all_dates)

    return {
        "total":        len(trades),
        "wins":         len(wins),
        "losses":       len(losses),
        "be":           len(be_list),
        "expired":      len(expired),
        "pnl_total":    round(pnl_total, 4),
        "win_rate":     round(win_rate, 1),
        "best":         round(best, 4),
        "worst":        round(worst, 4),
        "top_symbols":  top_symbols,
        "best_hours":   best_hours,
        "avg_dur_win":  round(avg_dur_win, 1),
        "avg_dur_loss": round(avg_dur_loss, 1),
        "data_days":    data_days,
    }
