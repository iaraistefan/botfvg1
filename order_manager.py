"""
Order Manager 1H — cu state persistent local
Supravietuieste restart-urilor de PC prin bot_state_1h.json
"""
import logging, json, os
import time as t


from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE



logger = logging.getLogger("FVGBot")

def _save_state(pending, active, closed):
    """Salveaza starea botului in JSON persistent."""
    try:
        sf = getattr(config, "STATE_FILE", "bot_state.json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({
                "pending_orders":   pending,
                "active_positions": active,
                "closed_trades":    closed,
            }, f, indent=2)
    except Exception as e:
        logger.error(f"_save_state error: {e}")


def _load_state():
    """Incarca starea botului din JSON. Supravietuieste restart-urilor."""
    try:
        sf = getattr(config, "STATE_FILE", "bot_state.json")
        if not os.path.exists(sf):
            return {}, {}, []
        with open(sf, encoding="utf-8") as f:
            data = json.load(f)
        p = data.get("pending_orders", {})
        a = data.get("active_positions", {})
        c = data.get("closed_trades", [])
        if p or a:
            logger.info(
                f"[STATE] Restaurat: {len(p)} pending, {len(a)} active, {len(c)} closed"
            )
        return p, a, c
    except Exception as e:
        logger.error(f"_load_state error: {e}")
        return {}, {}, []

