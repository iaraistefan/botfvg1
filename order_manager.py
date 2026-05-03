"""
═══════════════════════════════════════════════════════════
  ORDER MANAGER 1H — v10 (după backtest v4)
═══════════════════════════════════════════════════════════
Modificări față de v9:
  ✓ Detectează result granular: TP / SL / BE / TRAIL / EC (din PNL ROI)
  ✓ Stochează gap_top, gap_bot, atr în state (pentru Guardian context)
  ✓ Eliminat sl/tp cosmetic din state
  ✓ Compatibil cu detector v10 (FVGSetup are gap_top/gap_bot)
"""
import logging, json, os
import time as t

from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE

logger = logging.getLogger("FVGBot1H")


def _save_state(pending, active, closed, daily_pnl=None):
    try:
        sf = getattr(config, "STATE_FILE", "bot_state_1h.json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({
                "pending_orders":   pending,
                "active_positions": active,
                "closed_trades":    closed,
                "daily_pnl":        daily_pnl or {},
            }, f, indent=2)
    except Exception as e:
        logger.error(f"_save_state error: {e}")


def _load_state():
    try:
        sf = getattr(config, "STATE_FILE", "bot_state_1h.json")
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


def _classify_result_by_roi(pnl_usdt: float) -> str:
    """
    Determină tipul de exit pe baza PNL în USDT:
      pnl > 0 → în general TP/TRAIL (ambele sunt "win")
      pnl ≈ 0 (-1% ROI ≈ -0.07 USDT) → BE (breakeven move)
      pnl < BE region → SL
      pnl < EC region → EC
    
    Pentru raportare avem nevoie de ROI:
      pnl_usdt = USDT_PER_TRADE × ROI / 100 - commission
      ROI ≈ pnl_usdt × 100 / USDT_PER_TRADE (ignorăm commission pentru clasificare)
    """
    if pnl_usdt >= 0.01:
        # Win — fie TP fix, fie Trailing. Nu putem distinge perfect, raportăm "WIN"
        return "WIN"
    
    # Approximate ROI (% pe USDT_PER_TRADE)
    approx_roi = pnl_usdt * 100 / USDT_PER_TRADE if USDT_PER_TRADE > 0 else 0
    
    # Fees ~0.08% pe leverage 10 = ~0.8% ROI cost = ~0.056 USDT pentru 7 USDT
    # BE move = -1% ROI = -0.07 USDT (acoperă fees)
    # Deci tot ce e între -2% și 0% ROI e probabil BE
    if approx_roi >= -2.0:
        return "BE"
    elif approx_roi >= -28.0:  # SL_NATURAL = -25%, dar cu slippage poate ajunge la -28
        return "SL"
    else:
        return "EC"


