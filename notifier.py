"""
Notifier 1H — Telegram notificari si rapoarte
"""
import requests, logging
from datetime import datetime, timezone
import config

logger = logging.getLogger("FVGBot1H")


def _send(text: str):
    if not config.TELEGRAM_ENABLED:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Telegram: {e}")


def notify_setup(setup):
    dir_emoji = "🟢 LONG" if setup.direction == "BULL" else "🔴 SHORT"
    _send(
        f"<b>📡 [1H] FVG — {setup.symbol}</b>\n"
        f"Directie: {dir_emoji}\n"
        f"Entry:  <code>{setup.entry:.6f}</code>\n"
        f"SL:     <code>{setup.sl:.6f}</code>\n"
        f"TP:     <code>{setup.tp:.6f}</code>\n"
        f"RSI: {setup.rsi} | Slope: {setup.slope_fast:+.3f}%\n"
        f"⏰ Expira in {config.ORDER_EXPIRY_HOURS}h"
    )


def notify_trade(setup, success: bool):
    if not success:
        _send(f"⚠️ [1H] {setup.symbol} — ordin ESUAT")


def notify_trade_closed(symbol, direction, entry, sl, tp,
                        result, pnl_usdt, open_time, close_time,
                        rsi=0.0, duration_h=0.0):
    if result == "TP":
        emoji = "✅"; r_text = "TAKE PROFIT"; sign = "+"
    elif result == "SL":
        emoji = "❌"; r_text = "STOP LOSS";   sign = ""
    else:
        emoji = "⏰"; r_text = "TIMEOUT";      sign = "+" if pnl_usdt >= 0 else ""

    dir_emoji = "🟢 LONG" if direction in ("BUY","BULL") else "🔴 SHORT"
    pnl_emoji = "📈" if pnl_usdt >= 0 else "📉"
    risk  = abs(entry - sl)
    rw    = abs(entry - tp)
    rr    = round(rw/risk, 2) if risk > 0 else 0

    _send(
        f"{emoji} <b>[1H] {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Rezultat:  <b>{r_text}</b>\n"
        f"Directie:  {dir_emoji}\n"
        f"Entry:     <code>{entry:.6f}</code>\n"
        f"SL:        <code>{sl:.6f}</code>\n"
        f"TP:        <code>{tp:.6f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{pnl_emoji} PNL: <b>{sign}{pnl_usdt:.4f} USDT</b>\n"
        f"⏱ Durata: <b>{duration_h:.1f}h</b>\n"
        f"📊 RSI: {rsi:.1f} | RR: 1:{rr}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{open_time[:16].replace('T',' ')} → {close_time[:16].replace('T',' ')} UTC</i>"
    )


def notify_error(context, error):
    _send(f"🔥 <b>[1H] EROARE</b>\n{context}\n<code>{str(error)[:200]}</code>")


def send_statistics_report(stats: dict):
    total    = int(stats.get("total_trades", 0) or 0)
    wins     = int(stats.get("wins", 0) or 0)
    losses   = int(stats.get("losses", 0) or 0)
    pending  = int(stats.get("pending", 0) or 0)
    open_pos = int(stats.get("open_positions", 0) or 0)
    pnl      = float(stats.get("pnl_total", 0.0) or 0.0)
    pnl_today= float(stats.get("pnl_today", 0.0) or 0.0)
    wr       = float(stats.get("win_rate", 0.0) or 0.0)
    best     = float(stats.get("best_trade", 0.0) or 0.0)
    worst    = float(stats.get("worst_trade", 0.0) or 0.0)
    started  = str(stats.get("start_time", "?"))
    dll_today= float(stats.get("dll_today", 0.0) or 0.0)
    dll_limit= config.DAILY_LOSS_LIMIT_PCT * 100

    pnl_sign  = "+" if pnl >= 0 else ""
    ptd_sign  = "+" if pnl_today >= 0 else ""
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    wr_emoji  = "🔥" if wr >= 65 else ("✅" if wr >= 50 else "⚠️")
    dll_icon  = "⛔" if dll_today <= -(config.DAILY_LOSS_LIMIT_PCT * 500) else "✅"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if total == 0:
        trades_line = "Niciun trade inchis inca"
    else:
        trades_line = f"{wr_emoji} Win Rate: <b>{wr:.1f}%</b> ({wins}✅ / {losses}❌)"

    msg = (
        f"<b>📊 [1H] RAPORT BOT FVG — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>REZULTATE (bot 1H)</b>\n"
        f"Trade-uri inchise: <b>{total}</b>\n"
        f"{trades_line}\n"
        f"{pnl_emoji} PNL Total: <b>{pnl_sign}{pnl:.4f} USDT</b>\n"
        f"   Azi: <b>{ptd_sign}{pnl_today:.4f} USDT</b>\n"
    )
    if total > 0:
        msg += f"   Best: +{best:.4f} | Worst: {worst:.4f}\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>SITUATIE</b>\n"
        f"Pozitii: <b>{open_pos}</b>/{config.MAX_OPEN_TRADES} | Pending: <b>{pending}</b>\n"
        f"{dll_icon} DLL azi: {dll_today:+.2f} USDT (limita: -{dll_limit:.0f}%)\n"
        f"De la: {started}"
    )
    _send(msg)