class OrderManager:
    def __init__(self, client: Client):
        self.client = client
        self._precision_cache = {}
        self.pending_orders, self.active_positions, self.closed_trades = _load_state()


    def _save(self):
        _save_state(self.pending_orders, self.active_positions, self.closed_trades)

    # ─────────────────────────────────────────────
    #  RECONCILIERE LA STARTUP
    # ─────────────────────────────────────────────

    def reconcile_with_binance(self):
        """
        La pornire: importa pozitiile botului 1H din Binance.
        IMPORTANT: Reconcilierea se face DOAR daca exista deja un state file
        (adica botul a mai rulat inainte). La primul start, nu importam pozitiile
        altor boturi care ruleaza pe acelasi cont.
        """
        import os
        if not os.path.exists(config.STATE_FILE):
            logger.info("[RECONCILE] Primul start — nu importam pozitii externe")
            return

        # Daca state file exista dar pending/active sunt goale, inseamna
        # ca botul s-a restartat si trebuie sa reimporte propriile pozitii
        if self.pending_orders or self.active_positions:
            logger.info(f"[RECONCILE] State restaurat din fisier — {len(self.active_positions)} active, {len(self.pending_orders)} pending")
            return

        logger.info("[RECONCILE] State gol dupa restart — sincronizez cu Binance...")
        try:
            positions = self.client.futures_position_information()
            open_pos  = [p for p in positions if abs(float(p["positionAmt"])) > 0]

            for p in open_pos:
                symbol = p["symbol"]
                if symbol in self.active_positions:
                    continue
                amt   = float(p["positionAmt"])
                entry = float(p["entryPrice"])
                direction = "BUY" if amt > 0 else "SELL"

                # Cauta SL/TP din ordinele deschise
                sl_p = tp_p = 0.0
                try:
                    orders = self.client.futures_get_open_orders(symbol=symbol)
                    for o in orders:
                        sp = float(o.get("stopPrice", 0))
                        ot = o.get("type", "")
                        if "STOP" in ot and sp > 0:    sl_p = sp
                        elif "PROFIT" in ot and sp > 0: tp_p = sp
                except Exception:
                    pass

                self.active_positions[symbol] = {
                    "direction": direction,
                    "entry":     entry,
                    "sl":        sl_p,
                    "tp":        tp_p,
                    "qty":       abs(amt),
                    "open_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":   int(t.time() * 1000) - 3600000,
                    "rsi":       0.0,
                    "slope":     0.0,
                }
                logger.info(f"[RECONCILE] {symbol} {direction} @ {entry}")

            # Ordine LIMIT pending
            open_orders = self.client.futures_get_open_orders()
            for o in open_orders:
                sym = o["symbol"]
                if o.get("type") != "LIMIT": continue
                if sym in self.pending_orders: continue
                side = o["side"]
                self.pending_orders[sym] = {
                    "order_id":   o["orderId"],
                    "sl":         0.0, "tp": 0.0,
                    "qty":        float(o["origQty"]),
                    "close_side": "SELL" if side == "BUY" else "BUY",
                    "entry":      float(o["price"]),
                    "direction":  side,
                    "open_time":  t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":    int(t.time() * 1000),
                    "rsi":        0.0, "slope": 0.0,
                }
                logger.info(f"[RECONCILE] Pending: {sym} {side} LIMIT @ {o['price']}")

            if open_pos or open_orders:
                self._save()
                logger.info(
                    f"[RECONCILE] {len(self.active_positions)} active, "
                    f"{len(self.pending_orders)} pending"
                )
            else:
                logger.info("[RECONCILE] Nicio pozitie deschisa")

        except Exception as e:
            logger.error(f"reconcile error: {e}")

    # ─────────────────────────────────────────────
    #  UTILS
    # ─────────────────────────────────────────────

    def _get_symbol_info(self, symbol: str) -> dict:
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

    def _place_sl_tp(self, symbol: str, side: str,
                     order_type: str, trigger_price: float,
                     qty: float) -> bool:
        """
        Plaseaza SL sau TP folosind futures_create_order standard.

        FIX CRITIC (bazat pe documentatia Binance):
        - workingType=MARK_PRICE: trigger pe Mark Price (acelasi ca ROI afisat)
          CONTRACT_PRICE (Last Price) poate diverge 1-2% fata de Mark Price
          la altcoins volatile → la 10x leverage = 10-20% ROI diferenta!
        - closePosition=True: inchide toata pozitia, nu o cantitate fixa
          evita desync la partial fills, prioritate anti-throttle la Binance
        - timeInForce=GTC: GTE_GTC e legacy si poate genera -4129
        """
        label = "SL" if "STOP" in order_type else "TP"
        try:
            order = self.client.futures_create_order(
                symbol        = symbol,
                side          = side,
                type          = order_type,
                stopPrice     = str(trigger_price),
                closePosition = True,        # inchide toata pozitia
                workingType   = "MARK_PRICE",# trigger pe Mark Price = ROI real
                timeInForce   = "GTC",       # nu GTE_GTC (legacy, genereaza -4129)
            )
            oid = order.get("orderId","?")
            logger.info(f"[{symbol}] {label} plasat @ {trigger_price} | orderId={oid} | workingType=MARK_PRICE")
            return True
        except BinanceAPIException as e:
            if e.code == -2021:
                # Pretul deja dincolo de trigger — inchide imediat
                logger.warning(
                    f"[{symbol}] {label} -2021 (pret deja trecut) — inchid MARKET!"
                )
                try:
                    self.client.futures_create_order(
                        symbol=symbol, side=side,
                        type="MARKET", quantity=qty, reduceOnly=True
                    )
                    logger.info(f"[{symbol}] Inchis MARKET dupa -2021")
                    try:
                        from notifier import notify_error
                        notify_error("⚡ SL/TP executat instant",
                                     f"{symbol}: pret deja la/dincolo de {label}")
                    except Exception: pass
                    return True  # pozitia e inchisa, consideram OK
                except Exception as ce:
                    logger.error(f"[{symbol}] Market close error: {ce}")
                    return False
            elif e.code == -1111:
                # Precizie gresita — rotunjim si reincercam
                logger.warning(f"[{symbol}] {label} precizie — reincerca: {e}")
                try:
                    info = self._get_symbol_info(symbol)
                    pp   = info.get("price_prec", 4)
                    tick = info.get("tick_size", 0.0001)
                    tp_r = self._round_price(trigger_price, tick, pp)
                    order = self.client.futures_create_order(
                        symbol=symbol, side=side, type=order_type,
                        stopPrice=str(tp_r),
                        closePosition=True,
                        workingType="MARK_PRICE",
                        timeInForce="GTC",
                    )
                    logger.info(f"[{symbol}] {label} plasat (retry) @ {tp_r}")
                    return True
                except Exception as re:
                    logger.error(f"[{symbol}] {label} retry esuat: {re}")
                    return False
            else:
                logger.error(f"[{symbol}] {label} BinanceError {e.code}: {e.message}")
                return False
        except Exception as e:
            logger.error(f"[{symbol}] {label} eroare: {e}")
            return False

    def check_filled_orders(self):
        c1 = self._check_pending()
        c2 = self._check_active_positions()
        c3 = self._expire_old_orders()
        if c1 or c2 or c3:
            self._save()



    def _check_pending(self) -> bool:
        if not self.pending_orders:
            return False
        to_rm=[]; changed=False
        for sym, data in list(self.pending_orders.items()):
            try:
                order  = self.client.futures_get_order(symbol=sym, orderId=data["order_id"])
                status = order.get("status", "")

                if status == "FILLED":
                    filled = float(order.get("avgPrice", data["entry"]))
                    logger.info(f"[{sym}] UMPLUT la {filled} — SL+TP...")
                    t.sleep(0.5)
                    cs    = data["close_side"]
                    sl_ok = self._place_sl_tp(sym, cs, "STOP_MARKET",        data["sl"], data["qty"])
                    t.sleep(0.3)
                    tp_ok = self._place_sl_tp(sym, cs, "TAKE_PROFIT_MARKET", data["tp"], data["qty"])
                    if sl_ok and tp_ok:
                        logger.info(f"[{sym}] SL + TP plasate!")
                    elif not sl_ok:
                        logger.warning(f"[{sym}] SL esuat — PERICOL!")

                    self.active_positions[sym] = {
                        "direction": data.get("direction", "?"),
                        "entry":     filled,
                        "sl":        data["sl"],
                        "tp":        data["tp"],
                        "qty":       data["qty"],
                        "open_time": data.get("open_time", ""),
                        "open_ts":   data.get("open_ts", int(t.time() * 1000)),
                        "rsi":       data.get("rsi", 0.0),
                        "slope":     data.get("slope", 0.0),
                    }
                    to_rm.append(sym); changed = True

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.info(f"[{sym}] Ordin {status}")
                    self.closed_trades.append({
                        "symbol": sym, "direction": data.get("direction","?"),
                        "entry":  data.get("entry",0), "sl": data["sl"], "tp": data["tp"],
                        "result": "EXPIRED", "pnl": 0.0,
                        "open_time": data.get("open_time",""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    to_rm.append(sym); changed = True

            except Exception as e:
                logger.error(f"[{sym}] check_pending: {e}")

        for s in to_rm:
            self.pending_orders.pop(s, None)
        return changed

    def _check_active_positions(self) -> bool:
        if not self.active_positions:
            return False
        try:
            all_positions = self.client.futures_position_information()
            # DICT in loc de SET — avem nevoie de datele pozitiei (unRealizedProfit etc)
            real_open = {
                p["symbol"]: p for p in all_positions
                if abs(float(p["positionAmt"])) > 0
            }
        except Exception as e:
            logger.error(f"check_active: {e}"); return False

        to_cl=[]; changed=False
        for sym, pos in list(self.active_positions.items()):
            if sym in real_open:
                # Emergency Close gestionat de Guardian extern
                continue
            try:
                open_ts = int(pos["open_ts"])
                end_ts  = int(t.time() * 1000)
                income  = self.client.futures_income_history(
                    symbol=sym, incomeType="REALIZED_PNL",
                    startTime=open_ts, endTime=end_ts, limit=20
                )
                pnl = sum(float(x["income"]) for x in income) if income else 0.0
                if pnl == 0.0 and not income:
                    logger.warning(f"[{sym}] PNL=0, retry urmator ciclu")
                    continue

                result     = "TP" if pnl > 0 else "SL"
                close_time = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())
                sign       = "+" if pnl >= 0 else ""
                logger.info(f"[{sym}] {'✅ TP' if result=='TP' else '❌ SL'} | PNL: {sign}{pnl:.4f} USDT")

                trade_rec = {
                    "symbol":     sym,
                    "direction":  pos["direction"],
                    "entry":      pos["entry"],
                    "sl":         pos["sl"],
                    "tp":         pos["tp"],
                    "result":     result,
                    "pnl":        round(pnl, 4),
                    "open_time":  pos["open_time"],
                    "close_time": close_time,
                    "rsi":        pos.get("rsi", 0),
                    "slope":      pos.get("slope", 0),
                }
                self.closed_trades.append(trade_rec)

                # Notificare Telegram
                try:
                    from notifier import notify_trade_closed
                    dur_h = (end_ts - open_ts) / 3600000
                    notify_trade_closed(
                        symbol=sym, direction=pos["direction"],
                        entry=pos["entry"], sl=pos["sl"], tp=pos["tp"],
                        result=result, pnl_usdt=pnl,
                        open_time=pos["open_time"], close_time=close_time,
                        rsi=pos.get("rsi", 0.0), duration_h=dur_h,
                    )
                except Exception as ne:
                    logger.warning(f"[{sym}] notify error: {ne}")

                # Jurnal CSV
                try:
                    import journal
                    journal.log_trade(
                        symbol=sym, direction=pos["direction"],
                        entry=pos["entry"], sl=pos["sl"], tp=pos["tp"],
                        result=result, pnl_usdt=pnl,
                        usdt_per_trade=USDT_PER_TRADE,
                        open_time=pos["open_time"], close_time=close_time,
                        rsi=pos.get("rsi", 0), ema_slope=pos.get("slope", 0),
                    )
                except Exception:
                    pass

                to_cl.append(sym); changed = True

            except Exception as e:
                logger.error(f"[{sym}] get PNL: {e}")

        for s in to_cl:
            self.active_positions.pop(s, None)
        return changed

    def _expire_old_orders(self) -> bool:
        """Expiry corect in milisecunde."""
        expiry_ms = config.ORDER_EXPIRY_HOURS * 3600 * 1000
        now_ms    = int(t.time() * 1000)
        to_exp=[]; changed=False
        for sym, oi in list(self.pending_orders.items()):
            age = now_ms - oi.get("open_ts", now_ms)
            if age >= expiry_ms:
                logger.info(f"[{sym}] Expirat dupa {age/3600000:.1f}h — anulez...")
                try:
                    self.client.futures_cancel_order(symbol=sym, orderId=oi["order_id"])
                    self.closed_trades.append({
                        "symbol": sym, "direction": oi.get("direction","?"),
                        "entry":  oi.get("entry",0), "sl": oi["sl"], "tp": oi["tp"],
                        "result": "EXPIRED", "pnl": 0.0,
                        "open_time": oi.get("open_time",""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    changed = True
                except Exception as e:
                    logger.error(f"[{sym}] cancel: {e}")
                to_exp.append(sym)
        for s in to_exp:
            self.pending_orders.pop(s, None)
        return changed

    # ─────────────────────────────────────────────
    #  STATISTICI BOT
    # ─────────────────────────────────────────────

    def get_bot_stats(self) -> dict:
        closed  = [x for x in self.closed_trades if x["result"] in ("TP","SL")]
        expired = [x for x in self.closed_trades if x["result"] == "EXPIRED"]
        if not closed:
            return {"total":0,"wins":0,"losses":0,"expired":len(expired),
                    "pnl_total":0.0,"pnl_today":0.0,"win_rate":0.0,
                    "best":0.0,"worst":0.0,
                    "active":len(self.active_positions),
                    "pending":len(self.pending_orders)}
        wins   = [x for x in closed if x["result"]=="TP"]
        losses = [x for x in closed if x["result"]=="SL"]
        pnls   = [x["pnl"] for x in closed]
        today  = t.strftime("%Y-%m-%d", t.gmtime())
        return {
            "total":     len(closed),
            "wins":      len(wins),
            "losses":    len(losses),
            "expired":   len(expired),
            "pnl_total": round(sum(pnls), 4),
            "pnl_today": round(sum(x["pnl"] for x in closed if x.get("close_time","")[:10]==today), 4),
            "win_rate":  round(len(wins)/len(closed)*100, 1),
            "best":      round(max(pnls), 4),
            "worst":     round(min(pnls), 4),
            "active":    len(self.active_positions),
            "pending":   len(self.pending_orders),
        }

    # ─────────────────────────────────────────────
    #  PLASARE TRADE
    # ─────────────────────────────────────────────

    def set_leverage(self, symbol: str):
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
            sl_r    = self._round_price(setup.sl,    tick, pp)
            tp_r    = self._round_price(setup.tp,    tick, pp)
            if entry_r<=0 or sl_r<=0 or tp_r<=0: return False
            qty = self._calc_qty(entry_r, info)
            if qty <= 0: return False

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
                f"[{symbol}] LIMIT {side} | orderId={order_id} | "
                f"qty={qty} | entry={entry_r} | sl={sl_r} | tp={tp_r}"
            )
            self.pending_orders[symbol] = {
                "order_id":   order_id,
                "sl":         sl_r, "tp": tp_r, "qty": qty,
                "close_side": close_side, "entry": entry_r,
                "direction":  side, "open_time": open_time,
                "open_ts":    open_ts,
                "rsi":        getattr(setup, "rsi", 0.0),
                "slope":      getattr(setup, "slope_fast", 0.0),
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

    # ─────────────────────────────────────────────
    #  UTILITARE
    # ─────────────────────────────────────────────

    def count_active_trades(self):
        return len(self.pending_orders) + len(self.active_positions)

    def has_symbol(self, symbol):
        return symbol in self.pending_orders or symbol in self.active_positions
