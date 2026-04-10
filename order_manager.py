"""
Order Manager 1H — cu state persistent local
Supravietuieste restart-urilor de PC prin bot_state_1h.json
"""
import logging, hmac, hashlib, json, os
import time as t
import requests as req
from urllib.parse import urlencode
from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE

# Guardian importat inline
"""
╔══════════════════════════════════════════════════════════╗
║  GUARDIAN — Protectia pozitiilor deschise               ║
║                                                          ║
║  Verifica la fiecare ciclu ca FIECARE pozitie           ║
║  deschisa are SL si TP plasate pe Binance.              ║
║  Daca lipsesc → le replaseaza automat.                  ║
║                                                          ║
║  Scenarii acoperite:                                     ║
║  1. SL/TP nu s-au plasat (eroare la umplere)            ║
║  2. SL/TP anulate de Binance (lichidare partial, etc)   ║
║  3. Bot restartat cu pozitii deschise dar fara ordine   ║
╚══════════════════════════════════════════════════════════╝
"""
import logging
import hmac
import hashlib
import time as t
import requests as req
from urllib.parse import urlencode

logger = logging.getLogger("Guardian")


class PositionGuardian:
    """
    Guardian care monitorizeaza pozitiile deschise ale botului
    si se asigura ca fiecare are SL si TP plasate.
    """

    def __init__(self, client, config, algo_post_fn):
        """
        client       — Binance client
        config       — modulul config al botului
        algo_post_fn — functia _algo_signed_post din OrderManager
        """
        self.client      = client
        self.config      = config
        self._algo_post  = algo_post_fn
        self._warned     = set()  # simboluri pentru care am avertizat deja

    # ─────────────────────────────────────────────────────
    #  MAIN CHECK
    # ─────────────────────────────────────────────────────

    def check_positions(self, active_positions: dict) -> int:
        """
        Verifica toate pozitiile active ale botului.
        Returneaza numarul de SL/TP replasate.
        """
        if not active_positions:
            return 0

        try:
            # Obtine pozitiile reale de pe Binance
            real_positions = self.client.futures_position_information()
            real_open = {
                p["symbol"]: p for p in real_positions
                if abs(float(p["positionAmt"])) > 0
            }
        except Exception as e:
            logger.error(f"[GUARDIAN] Nu pot citi pozitiile: {e}")
            return 0

        # Obtine toate ordinele algo active (SL/TP)
        try:
            algo_orders = self._get_algo_orders()
        except Exception as e:
            logger.error(f"[GUARDIAN] Nu pot citi ordinele algo: {e}")
            algo_orders = {}

        replased = 0

        for symbol, pos in list(active_positions.items()):
            if symbol not in real_open:
                continue  # pozitia nu mai e deschisa

            real_pos   = real_open[symbol]
            pos_amt    = float(real_pos["positionAmt"])
            close_side = "SELL" if pos_amt > 0 else "BUY"
            qty        = abs(pos_amt)

            # Verifica ce ordine algo exista pentru acest simbol
            sym_algos  = algo_orders.get(symbol, [])
            has_sl     = any("STOP" in o.get("orderType","") for o in sym_algos)
            has_tp     = any("PROFIT" in o.get("orderType","") for o in sym_algos)

            # Daca ambele exista — totul e OK
            if has_sl and has_tp:
                if symbol in self._warned:
                    logger.info(f"[GUARDIAN] ✅ {symbol} — SL+TP restaurate cu succes")
                    self._warned.discard(symbol)
                continue

            # Verifica daca avem preturile SL/TP in state
            sl_price = float(pos.get("sl", 0))
            tp_price = float(pos.get("tp", 0))

            # Fallback: calculeaza SL/TP din entry daca lipsesc din state
            if sl_price <= 0 or tp_price <= 0:
                entry = float(pos.get("entry", float(real_pos.get("entryPrice", 0))))
                if entry > 0:
                    direction = pos.get("direction", "BUY")
                    risk_pct  = 0.02  # 2% default
                    if direction == "BUY" or pos_amt > 0:
                        sl_price = entry * (1 - risk_pct) if sl_price <= 0 else sl_price
                        tp_price = entry * (1 + risk_pct) if tp_price <= 0 else tp_price
                    else:
                        sl_price = entry * (1 + risk_pct) if sl_price <= 0 else sl_price
                        tp_price = entry * (1 - risk_pct) if tp_price <= 0 else tp_price

            if sl_price <= 0 or tp_price <= 0:
                logger.warning(f"[GUARDIAN] {symbol} — nu pot calcula SL/TP (entry lipsa)")
                continue

            # Log avertisment la prima detectie
            if symbol not in self._warned:
                missing = []
                if not has_sl: missing.append("SL")
                if not has_tp: missing.append("TP")
                logger.warning(
                    f"[GUARDIAN] ⚠️  {symbol} — lipsesc: {', '.join(missing)} | "
                    f"Pozitie: {qty} @ {pos.get('entry','?')} | "
                    f"SL={sl_price:.6f} | TP={tp_price:.6f}"
                )
                self._warned.add(symbol)

            # Replaseaza ce lipseste
            if not has_sl:
                ok = self._place_conditional(
                    symbol, close_side, "STOP_MARKET", sl_price, qty
                )
                if ok:
                    logger.info(f"[GUARDIAN] ✅ {symbol} — SL replasat @ {sl_price:.6f}")
                    replased += 1
                else:
                    logger.error(f"[GUARDIAN] ❌ {symbol} — SL ESUAT!")

            if not has_tp:
                t.sleep(0.3)
                ok = self._place_conditional(
                    symbol, close_side, "TAKE_PROFIT_MARKET", tp_price, qty
                )
                if ok:
                    logger.info(f"[GUARDIAN] ✅ {symbol} — TP replasat @ {tp_price:.6f}")
                    replased += 1
                else:
                    logger.error(f"[GUARDIAN] ❌ {symbol} — TP ESUAT!")

        return replased

    # ─────────────────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────────────────

    def _get_algo_orders(self) -> dict:
        """
        Obtine toate ordinele CONDITIONAL (SL/TP) active.
        Returneaza dict: {symbol: [orders]}
        """
        FAPI = "https://fapi.binance.com"
        params = {"timestamp": int(t.time() * 1000)}
        qs  = urlencode(params)
        sig = hmac.new(
            self.config.API_SECRET.encode(),
            qs.encode(),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = sig

        resp = req.get(
            FAPI + "/fapi/v1/openAlgoOrders",
            params  = params,
            headers = {"X-MBX-APIKEY": self.config.API_KEY},
            timeout = 10
        )
        data = resp.json()

        result = {}
        if isinstance(data, list):
            for o in data:
                sym = o.get("symbol","")
                if sym not in result:
                    result[sym] = []
                result[sym].append(o)
        return result

    def _place_conditional(self, symbol: str, side: str,
                            order_type: str, trigger_price: float,
                            qty: float) -> bool:
        label = "SL" if "STOP" in order_type else "TP"
        data  = self._algo_post({
            "algoType":     "CONDITIONAL",
            "symbol":       symbol,
            "side":         side,
            "type":         order_type,
            "triggerPrice": str(round(trigger_price, 8)),
            "quantity":     str(qty),
            "reduceOnly":   "true",
            "workingType":  "CONTRACT_PRICE",
        })
        if "algoId" in data or "orderId" in data:
            return True
        logger.error(f"[GUARDIAN] {label} error: {data}")
        return False


logger = logging.getLogger("FVGBot1H")
FAPI   = "https://fapi.binance.com"


def _save_state(pending, active, closed):
    try:
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "pending_orders":   pending,
                "active_positions": active,
                "closed_trades":    closed,
            }, f, indent=2)
    except Exception as e:
        logger.error(f"_save_state error: {e}")


