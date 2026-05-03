"""
Notifier 1H — Telegram notificări si rapoarte (v10)
Actualizat pentru sistemul Trailing+BE — fără SL/TP fix afișat.
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
        f"Direcție: {dir_emoji}\n"
        f"Entry:  <code>{setup.entry:.6f}</code>\n"
        f"Gap: <code>{setup.gap_bot:.6f}</code> ↔ <code>{setup.gap_top:.6f}</code>\n"
        f"RSI: {setup.rsi} | Slope: {setup.slope_fast:+.3f}%\n"
        f"🛡 Guardian: SL=-25% | BE@+3% | TRAIL@+5% (step 3%)\n"
        f"⏰ Expiră în {config.ORDER_EXPIRY_HOURS}h"
    )


def notify_trade(setup, success: bool):
    if not success:
        _send(f"⚠️ [1H] {setup.symbol} — ordin EȘUAT")


def notify_trade_closed(symbol, direction, entry,
                        result, pnl_usdt, open_time, close_time,
                        rsi=0.0, duration_h=0.0, **kwargs):
    """
    Notificare trade închis. result poate fi:
      WIN  — câștig (TP fix sau Trailing exit profitabil)
      BE   — breakeven (mic loss ~ -0.07 USDT)
      SL   — stop loss
      EC   — emergency close (failsafe)
    """
    emoji_map = {
        "WIN":  ("✅", "WIN"),
        "TP":   ("✅", "TAKE PROFIT"),
        "TRAIL":("🚀", "TRAILING EXIT"),
        "BE":   ("🟡", "BREAKEVEN"),
        "SL":   ("❌", "STOP LOSS"),
        "EC":   ("🔴", "EMERGENCY CLOSE"),
    }
    emoji, r_text = emoji_map.get(result, ("⚪", result))
    sign = "+" if pnl_usdt >= 0 else ""
    
    dir_emoji = "🟢 LONG" if direction in ("BUY","BULL") else "🔴 SHORT"
    pnl_emoji = "📈" if pnl_usdt >= 0 else "📉"

    _send(
        f"{emoji} <b>[1H] {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Rezultat:  <b>{r_text}</b>\n"
        f"Direcție:  {dir_emoji}\n"
        f"Entry:     <code>{entry:.6f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{pnl_emoji} PNL: <b>{sign}{pnl_usdt:.4f} USDT</b>\n"
        f"⏱ Durată: <b>{duration_h:.1f}h</b>\n"
        f"📊 RSI la intrare: {rsi:.1f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{open_time[:16].replace('T',' ')} → {close_time[:16].replace('T',' ')} UTC</i>"
    )


def notify_error(context, error):
    _send(f"🔥 <b>[1H] EROARE</b>\n{context}\n<code>{str(error)[:200]}</code>")


def send_statistics_report(stats: dict):
    total    = int(stats.get("total_trades", 0) or 0)
    wins     = int(stats.get("wins", 0) or 0)
    losses   = int(stats.get("losses", 0) or 0)
    be       = int(stats.get("be", 0) or 0)
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
        trades_line = "Niciun trade închis încă"
    else:
        trades_line = f"{wr_emoji} Win Rate: <b>{wr:.1f}%</b> ({wins}✅ / {be}🟡 / {losses}❌)"

    msg = (
        f"<b>📊 [1H] RAPORT BOT FVG — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>REZULTATE (bot 1H)</b>\n"
        f"Trade-uri închise: <b>{total}</b>\n"
        f"{trades_line}\n"
        f"{pnl_emoji} PNL Total: <b>{pnl_sign}{pnl:.4f} USDT</b>\n"
        f"   Azi: <b>{ptd_sign}{pnl_today:.4f} USDT</b>\n"
    )
    if total > 0:
        msg += f"   Best: +{best:.4f} | Worst: {worst:.4f}\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>SITUAȚIE</b>\n"
        f"Poziții: <b>{open_pos}</b>/{config.MAX_OPEN_TRADES} | Pending: <b>{pending}</b>\n"
        f"{dll_icon} DLL azi: {dll_today:+.2f} USDT (limită: -{dll_limit:.0f}%)\n"
        f"De la: {started}"
    )
    _send(msg)
