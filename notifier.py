"""
NOTIFIER 1H - v11 ASCII-safe
"""
import logging
import time
import requests
from datetime import datetime, timezone

import config

logger = logging.getLogger("FVGBot1H")

_session = requests.Session()
_session.headers.update({"Content-Type": "application/x-www-form-urlencoded"})

RESULT_LABELS = {
    "WIN":   "WIN",
    "TP":    "TAKE PROFIT",
    "TRAIL": "TRAILING EXIT",
    "BE":    "BREAKEVEN",
    "SL":    "STOP LOSS",
    "EC":    "EMERGENCY CLOSE",
}


def _send(text, retries=2):
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return
    token = getattr(config, "TELEGRAM_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    for attempt in range(retries + 1):
        try:
            resp = _session.post(url, data=data, timeout=12)
            if resp.status_code == 200:
                return
            if resp.status_code == 429:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                logger.warning("Telegram 429 - astept " + str(retry_after) + "s")
                time.sleep(retry_after)
                continue
            logger.warning("Telegram HTTP " + str(resp.status_code))
        except requests.exceptions.Timeout:
            logger.warning("Telegram timeout attempt " + str(attempt + 1))
        except Exception as e:
            logger.warning("Telegram error: " + str(e))
        if attempt < retries:
            time.sleep(3)
    logger.warning("Telegram: toate incercarile au esuat")


def notify_startup(n_symbols, version="v13"):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "<b>[1H] BOT FVG PORNIT - " + version + "</b>",
        "====================",
        "Ora: " + now,
        "Simboluri: <b>" + str(n_symbols) + "</b>",
        "Capital: <b>" + str(config.USDT_PER_TRADE * config.MAX_OPEN_TRADES) + " USDT</b>",
        "Leverage: <b>" + str(config.LEVERAGE) + "x</b> | TF: <b>" + str(config.TIMEFRAME) + "</b>",
        "DLL: <b>" + str(int(config.DAILY_LOSS_LIMIT_PCT * 100)) + "%</b> din capital/zi",
    ]
    _send("\n".join(lines))


def notify_setup(setup):
    dir_text = "LONG" if setup.direction == "BULL" else "SHORT"
    sl_pct = getattr(config, "SL_PCT", 25)
    be_pct = getattr(config, "BE_TRIGGER_PCT", 3)
    trail_pct = getattr(config, "TRAIL_PCT", 5)
    trail_step = getattr(config, "TRAIL_STEP_PCT", 3)
    lines = [
        "<b>[1H] FVG - " + setup.symbol + "</b>",
        "====================",
        "Directie: <b>" + dir_text + "</b>",
        "Entry: <code>" + str(round(setup.entry, 6)) + "</code>",
        "Gap: <code>" + str(round(setup.gap_bot, 6)) + "</code> - <code>" + str(round(setup.gap_top, 6)) + "</code>",
        "ATR: <code>" + str(round(setup.atr, 6)) + "</code>",
        "RSI: " + str(setup.rsi) + " | Slope: " + str(round(setup.slope_fast, 3)) + "%",
        "====================",
        "Guardian: SL=-" + str(sl_pct) + "% | BE@+" + str(be_pct) + "% | TRAIL@+" + str(trail_pct) + "% (step " + str(trail_step) + "%)",
        "Expira in " + str(config.ORDER_EXPIRY_HOURS) + "h",
    ]
    _send("\n".join(lines))


def notify_trade(setup, success):
    if not success:
        _send("<b>[1H] ORDIN ESUAT</b>\n" + setup.symbol + " | " + setup.direction + " @ " + str(round(setup.entry, 6)))


def notify_trade_closed(symbol, direction, entry, result, pnl_usdt,
                        open_time, close_time, rsi=0.0, duration_h=0.0, **kwargs):
    r_text = RESULT_LABELS.get(result, result)
    sign = "+" if pnl_usdt >= 0 else ""
    dir_text = "LONG" if direction in ("BUY", "BULL") else "SHORT"
    capital_efectiv = config.USDT_PER_TRADE * config.LEVERAGE
    roi_pct = round(pnl_usdt / capital_efectiv * 100, 1) if capital_efectiv > 0 else 0.0
    roi_sign = "+" if roi_pct >= 0 else ""
    t_open_fmt = open_time[:16].replace("T", " ") if len(open_time) >= 16 else open_time
    t_close_fmt = close_time[:16].replace("T", " ") if len(close_time) >= 16 else close_time
    lines = [
        "<b>[1H] " + symbol + " - " + r_text + "</b>",
        "====================",
        "Directie: <b>" + dir_text + "</b>",
        "Entry: <code>" + str(round(entry, 6)) + "</code>",
        "====================",
        "PNL: <b>" + sign + str(round(pnl_usdt, 4)) + " USDT</b> (" + roi_sign + str(roi_pct) + "% ROI)",
        "Durata: <b>" + str(round(duration_h, 1)) + "h</b>",
        "RSI intrare: " + str(round(rsi, 1)),
        "====================",
        "<i>" + t_open_fmt + " -&gt; " + t_close_fmt + " UTC</i>",
    ]
    _send("\n".join(lines))