def _load_state():
    if not os.path.exists(config.STATE_FILE):
        return {}, {}, []
    try:
        with open(config.STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        p = data.get("pending_orders", {})
        a = data.get("active_positions", {})
        c = data.get("closed_trades", [])
        if p or a:
            logger.info(
                f"[STATE] Restaurat: {len(p)} pending, "
                f"{len(a)} active, {len(c)} closed"
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
        # Guardian — monitorizeaza SL/TP pentru toate pozitiile
        self.guardian = PositionGuardian(
            client       = client,
            config       = config,
            algo_post_fn = self._algo_signed_post,
        )

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

    def _algo_signed_post(self, params: dict) -> dict:
        """POST la /fapi/v1/algoOrder — parametrii in body."""
        params["timestamp"] = int(t.time() * 1000)
        qs  = urlencode(params)
        sig = hmac.new(
            config.API_SECRET.encode("utf-8"),
            qs.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        try:
            resp = req.post(
                FAPI + "/fapi/v1/algoOrder",
                data    = params,
                headers = {"X-MBX-APIKEY": config.API_KEY},
                timeout = 15
            )
            return resp.json() if resp.text.strip() else {"error": f"Empty {resp.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    # ─────────────────────────────────────────────
    #  SL / TP
    # ─────────────────────────────────────────────

    def _place_conditional(self, symbol, side, order_type, trigger_price, qty) -> bool:
        label = "SL" if "STOP" in order_type else "TP"
        data  = self._algo_signed_post({
            "algoType":     "CONDITIONAL",
            "symbol":       symbol,
            "side":         side,
            "type":         order_type,
            "triggerPrice": str(trigger_price),
            "quantity":     str(qty),
            "reduceOnly":   "true",
            "workingType":  "CONTRACT_PRICE",
        })
        if "algoId" in data or "orderId" in data:
            logger.info(f"[{symbol}] {label} @ {trigger_price} | algoId={data.get('algoId','?')}")
            return True
        logger.error(f"[{symbol}] {label} ESUAT: {data}")
        return False

    # ─────────────────────────────────────────────
    #  CHECK CYCLE
    # ─────────────────────────────────────────────

    def check_filled_orders(self):
        c1 = self._check_pending()
        c2 = self._check_active_positions()
        c3 = self._expire_old_orders()
        if c1 or c2 or c3:
            self._save()

        # Guardian: verifica SL/TP pentru toate pozitiile active
        try:
            self.guardian.check_positions(self.active_positions)
        except Exception as e:
            logger.error(f"[GUARDIAN] Eroare: {e}")

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
                    sl_ok = self._place_conditional(sym, cs, "STOP_MARKET",        data["sl"], data["qty"])
                    t.sleep(0.3)
                    tp_ok = self._place_conditional(sym, cs, "TAKE_PROFIT_MARKET", data["tp"], data["qty"])
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
            real_open = {
                p["symbol"] for p in self.client.futures_position_information()
                if abs(float(p["positionAmt"])) > 0
            }
        except Exception as e:
            logger.error(f"check_active: {e}"); return False

        to_cl=[]; changed=False
        for sym, pos in list(self.active_positions.items()):
            if sym in real_open:
                # FIX: Emergency close daca pierderea e prea mare si SL nu s-a executat
                real_p    = real_open.get(sym, {})
                entry     = float(pos.get("entry", 0))
                mark      = float(real_p.get("markPrice", entry) or entry)
                direction = pos.get("direction","BUY")
                if entry > 0 and mark > 0:
                    if direction == "BUY":
                        loss_pct = (mark - entry) / entry * 100
                    else:
                        loss_pct = (entry - mark) / entry * 100
                    max_loss = -config.MAX_LOSS_PCT_EMERGENCY * 100
                    if loss_pct < max_loss:
                        logger.error(
                            f"[EMERGENCY] {sym} pierde {loss_pct:.1f}% "
                            f"(limita: {max_loss:.0f}%) — INCHID MARKET!"
                        )
                        try:
                            qty = abs(float(real_p.get("positionAmt", pos.get("qty",0))))
                            close_side = "SELL" if direction=="BUY" else "BUY"
                            self.client.futures_create_order(
                                symbol=sym, side=close_side,
                                type="MARKET", quantity=qty, reduceOnly=True
                            )
                            logger.info(f"[EMERGENCY] {sym} inchis market!")
                            try:
                                from notifier import notify_error
                                notify_error(
                                    "EMERGENCY CLOSE",
                                    f"{sym} inchis fortat: {loss_pct:.0f}% pierdere"
                                )
                            except Exception: pass
                        except Exception as e:
                            logger.error(f"[EMERGENCY] {sym} close error: {e}")
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
