"""
═══════════════════════════════════════════════════════════
  NOTIFIER 1H — v11 (după audit sistem v13/v11)
═══════════════════════════════════════════════════════════
Fix-uri față de v10:
  ✓ FIX CRITIC: dll_icon prag corect din capital real (nu hardcodat 500 USDT)
  ✓ FIX CRITIC: _send() cu retry (2 încercări, backoff 3s) — notificări importante
                nu se mai pierd la timeout Render
  ✓ FIX IMPORTANT: Guardian params din config (nu hardcodați în mesaj)
  ✓ FIX IMPORTANT: expired_orders afișat în raport statistici
  ✓ FIX IMPORTANT: notify_startup() separat (nu mai abuzăm notify_error)
  ✓ FIX IMPORTANT: dll_today afișat cu unitate USDT + % din capital
  ✓ FIX MINOR: requests.Session reutilizat (nu conexiune nouă per mesaj)
  ✓ FIX MINOR: **kwargs eliminat din notify_trade_closed
  ✓ FIX MINOR: uptime calculat în raport
"""
import logging
import time
import requests
from datetime import datetime, timezone

import config

logger = logging.getLogger("FVGBot1H")

# ── Session HTTP reutilizată ─────────────────────────────
_session = requests.Session()
_session.headers.update({"Content-Type": "application/x-www-form-urlencoded"})

# ── Emoji maps ───────────────────────────────────────────
RESULT_EMOJI = {
    "WIN":   ("✅", "WIN"),
    "TP":    ("✅", "TAKE PROFIT"),
    "TRAIL": ("🚀", "TRAILING EXIT"),
    "BE":    ("🟡", "BREAKEVEN"),
    "SL":    ("❌", "STOP LOSS"),
    "EC":    ("🔴", "EMERGENCY CLOSE"),
}


# ══════════════════════════════════════════════════════════
#  SEND — cu retry
# ══════════════════════════════════════════════════════════