def notify_error(context, error):
    _send("<b>[1H] EROARE</b>\n<b>" + str(context) + "</b>\n<code>" + str(error)[:300] + "</code>")


def send_statistics_report(stats):
    total = int(stats.get("total_trades", 0) or 0)
    wins = int(stats.get("wins", 0) or 0)
    losses = int(stats.get("losses", 0) or 0)
    be = int(stats.get("be", 0) or 0)
    expired = int(stats.get("expired_orders", 0) or 0)
    pending = int(stats.get("pending", 0) or 0)
    open_pos = int(stats.get("open_positions", 0) or 0)
    pnl = float(stats.get("pnl_total", 0.0) or 0.0)
    pnl_today = float(stats.get("pnl_today", 0.0) or 0.0)
    wr = float(stats.get("win_rate", 0.0) or 0.0)
    best = float(stats.get("best_trade", 0.0) or 0.0)
    worst = float(stats.get("worst_trade", 0.0) or 0.0)
    started = str(stats.get("start_time", "?"))
    dll_today = float(stats.get("dll_today", 0.0) or 0.0)

    capital = float(stats.get("capital", config.USDT_PER_TRADE * config.MAX_OPEN_TRADES))
    dll_limit_usdt = capital * config.DAILY_LOSS_LIMIT_PCT
    dll_limit_pct = int(config.DAILY_LOSS_LIMIT_PCT * 100)
    dll_status = "STOP" if dll_today <= -dll_limit_usdt else "OK"

    wr_label = "EXCELENT" if wr >= 65 else ("BUN" if wr >= 50 else "ATENTIE")
    pnl_sign = "+" if pnl >= 0 else ""
    ptd_sign = "+" if pnl_today >= 0 else ""

    uptime_str = ""
    try:
        t_start = datetime.strptime(started, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        uptime_h = (datetime.now(timezone.utc) - t_start).total_seconds() / 3600
        uptime_str = " (uptime " + str(int(uptime_h)) + "h)" if uptime_h < 24 else " (uptime " + str(round(uptime_h / 24, 1)) + "z)"
    except Exception:
        pass

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if total == 0:
        trades_line = "Niciun trade inchis inca"
    else:
        trades_line = "Win Rate: <b>" + str(round(wr, 1)) + "%</b> [" + wr_label + "] (" + str(wins) + "W / " + str(be) + "BE / " + str(losses) + "L / " + str(expired) + "EXP)"

    lines = [
        "<b>[1H] RAPORT BOT FVG - " + now_str + "</b>",
        "====================",
        "<b>REZULTATE</b>",
        "Trades: <b>" + str(total) + "</b>",
        trades_line,
        "PNL Total: <b>" + pnl_sign + str(round(pnl, 4)) + " USDT</b>",
        "Azi: <b>" + ptd_sign + str(round(pnl_today, 4)) + " USDT</b>",
    ]

    if total > 0:
        lines.append("Best: <b>+" + str(round(best, 4)) + "</b> | Worst: <b>" + str(round(worst, 4)) + "</b>")

    lines += [
        "====================",
        "<b>SITUATIE</b>",
        "Pozitii: <b>" + str(open_pos) + "</b>/" + str(config.MAX_OPEN_TRADES) + " | Pending: <b>" + str(pending) + "</b>",
        "DLL [" + dll_status + "]: " + str(round(dll_today, 2)) + " USDT (limita: " + str(dll_limit_pct) + "% = -" + str(round(dll_limit_usdt, 1)) + " USDT)",
        "Capital: <b>" + str(round(capital, 2)) + " USDT</b>",
        "====================",
        "<i>Pornit: " + started + uptime_str + "</i>",
    ]

    _send("\n".join(lines))