class OrderManager:
    def __init__(self, client: Client):
        self.client = client
        self._precision_cache = {}
        self.pending_orders, self.active_positions, self.closed_trades, self.daily_pnl = _load_state()

    def _save(self):
        _save_state(self.pending_orders, self.active_positions,
                    self.closed_trades, self.daily_pnl)

    def reconcile_with_binance(self):
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
                    "open_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":   int(t.time() * 1000) - 86400000,
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
                    "open_time":  t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":    int(t.time() * 1000),
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

        except Exception as e:
            if "-1003" in str(e):
                logger.warning("reconcile: rate limit — astept 60s...")
                t.sleep(60)
            else:
                logger.error(f"reconcile error: {e}")

    def _get_symbol_info(self, symbol):
        if symbol not in self._precision_cache:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] == symbol:
                    tick = float(next(
                        f["tickSize"] for f in s["filters"]
                        if f["filterType"] == "PRICE_FILTER"
                    ))
                    self._precision_cache[symbol] = {
                        "price_prec": int(s["pricePrecision"]),
                        "qty_prec":   int(s["quantityPrecision"]),
                        "tick_size":  tick,
                    }
                    break
        return self._precision_cache.get(symbol, {})

    def _round_price(self, price, tick, decimals):
        return round(round(price / tick) * tick, decimals)

    def _calc_qty(self, entry, info):
        return round((USDT_PER_TRADE * LEVERAGE) / entry, info["qty_prec"])

    def check_filled_orders(self):
        c1 = self._check_pending()
        c2 = self._check_active_positions()
        c3 = self._expire_old_orders()
        if c1 or c2 or c3:
            self._save()

    def _check_pending(self) -> bool:
        if not self.pending_orders:
            return False
        try:
            open_orders = self.client.futures_get_open_orders()
            open_ids    = {str(o["orderId"]) for o in open_orders}
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("_check_pending: rate limit — skip")
                return False
            logger.error(f"_check_pending: {e}")
            return False
        except Exception as e:
            logger.error(f"_check_pending: {e}")
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
                    "open_ts":   data.get("open_ts", int(t.time() * 1000)),
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
                    "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                })
                to_remove.append(symbol)
                changed = True

        for sym in to_remove:
            self.pending_orders.pop(sym, None)
        return changed

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
                return False
            logger.error(f"_check_active: {e}")
            return False
        except Exception as e:
            logger.error(f"_check_active: {e}")
            return False

        to_close = []
        changed  = False

        for symbol, pos in list(self.active_positions.items()):
            if symbol in real_open:
                continue

            t.sleep(2)  # delay pentru procesare PNL Binance

            try:
                open_ts = int(pos["open_ts"])
                end_ts  = int(t.time() * 1000)
                income  = self.client.futures_income_history(
                    symbol=symbol, incomeType="REALIZED_PNL",
                    startTime=open_ts, endTime=end_ts, limit=20
                )
                pnl = sum(float(x["income"]) for x in income) if income else 0.0

                if pnl == 0.0 and not income:
                    logger.warning(f"[{symbol}] PNL=0 — retry urmator ciclu")
                    t.sleep(3)
                    continue

                # Clasificare granular pe baza ROI
                result = _classify_result_by_roi(pnl)
                close_time = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())
                sign = "+" if pnl >= 0 else ""
                
                emoji_map = {"WIN": "✅", "BE": "🟡", "SL": "❌", "EC": "🔴"}
                emoji = emoji_map.get(result, "⚪")
                
                logger.info(
                    f"[{symbol}] {emoji} {result} | PNL: {sign}{pnl:.4f} USDT (Guardian)"
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

                today = t.strftime("%Y-%m-%d", t.gmtime())
                self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + pnl

                try:
                    from notifier import notify_trade_closed
                    dur_h = (end_ts - open_ts) / 3600000
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
                    logger.warning(f"[{symbol}] notify error: {ne}")

                try:
                    import journal
                    journal.log_trade(
                        symbol=symbol,
                        direction=pos["direction"],
                        entry=pos["entry"],
                        sl=0,    # nu mai folosim — Guardian dynamic
                        tp=0,    # nu mai folosim — Guardian dynamic
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

            except Exception as e:
                logger.error(f"[{symbol}] get PNL error: {e}")

        for sym in to_close:
            self.active_positions.pop(sym, None)
        return changed

    def _expire_old_orders(self) -> bool:
        expiry_ms = config.ORDER_EXPIRY_HOURS * 3600 * 1000
        now_ms    = int(t.time() * 1000)
        to_expire = []
        changed   = False

        for symbol, oi in list(self.pending_orders.items()):
            if now_ms - oi.get("open_ts", now_ms) >= expiry_ms:
                age_h = (now_ms - oi.get("open_ts", now_ms)) / 3600000
                logger.info(f"[{symbol}] Expirat dupa {age_h:.1f}h — anulez...")
                try:
                    self.client.futures_cancel_order(
                        symbol=symbol, orderId=oi["order_id"]
                    )
                    self.closed_trades.append({
                        "symbol":     symbol,
                        "direction":  oi.get("direction", "?"),
                        "entry":      oi.get("entry", 0),
                        "result":     "EXPIRED",
                        "pnl":        0.0,
                        "open_time":  oi.get("open_time", ""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    changed = True
                except Exception as e:
                    logger.error(f"[{symbol}] cancel: {e}")
                to_expire.append(symbol)

        for sym in to_expire:
            self.pending_orders.pop(sym, None)
        return changed

    def get_bot_stats(self) -> dict:
        # Acum incluzând BE și TRAIL în "wins/losses" — clasificate pe pnl
        closed  = [x for x in self.closed_trades if x["result"] in ("WIN", "BE", "SL", "EC", "TP", "TRAIL")]
        expired = [x for x in self.closed_trades if x["result"] == "EXPIRED"]
        if not closed:
            return {
                "total": 0, "wins": 0, "losses": 0, "be": 0, "expired": len(expired),
                "pnl_total": 0.0, "pnl_today": 0.0, "win_rate": 0.0,
                "best": 0.0, "worst": 0.0,
                "active": len(self.active_positions),
                "pending": len(self.pending_orders),
            }
        wins   = [x for x in closed if x["pnl"] > 0]
        losses = [x for x in closed if x["pnl"] < 0]
        be     = [x for x in closed if x["result"] == "BE"]
        pnls   = [x["pnl"] for x in closed]
        today = t.strftime("%Y-%m-%d", t.gmtime())
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
            "win_rate":  round(len(wins) / len(closed) * 100, 1),
            "best":      round(max(pnls), 4),
            "worst":     round(min(pnls), 4),
            "active":    len(self.active_positions),
            "pending":   len(self.pending_orders),
        }

    def set_leverage(self, symbol):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except BinanceAPIException as e:
            logger.warning(f"[{symbol}] leverage: {e}")

    def place_fvg_trade(self, setup: FVGSetup) -> bool:
        symbol = setup.symbol
        try:
            info    = self._get_symbol_info(symbol)
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
            open_ts    = int(t.time() * 1000)
            open_time  = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())

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
            else:
                logger.error(f"[{symbol}] BinanceAPIException: {e}")
            return False
        except Exception as e:
            logger.error(f"[{symbol}] Eroare: {e}")
            return False

    def count_active_trades(self):
        return len(self.pending_orders) + len(self.active_positions)

    def has_symbol(self, symbol):
        return symbol in self.pending_orders or symbol in self.active_positions

    def is_at_capacity(self):
        return self.count_active_trades() >= config.MAX_OPEN_TRADES