def _send(text: str, retries: int = 2):
    """
    Trimite mesaj Telegram cu retry.
    retries=2 → maxim 3 încercări totale (1 + 2 retry).
    Backoff: 3s între încercări.
    """
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return

    token   = getattr(config, "TELEGRAM_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}

    for attempt in range(retries + 1):
        try:
            resp = _session.post(url, data=data, timeout=12)
            if resp.status_code == 200:
                return
            # 429 = Telegram rate limit → așteptăm mai mult
            if resp.status_code == 429:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                logger.warning(f"Telegram 429 — aștept {retry_after}s")
                time.sleep(retry_after)
                continue
            logger.warning(f"Telegram HTTP {resp.status_code}: {resp.text[:100]}")
        except requests.exceptions.Timeout:
            logger.warning(f"Telegram timeout (attempt {attempt + 1}/{retries + 1})")
        except Exception as e:
            logger.warning(f"Telegram error: {e}")

        if attempt < retries:
            time.sleep(3)

    logger.warning("Telegram: toate încercările au eșuat — mesaj pierdut")


# ══════════════════════════════════════════════════════════
#  NOTIFY FUNCTIONS
# ══════════════════════════════════════════════════════════

def notify_startup(n_symbols: int, version: str = "v13"):
    """Notificare pornire bot — separată de notify_error."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _send(
        f"🚀 <b>[1H] BOT FVG PORNIT — {version}</b>
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"⏰ {now}
"
        f"📊 Simboluri: <b>{n_symbols}</b>
"
        f"💰 Capital: <b>{config.USDT_PER_TRADE * config.MAX_OPEN_TRADES:.0f} USDT</b> "
        f"({config.USDT_PER_TRADE} × {config.MAX_OPEN_TRADES} max)
"
        f"⚡ Leverage: <b>{config.LEVERAGE}x</b> | TF: <b>{config.TIMEFRAME}</b>
"
        f"🛡 DLL: <b>{config.DAILY_LOSS_LIMIT_PCT * 100:.0f}%</b> din capital/zi"
    )


def notify_setup(setup):
    """Notificare setup FVG detectat — ordin plasat."""
    dir_emoji = "🟢 LONG" if setup.direction == "BULL" else "🔴 SHORT"

    # Guardian params din config (nu hardcodați)
    sl_pct     = getattr(config, "SL_PCT",         25)
    be_pct     = getattr(config, "BE_TRIGGER_PCT",  3)
    trail_pct  = getattr(config, "TRAIL_PCT",        5)
    trail_step = getattr(config, "TRAIL_STEP_PCT",   3)

    _send(
        f"<b>📡 [1H] FVG — {setup.symbol}</b>
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"Direcție: {dir_emoji}
"
        f"Entry:  <code>{setup.entry:.6f}</code>
"
        f"Gap: <code>{setup.gap_bot:.6f}</code> ↔ <code>{setup.gap_top:.6f}</code>
"
        f"ATR: <code>{setup.atr:.6f}</code>
"
        f"RSI: {setup.rsi} | Slope: {setup.slope_fast:+.3f}%
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"🛡 Guardian: SL=-{sl_pct}% | BE@+{be_pct}% | "
        f"TRAIL@+{trail_pct}% (step {trail_step}%)
"
        f"⏰ Expiră în {config.ORDER_EXPIRY_HOURS}h"
    )


def notify_trade(setup, success: bool):
    """Notificare plasare ordin — doar la eșec."""
    if not success:
        _send(
            f"⚠️ <b>[1H] ORDIN EȘUAT</b>
"
            f"{setup.symbol} | {setup.direction} @ {setup.entry:.6f}"
        )


def notify_trade_closed(
    symbol:      str,
    direction:   str,
    entry:       float,
    result:      str,
    pnl_usdt:    float,
    open_time:   str,
    close_time:  str,
    rsi:         float = 0.0,
    duration_h:  float = 0.0,
):
    """
    Notificare trade închis.
    Compatibil cu result: WIN / BE / SL / EC / TRAIL / TP
    """
    emoji, r_text = RESULT_EMOJI.get(result, ("⚪", result))
    sign      = "+" if pnl_usdt >= 0 else ""
    dir_emoji = "🟢 LONG" if direction in ("BUY", "BULL") else "🔴 SHORT"
    pnl_emoji = "📈" if pnl_usdt >= 0 else "📉"

    # ROI față de capital efectiv cu leverage
    capital_efectiv = config.USDT_PER_TRADE * config.LEVERAGE
    roi_pct = (pnl_usdt / capital_efectiv * 100) if capital_efectiv > 0 else 0.0
    roi_sign = "+" if roi_pct >= 0 else ""

    # Format timestamp-uri
    t_open_fmt  = open_time[:16].replace("T", " ") if len(open_time) >= 16 else open_time
    t_close_fmt = close_time[:16].replace("T", " ") if len(close_time) >= 16 else close_time

    _send(
        f"{emoji} <b>[1H] {symbol}</b>
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"Rezultat:  <b>{r_text}</b>
"
        f"Direcție:  {dir_emoji}
"
        f"Entry:     <code>{entry:.6f}</code>
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"{pnl_emoji} PNL: <b>{sign}{pnl_usdt:.4f} USDT</b> "
        f"({roi_sign}{roi_pct:.1f}% ROI)
"
        f"⏱ Durată: <b>{duration_h:.1f}h</b>
"
        f"📊 RSI intrare: {rsi:.1f}
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"<i>{t_open_fmt} → {t_close_fmt} UTC</i>"
    )


def notify_error(context: str, error: str):
    """Notificare eroare sau eveniment neașteptat."""
    _send(
        f"🔥 <b>[1H] EROARE</b>
"
        f"<b>{context}</b>
"
        f"<code>{str(error)[:300]}</code>"
    )


# ══════════════════════════════════════════════════════════
#  RAPORT STATISTICI
# ══════════════════════════════════════════════════════════

def send_statistics_report(stats: dict):
    """
    Raport periodic complet.
    Compatibil cu stats-ul returnat de order_manager.get_bot_stats()
    + câmpuri adiționale trimise din main.py.
    """
    total    = int(stats.get("total_trades",   0) or 0)
    wins     = int(stats.get("wins",           0) or 0)
    losses   = int(stats.get("losses",         0) or 0)
    be       = int(stats.get("be",             0) or 0)
    expired  = int(stats.get("expired_orders", 0) or 0)
    pending  = int(stats.get("pending",        0) or 0)
    open_pos = int(stats.get("open_positions", 0) or 0)
    pnl      = float(stats.get("pnl_total",    0.0) or 0.0)
    pnl_today= float(stats.get("pnl_today",    0.0) or 0.0)
    wr       = float(stats.get("win_rate",     0.0) or 0.0)
    best     = float(stats.get("best_trade",   0.0) or 0.0)
    worst    = float(stats.get("worst_trade",  0.0) or 0.0)
    started  = str(stats.get("start_time",     "?"))
    dll_today= float(stats.get("dll_today",    0.0) or 0.0)

    # Capital real din stats sau estimat
    capital = float(stats.get("capital", config.USDT_PER_TRADE * config.MAX_OPEN_TRADES))

    # DLL limit în USDT (FIX CRITIC — nu mai e hardcodat 500)
    dll_limit_usdt = capital * config.DAILY_LOSS_LIMIT_PCT
    dll_limit_pct  = config.DAILY_LOSS_LIMIT_PCT * 100
    dll_icon       = "⛔" if dll_today <= -dll_limit_usdt else "✅"

    # Win rate emoji
    wr_emoji = "🔥" if wr >= 65 else ("✅" if wr >= 50 else "⚠️")

    # PNL signs
    pnl_sign  = "+" if pnl      >= 0 else ""
    ptd_sign  = "+" if pnl_today >= 0 else ""
    pnl_emoji = "📈" if pnl     >= 0 else "📉"

    # Uptime
    uptime_str = ""
    try:
        t_start = datetime.strptime(started, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        uptime_h = (datetime.now(timezone.utc) - t_start).total_seconds() / 3600
        if uptime_h < 24:
            uptime_str = f" (uptime {uptime_h:.0f}h)"
        else:
            uptime_str = f" (uptime {uptime_h/24:.1f}z)"
    except Exception:
        pass

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Construim mesajul ─────────────────────────────────
    if total == 0:
        trades_line = "Niciun trade închis încă"
    else:
        trades_line = (
            f"{wr_emoji} Win Rate: <b>{wr:.1f}%</b> "
            f"({wins}✅ / {be}🟡 / {losses}❌ / {expired}⏰)"
        )

    dll_display = (
        f"{dll_today:+.2f} USDT "
        f"(limită: {dll_limit_pct:.0f}% = -{dll_limit_usdt:.1f} USDT)"
    )

    msg = (
        f"<b>📊 [1H] RAPORT BOT FVG — {now_str}</b>
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"<b>REZULTATE</b>
"
        f"Trades: <b>{total}</b> | {trades_line}
"
        f"{pnl_emoji} PNL Total: <b>{pnl_sign}{pnl:.4f} USDT</b>
"
        f"   Azi: <b>{ptd_sign}{pnl_today:.4f} USDT</b>"
    )

    if total > 0:
        msg += f"
   Best: <b>+{best:.4f}</b> | Worst: <b>{worst:.4f}</b>"

    msg += (
        f"
━━━━━━━━━━━━━━━━━━━━
"
        f"<b>SITUAȚIE</b>
"
        f"Poziții: <b>{open_pos}</b>/{config.MAX_OPEN_TRADES} | "
        f"Pending: <b>{pending}</b>
"
        f"{dll_icon} DLL azi: {dll_display}
"
        f"💰 Capital: <b>{capital:.2f} USDT</b>
"
        f"━━━━━━━━━━━━━━━━━━━━
"
        f"<i>Pornit: {started}{uptime_str}</i>"
    )

    _send(msg)
