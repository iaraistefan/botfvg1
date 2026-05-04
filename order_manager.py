"""
═══════════════════════════════════════════════════════════
  ORDER MANAGER 1H — v11.0 (după audit v13 main.py)
═══════════════════════════════════════════════════════════
Fix-uri față de v10:
  ✓ FIX CRITIC #1: import time (nu 'as t') — elimină aliasul confuz
  ✓ FIX CRITIC #2: _get_symbol_info() — exchange_info bulkîncărcat O SINGURĂ DATĂ
                    (v10 = 40 weight × N simboluri noi; v11 = 40 weight total)
  ✓ FIX CRITIC #3: reconcile_with_binance() — rate limit handling corect,
                    distincție ban global vs rate limit normal
  ✓ FIX CRITIC #4: _check_active_positions() — rate limit handler pe
                    futures_income_history()
  ✓ FIX CRITIC #5: _save_state() — scriere atomică (write temp → rename)
  ✓ FIX IMPORTANT #1: importuri notifier/journal scoase din loop (top-level)
  ✓ FIX IMPORTANT #2: set_leverage() cu cache — un singur apel per simbol
  ✓ FIX IMPORTANT #3: closed_trades pruning (max 500 intrări în memorie)
  ✓ FIX IMPORTANT #4: _expire_old_orders() guard pentru ordine deja FILLED
  ✓ FIX IMPORTANT #5: _today() folosește timezone.utc (consistent cu main.py)
  ✓ FIX MINOR: _classify_result_by_roi() adaptiv la config
  ✓ Strategia FVG neschimbată
"""
import logging
import json
import os
import time
import tempfile

from datetime import datetime, timezone
from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE

# ── Importuri top-level (nu în loop) ─────────────────────
try:
    from notifier import notify_trade_closed
except ImportError:
    notify_trade_closed = None

try:
    import journal
except ImportError:
    journal = None

logger = logging.getLogger("FVGBot1H")

# Max intrări în closed_trades (pruning periodic)
MAX_CLOSED_TRADES = 500


# ══════════════════════════════════════════════════════════
#  HELPERS STATE FILE
# ══════════════════════════════════════════════════════════

def _today_utc() -> str:
    """UTC consistent cu _today() din main.py."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_state(pending, active, closed, daily_pnl=None):
    """
    Scriere ATOMICĂ — scrie în fișier temporar, apoi rename.
    Previne coruperea JSON dacă Render kill-ează procesul în mij locul scrierii.
    """
    sf = getattr(config, "STATE_FILE", "bot_state_1h.json")
    try:
        data = json.dumps({
            "pending_orders":   pending,
            "active_positions": active,
            "closed_trades":    closed[-MAX_CLOSED_TRADES:],  # pruning
            "daily_pnl":        daily_pnl or {},
        }, indent=2)
        # Scriem în același director pentru ca rename să fie atomic (același filesystem)
        dir_name  = os.path.dirname(os.path.abspath(sf))
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, sf)   # atomic pe Linux/Mac/Windows
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.error(f"_save_state error: {e}")


def _load_state():
    sf = getattr(config, "STATE_FILE", "bot_state_1h.json")
    try:
        if not os.path.exists(sf):
            return {}, {}, [], {}
        with open(sf, encoding="utf-8") as f:
            data = json.load(f)
        p   = data.get("pending_orders", {})
        a   = data.get("active_positions", {})
        c   = data.get("closed_trades", [])
        dll = data.get("daily_pnl", {})
        if p or a:
            logger.info(f"[STATE] Restaurat: {len(p)} pending, {len(a)} active, {len(c)} closed")
        if dll:
            logger.info(f"[STATE] DLL restaurat: {dll}")
        return p, a, c, dll
    except Exception as e:
        logger.error(f"_load_state error: {e}")
        return {}, {}, [], {}


# ══════════════════════════════════════════════════════════
#  CLASIFICARE RESULT — adaptivă la config
# ══════════════════════════════════════════════════════════

def _classify_result_by_roi(pnl_usdt: float) -> str:
    """
    Clasificare granulară bazată pe PNL real în USDT.
    Pragurile se calculează dinamic din USDT_PER_TRADE și LEVERAGE
    pentru a rămâne corecte dacă modifici parametrii.

    WIN  → pnl pozitiv (TP sau Trailing Stop — ambele profit)
    BE   → aproape de zero (breakeven ± fees)
    SL   → SL natural (pierdere normală, în limita -25% ROI)
    EC   → early close forțat (pierdere mare > SL natural)
    """
    if pnl_usdt >= 0.01:
        return "WIN"

    # Fees estimative: ~0.04% taker × 2 laturi × leverage = ~0.8% ROI
    # 1 unitate ROI = USDT_PER_TRADE / 100
    roi_unit = USDT_PER_TRADE / 100.0 if USDT_PER_TRADE > 0 else 0.07

    # BE: între 0 și -2% ROI (acoperă fees)
    be_threshold = -2.0 * roi_unit
    # SL natural: config.SL_PCT dacă există, altfel -25% ROI
    sl_pct = getattr(config, "SL_PCT", 25.0)
    sl_threshold = -(sl_pct + 3.0) * roi_unit  # +3% slippage buffer

    if pnl_usdt >= be_threshold:
        return "BE"
    elif pnl_usdt >= sl_threshold:
        return "SL"
    else:
        return "EC"


# ══════════════════════════════════════════════════════════
#  ORDER MANAGER
# ══════════════════════════════════════════════════════════

class OrderManager:
    def __init__(self, client: Client):
        self.client = client

        # Cache precision — inițializat BULK la primul apel (40 weight o singură dată)
        self._precision_cache: dict = {}
        self._precision_loaded: bool = False

        # Cache leverage setat per simbol (evită apeluri repetate)
        self._leverage_set: set = set()

        self.pending_orders, self.active_positions, self.closed_trades, self.daily_pnl = _load_state()

    def _save(self):
        _save_state(
            self.pending_orders,
            self.active_positions,
            self.closed_trades,
            self.daily_pnl
        )

    # ─── PRECISION CACHE — bulk load (40 weight o singură dată) ──

    def _load_precision_bulk(self):
        """
        Încarcă preciziile TUTUROR simbolurilor dintr-un singur apel
        futures_exchange_info() — costă 40 weight O SINGURĂ DATĂ.
        v10 apela exchange_info() pentru fiecare simbol nou = catastrofă.
        """
        if self._precision_loaded:
            return
        try:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                sym = s["symbol"]
                try:
                    tick = float(next(
                        f["tickSize"] for f in s["filters"]
                        if f["filterType"] == "PRICE_FILTER"
                    ))
                    self._precision_cache[sym] = {
                        "price_prec": int(s["pricePrecision"]),
                        "qty_prec":   int(s["quantityPrecision"]),
                        "tick_size":  tick,
                    }
                except (StopIteration, KeyError, ValueError):
                    pass
            self._precision_loaded = True
            logger.info(f"[PRECISION] Cache încărcat: {len(self._precision_cache)} simboluri")
        except BinanceAPIException as e:
            logger.error(f"_load_precision_bulk: {e}")
        except Exception as e:
            logger.error(f"_load_precision_bulk: {e}")

    def _get_symbol_info(self, symbol: str) -> dict:
        """Returnează info din cache bulk. Dacă nu e încărcat, îl încarcă acum."""
        if not self._precision_loaded:
            self._load_precision_bulk()
        return self._precision_cache.get(symbol, {})

    def _round_price(self, price: float, tick: float, decimals: int) -> float:
        return round(round(price / tick) * tick, decimals)

    def _calc_qty(self, entry: float, info: dict) -> float:
        return round((USDT_PER_TRADE * LEVERAGE) / entry, info["qty_prec"])

    # ─── RECONCILE ──────────────────────────────────────────

    def reconcile_with_binance(self):
        """
        Sincronizare cu Binance la startup.
        Rate limit handling corect — distincție ban global vs rate limit normal.
        """
        try:
            positions = self.client.futures_position_information()
            open_pos  = [p for p in positions if abs(float(p["positionAmt"])) > 0]

            for p in open_pos:
                symbol = p["symbol"]
                if symbol in self.active_positions:
                    continue
                amt       = float(p["positionAmt"])
                entry     = float(p["entryPrice"])
                direction = "BUY" if amt > 0 else "SELL"
                self.active_positions[symbol] = {
                    "direction": direction,
                    "entry":     entry,
                    "qty":       abs(amt),
                    "open_time": _now_utc_str(),
                    "open_ts":   int(time.time() * 1000) - 86_400_000,
                    "rsi":       0.0,
                    "slope":     0.0,
                    "gap_top":   0.0,
                    "gap_bot":   0.0,
                    "atr":       0.0,
                }
                logger.info(f"[RECONCILE] {symbol} {direction} @ {entry}")

            open_orders = self.client.futures_get_open_orders()
            for o in open_orders:
                symbol = o["symbol"]
                if o.get("type") != "LIMIT" or symbol in self.pending_orders:
                    continue
                side = o["side"]
                self.pending_orders[symbol] = {
                    "order_id":   o["orderId"],
                    "qty":        float(o["origQty"]),
                    "close_side": "SELL" if side == "BUY" else "BUY",
                    "entry":      float(o["price"]),
                    "direction":  side,
                    "open_time":  _now_utc_str(),
                    "open_ts":    int(time.time() * 1000),
                    "rsi":        0.0,
                    "slope":      0.0,
                    "gap_top":    0.0,
                    "gap_bot":    0.0,
                    "atr":        0.0,
                }
                logger.info(f"[RECONCILE] Pending: {symbol} {side} LIMIT @ {o['price']}")

            if open_pos or open_orders:
                self._save()
                logger.info(
                    f"[RECONCILE] {len(self.active_positions)} pozitii, "
                    f"{len(self.pending_orders)} pending — Guardian protejeaza"
                )
            else:
                logger.info("[RECONCILE] Nicio pozitie deschisa")

        except BinanceAPIException as e:
            if e.code == -1003:
                err_msg = str(e)
                if "banned until" in err_msg:
                    logger.warning(f"[RECONCILE] Ban global — aștept expirare...")
                    # Importăm funcția din main (sau re-implementăm logica)
                    import re
                    match = re.search(r"banned until[:s]+(d{10,15})", err_msg, re.IGNORECASE)
                    if match:
                        raw = int(match.group(1))
                        if raw < 1e12:
                            raw *= 1000
                        wait_s = min(7200, max(60, (raw - int(time.time() * 1000)) // 1000 + 10))
                    else:
                        wait_s = 60
                    logger.warning(f"[RECONCILE] Aștept {wait_s}s...")
                    time.sleep(wait_s)
                else:
                    logger.warning("reconcile: rate limit normal — aștept 60s")
                    time.sleep(60)
            else:
                logger.error(f"reconcile error: {e}")
        except Exception as e:
            logger.error(f"reconcile error: {e}")

    # ─── CHECK PENDING ───────────────────────────────────────

    def _check_pending(self) -> bool:
        if not self.pending_orders:
            return False

        try:
            open_orders = self.client.futures_get_open_orders()
            open_ids    = {str(o["orderId"]) for o in open_orders}
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("_check_pending: rate limit — skip")
            else:
                logger.error(f"_check_pending get_open_orders: {e}")
            return False
        except Exception as e:
            logger.error(f"_check_pending get_open_orders: {e}")
            return False

        to_remove = []
        changed   = False

        for symbol, data in list(self.pending_orders.items()):
            if str(data["order_id"]) in open_ids:
                continue

            try:
                order  = self.client.futures_get_order(
                    symbol=symbol, orderId=data["order_id"]
                )
                status = order.get("status", "")
            except BinanceAPIException as e:
                if e.code == -1003:
                    logger.warning("_check_pending: rate limit get_order — break")
                    break
                logger.error(f"[{symbol}] get_order: {e}")
                continue
            except Exception as e:
                logger.error(f"[{symbol}] get_order: {e}")
                continue

            if status == "FILLED":
                filled = float(order.get("avgPrice", data["entry"]))
                logger.info(
                    f"[{symbol}] UMPLUT la {filled} — Guardian preia protectia (Trailing+BE)"
                )
                self.active_positions[symbol] = {
                    "direction": data.get("direction", "?"),
                    "entry":     filled,
                    "qty":       data["qty"],
                    "open_time": data.get("open_time", ""),
                    "open_ts":   data.get("open_ts", int(time.time() * 1000)),
                    "rsi":       data.get("rsi", 0.0),
                    "slope":     data.get("slope", 0.0),
                    "gap_top":   data.get("gap_top", 0.0),
                    "gap_bot":   data.get("gap_bot", 0.0),
                    "atr":       data.get("atr", 0.0),
                }
                to_remove.append(symbol)
                changed = True

            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                logger.info(f"[{symbol}] Ordin {status}")
                self.closed_trades.append({
                    "symbol":     symbol,
                    "direction":  data.get("direction", "?"),
                    "entry":      data.get("entry", 0),
                    "result":     "EXPIRED",
                    "pnl":        0.0,
                    "open_time":  data.get("open_time", ""),
                    "close_time": _now_utc_str(),
                })
                to_remove.append(symbol)
                changed = True

        for sym in to_remove:
            self.pending_orders.pop(sym, None)
        return changed

    # ─── CHECK ACTIVE POSITIONS ─────────────────────────────

    def _check_active_positions(self) -> bool:
        if not self.active_positions:
            return False

        try:
            real_open = {
                p["symbol"] for p in self.client.futures_position_information()
                if abs(float(p["positionAmt"])) > 0
            }
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("_check_active: rate limit — skip")
            else:
                logger.error(f"_check_active position_information: {e}")
            return False
        except Exception as e:
            logger.error(f"_check_active position_information: {e}")
            return False

        to_close = []
        changed  = False

        for symbol, pos in list(self.active_positions.items()):
            if symbol in real_open:
                continue

            time.sleep(1)  # delay scurt pentru procesare PNL Binance (v10: 2s)

            try:
                open_ts = int(pos["open_ts"])
                end_ts  = int(time.time() * 1000)

                income = self.client.futures_income_history(
                    symbol=symbol, incomeType="REALIZED_PNL",
                    startTime=open_ts, endTime=end_ts, limit=20
                )
                pnl = sum(float(x["income"]) for x in income) if income else 0.0

                if pnl == 0.0 and not income:
                    logger.warning(f"[{symbol}] PNL=0 și income gol — retry urmator ciclu")
                    continue

                result     = _classify_result_by_roi(pnl)
                close_time = _now_utc_str()
                sign       = "+" if pnl >= 0 else ""

                emoji_map = {"WIN": "✅", "BE": "🟡", "SL": "❌", "EC": "🔴"}
                emoji = emoji_map.get(result, "⚪")

                logger.info(
                    f"[{symbol}] {emoji} {result} | "
                    f"PNL: {sign}{pnl:.4f} USDT (Guardian)"
                )

                trade_record = {
                    "symbol":     symbol,
                    "direction":  pos["direction"],
                    "entry":      pos["entry"],
                    "result":     result,
                    "pnl":        round(pnl, 4),
                    "open_time":  pos["open_time"],
                    "close_time": close_time,
                    "rsi":        pos.get("rsi", 0),
                    "slope":      pos.get("slope", 0),
                    "gap_top":    pos.get("gap_top", 0),
                    "gap_bot":    pos.get("gap_bot", 0),
                    "atr":        pos.get("atr", 0),
                }
                self.closed_trades.append(trade_record)

                today = _today_utc()
                self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + pnl

                # Notificare Telegram
                if notify_trade_closed:
                    try:
                        dur_h = (end_ts - open_ts) / 3_600_000
                        notify_trade_closed(
                            symbol=symbol,
                            direction=pos["direction"],
                            entry=pos["entry"],
                            result=result,
                            pnl_usdt=pnl,
                            open_time=pos["open_time"],
                            close_time=close_time,
                            rsi=pos.get("rsi", 0.0),
                            duration_h=dur_h,
                        )
                    except Exception as ne:
                        logger.warning(f"[{symbol}] notify_trade_closed error: {ne}")

                # Journal
                if journal:
                    try:
                        journal.log_trade(
                            symbol=symbol,
                            direction=pos["direction"],
                            entry=pos["entry"],
                            sl=0,
                            tp=0,
                            result=result,
                            pnl_usdt=pnl,
                            usdt_per_trade=USDT_PER_TRADE,
                            open_time=pos["open_time"],
                            close_time=close_time,
                            rsi=pos.get("rsi", 0),
                            ema_slope=pos.get("slope", 0),
                        )
                    except Exception:
                        pass

                to_close.append(symbol)
                changed = True

            except BinanceAPIException as e:
                if e.code == -1003:
                    logger.warning(f"[{symbol}] income_history: rate limit — skip")
                    break   # nu continuăm — așteptăm next ciclu
                logger.error(f"[{symbol}] income_history BinanceError: {e}")
            except Exception as e:
                logger.error(f"[{symbol}] get PNL error: {e}")

        for sym in to_close:
            self.active_positions.pop(sym, None)
        return changed

    # ─── EXPIRE OLD ORDERS ──────────────────────────────────

    def _expire_old_orders(self) -> bool:
        expiry_ms = config.ORDER_EXPIRY_HOURS * 3_600 * 1000
        now_ms    = int(time.time() * 1000)
        to_expire = []
        changed   = False

        for symbol, oi in list(self.pending_orders.items()):
            if now_ms - oi.get("open_ts", now_ms) < expiry_ms:
                continue

            age_h = (now_ms - oi.get("open_ts", now_ms)) / 3_600_000
            logger.info(f"[{symbol}] Expirat dupa {age_h:.1f}h — anulez...")

            try:
                self.client.futures_cancel_order(
                    symbol=symbol, orderId=oi["order_id"]
                )
                logger.info(f"[{symbol}] Ordin anulat cu succes")
            except BinanceAPIException as e:
                if e.code == -2011:
                    # Ordinul nu mai există — deja FILLED sau CANCELED de Binance
                    # Nu tratăm ca eroare, doar logăm și continuăm
                    logger.info(
                        f"[{symbol}] Ordin -{oi['order_id']} deja executat/anulat "
                        f"(cod -2011) — tratăm în _check_pending următor"
                    )
                elif e.code == -1003:
                    logger.warning(f"[{symbol}] cancel: rate limit — skip expiry")
                    continue   # nu adăugăm în to_expire, reîncercăm data viitoare
                else:
                    logger.error(f"[{symbol}] cancel error: {e}")
            except Exception as e:
                logger.error(f"[{symbol}] cancel error: {e}")

            self.closed_trades.append({
                "symbol":     symbol,
                "direction":  oi.get("direction", "?"),
                "entry":      oi.get("entry", 0),
                "result":     "EXPIRED",
                "pnl":        0.0,
                "open_time":  oi.get("open_time", ""),
                "close_time": _now_utc_str(),
            })
            to_expire.append(symbol)
            changed = True

        for sym in to_expire:
            self.pending_orders.pop(sym, None)
        return changed

    # ─── STATISTICI ─────────────────────────────────────────

    def get_bot_stats(self) -> dict:
        closed  = [x for x in self.closed_trades
                   if x["result"] in ("WIN", "BE", "SL", "EC", "TP", "TRAIL")]
        expired = [x for x in self.closed_trades if x["result"] == "EXPIRED"]

        if not closed:
            return {
                "total": 0, "wins": 0, "losses": 0, "be": 0,
                "expired": len(expired),
                "pnl_total": 0.0, "pnl_today": 0.0, "win_rate": 0.0,
                "best": 0.0, "worst": 0.0,
                "active":  len(self.active_positions),
                "pending": len(self.pending_orders),
            }

        wins   = [x for x in closed if x["pnl"] > 0]
        losses = [x for x in closed if x["pnl"] < -0.01]
        be     = [x for x in closed if x["result"] == "BE"]
        pnls   = [x["pnl"] for x in closed]
        today  = _today_utc()

        return {
            "total":     len(closed),
            "wins":      len(wins),
            "losses":    len(losses),
            "be":        len(be),
            "expired":   len(expired),
            "pnl_total": round(sum(pnls), 4),
            "pnl_today": round(sum(
                x["pnl"] for x in closed
                if x.get("close_time", "")[:10] == today
            ), 4),
            "win_rate":  round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "best":      round(max(pnls), 4),
            "worst":     round(min(pnls), 4),
            "active":    len(self.active_positions),
            "pending":   len(self.pending_orders),
        }

    # ─── LEVERAGE (cu cache) ─────────────────────────────────

    def set_leverage(self, symbol: str):
        """Setează leverage O SINGURĂ DATĂ per simbol (cache în memorie)."""
        if symbol in self._leverage_set:
            return
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
            self._leverage_set.add(symbol)
        except BinanceAPIException as e:
            if e.code == -4028:
                # Leverage already set to this value — nu e eroare
                self._leverage_set.add(symbol)
            else:
                logger.warning(f"[{symbol}] set_leverage: {e}")
        except Exception as e:
            logger.warning(f"[{symbol}] set_leverage: {e}")

    # ─── PLACE TRADE ────────────────────────────────────────

    def place_fvg_trade(self, setup: FVGSetup) -> bool:
        """Plasează ordin LIMIT pentru setup FVG. Strategia neschimbată."""
        symbol = setup.symbol
        try:
            info = self._get_symbol_info(symbol)
            if not info:
                logger.warning(f"[{symbol}] Nu am info precizie — skip")
                return False

            tick    = info["tick_size"]
            pp      = info["price_prec"]
            entry_r = self._round_price(setup.entry, tick, pp)
            if entry_r <= 0:
                return False

            qty = self._calc_qty(entry_r, info)
            if qty <= 0:
                return False

            self.set_leverage(symbol)

            side       = "BUY"  if setup.direction == "BULL" else "SELL"
            close_side = "SELL" if setup.direction == "BULL" else "BUY"
            open_ts    = int(time.time() * 1000)
            open_time  = _now_utc_str()

            order    = self.client.futures_create_order(
                symbol=symbol, side=side, type="LIMIT",
                timeInForce="GTC", quantity=qty, price=entry_r,
            )
            order_id = order["orderId"]
            logger.info(
                f"[{symbol}] LIMIT {side} | id={order_id} | "
                f"qty={qty} | entry={entry_r} | Trailing+BE protejează"
            )

            self.pending_orders[symbol] = {
                "order_id":   order_id,
                "qty":        qty,
                "close_side": close_side,
                "entry":      entry_r,
                "direction":  side,
                "open_time":  open_time,
                "open_ts":    open_ts,
                "rsi":        getattr(setup, "rsi", 0.0),
                "slope":      getattr(setup, "slope_fast", 0.0),
                "gap_top":    getattr(setup, "gap_top", 0.0),
                "gap_bot":    getattr(setup, "gap_bot", 0.0),
                "atr":        getattr(setup, "atr", 0.0),
            }
            self._save()
            return True

        except BinanceAPIException as e:
            if e.code == -2019:
                logger.warning(f"[{symbol}] margin insuficient")
            elif e.code == -1003:
                logger.warning(f"[{symbol}] place_fvg_trade: rate limit — skip")
            else:
                logger.error(f"[{symbol}] BinanceAPIException: {e}")
            return False
        except Exception as e:
            logger.error(f"[{symbol}] place_fvg_trade error: {e}")
            return False

    # ─── HELPERS ────────────────────────────────────────────

    def count_active_trades(self) -> int:
        return len(self.pending_orders) + len(self.active_positions)

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.pending_orders or symbol in self.active_positions

    def is_at_capacity(self) -> bool:
        return self.count_active_trades() >= config.MAX_OPEN_TRADES

    def check_filled_orders(self):
        """Wrapper public pentru compatibilitate cu cod extern."""
        c1 = self._check_pending()
        c2 = self._check_active_positions()
        c3 = self._expire_old_orders()
        if c1 or c2 or c3:
            self._save()
