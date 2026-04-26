"""
FVG BOT 1H — v4
Fix-uri aplicate:
  1. DLL persistent in bot_state_1h.json (supravietuieste restart)
  2. DLL include pierderi flotante din pozitii active
  3. last_candle_ts setat indiferent de rezultatul plasarii
  4. Double-loop: CHECK 10s + SCAN 90s
"""
import sys, io, time, logging
from datetime import datetime, timezone
from collections import defaultdict

from binance.client import Client
from binance.exceptions import BinanceAPIException

import config
from detector import detect_fvg, prepare_df
from order_manager import OrderManager
from notifier import notify_setup, notify_trade, notify_error, send_statistics_report

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("FVGBot1H")


class FVGBot1H:
    def __init__(self):
        self.client           = Client(config.API_KEY, config.API_SECRET)
        self.om               = OrderManager(self.client)
        self.last_candle_ts   = {}
        self.last_report_time = time.time()
        self.stats = {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG BOT 1H — v4")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  EMA: {config.EMA_FAST}/{config.EMA_SLOW} | Slope: {config.EMA_MIN_SLOPE*100:.1f}%/{config.EMA_SLOPE_BARS}bars")
        logger.info(f"  Max pozitii: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  DLL: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% din capital/zi")
        logger.info("═══════════════════════════════════════════════════════")

    # ─────────────────────────────────────────────
    #  DAILY LOSS LIMIT
    # ─────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_capital(self) -> float:
        """Capital cu cache 10 minute."""
        now_ts = time.time()
        if not hasattr(self,"_cap_cache") or now_ts - getattr(self,"_cap_ts",0) > 600:
            try:
                bal = self.client.futures_account_balance()
                cap = 0.0
                for b in bal:
                    if b.get("asset") == "USDT":
                        v = float(b.get("walletBalance") or b.get("balance") or 0)
                        if v > 0: cap = v; break
                if cap < 10:
                    cap = config.USDT_PER_TRADE * config.MAX_OPEN_TRADES
                self._cap_cache = cap
                self._cap_ts    = now_ts
            except Exception as e:
                logger.warning(f"Balance error: {e}")
                cap = getattr(self, "_cap_cache", config.USDT_PER_TRADE * config.MAX_OPEN_TRADES)
        else:
            cap = self._cap_cache
        return cap

    def _dll_active(self, capital: float) -> bool:
        """
        DLL = pierderi inchise + pierderi flotante active.
        FIX: include pozitii deschise cu pierdere nerealizata.
        """
        today = self._today()

        # Pierderi din trades inchise (din order_manager)
        closed_loss = self.om.daily_pnl.get(today, 0.0)

        # FIX: Pierderi flotante din pozitii active
        floating_loss = 0.0
        try:
            if self.om.active_positions:
                positions = self.client.futures_position_information()
                for p in positions:
                    sym = p["symbol"]
                    if sym in self.om.active_positions:
                        unrealized = float(p.get("unRealizedProfit", 0))
                        if unrealized < 0:
                            floating_loss += unrealized
        except Exception:
            pass  # daca API esueaza, folosim doar closed_loss

        total_loss = closed_loss + floating_loss
        limit      = -(capital * config.DAILY_LOSS_LIMIT_PCT)

        if total_loss <= limit:
            logger.info(
                f"⛔ DLL activ: inchise={closed_loss:.2f} + flotante={floating_loss:.2f} "
                f"= {total_loss:.2f} USDT (limita: {limit:.2f} USDT)"
            )
            return True
        return False

    # ─────────────────────────────────────────────
    #  SIMBOLURI + KLINES
    # ─────────────────────────────────────────────

    def get_symbols(self) -> list:
        now_ts = time.time()
        cache  = getattr(self, "_symbols_cache", [])
        if cache and (now_ts - getattr(self, "_symbols_ts", 0) < 900):
            return cache
        try:
            info = self.client.futures_exchange_info()
            syms = [s["symbol"] for s in info["symbols"]
                    if s["symbol"].endswith("USDT")
                    and s["status"] == "TRADING"
                    and s["symbol"] not in config.BLACKLIST]
            self._symbols_cache = syms
            self._symbols_ts    = now_ts
            logger.info(f"Simboluri actualizate: {len(syms)}")
            return syms
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("Rate limit get_symbols — astept 60s si retry...")
                time.sleep(60)
                try:
                    info = self.client.futures_exchange_info()
                    syms = [s["symbol"] for s in info["symbols"]
                            if s["symbol"].endswith("USDT")
                            and s["status"]=="TRADING"
                            and s["symbol"] not in config.BLACKLIST]
                    self._symbols_cache = syms
                    self._symbols_ts = time.time()
                    return syms
                except Exception:
                    return cache
            else:
                logger.error(f"get_symbols: {e}")
            return cache
        except Exception as e:
            logger.error(f"get_symbols: {e}")
            return cache

    def get_klines(self, symbol: str) -> list:
        try:
            klines = self.client.futures_klines(
                symbol=symbol, interval=config.TIMEFRAME, limit=200
            )
            return klines[:-1]
        except BinanceAPIException as e:
            if e.code == -1003:
                raise
            if e.code != -1121:
                logger.warning(f"[{symbol}] klines: {e}")
            return []
        except Exception as e:
            logger.warning(f"[{symbol}] klines: {e}")
            return []

    # ─────────────────────────────────────────────
    #  SCAN SIMBOL
    # ─────────────────────────────────────────────

    def scan_symbol(self, symbol: str, capital: float):
        klines = self.get_klines(symbol)
        if not klines:
            return

        df      = prepare_df(klines)
        last_ts = df.index[-1]

        if self.last_candle_ts.get(symbol) == last_ts:
            return

        setup = detect_fvg(symbol, df)

        # FIX: seteaza last_candle_ts INDIFERENT de rezultat
        self.last_candle_ts[symbol] = last_ts

        if setup is None:
            return

        logger.info(f"[{symbol}] FVG {setup.direction} | RSI={setup.rsi} | "
                    f"Entry={setup.entry:.6f} | SL={setup.sl:.6f} | "
                    f"TP={setup.tp:.6f} | Slope={setup.slope_fast:+.3f}%")

        if self._dll_active(capital):
            logger.info(f"[{symbol}] SKIP — DLL activ")
            return

        if self.om.has_symbol(symbol):
            return

        if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
            logger.info(f"[{symbol}] SKIP — limita {config.MAX_OPEN_TRADES} atinsa")
            return

        notify_setup(setup)
        success = self.om.place_fvg_trade(setup)
        notify_trade(setup, success)

    # ─────────────────────────────────────────────
    #  RAPORT
    # ─────────────────────────────────────────────

    def check_and_send_report(self):
        if time.time() - self.last_report_time >= config.TELEGRAM_REPORT_HOURS * 3600:
            capital  = self._get_capital()
            today    = self._today()
            bstats   = self.om.get_bot_stats()
            dll_today = self.om.daily_pnl.get(today, 0.0)
            send_statistics_report({
                "total_trades":   bstats["total"],
                "wins":           bstats["wins"],
                "losses":         bstats["losses"],
                "expired_orders": bstats["expired"],
                "pending":        bstats["pending"],
                "open_positions": bstats["active"],
                "pnl_total":      bstats["pnl_total"],
                "pnl_today":      bstats["pnl_today"],
                "win_rate":       bstats["win_rate"],
                "best_trade":     bstats["best"],
                "worst_trade":    bstats["worst"],
                "commission_paid":0.0,
                "start_time":     self.stats["start"],
                "dll_today":      dll_today,
                "timeframe":      config.TIMEFRAME,
            })
            self.last_report_time = time.time()
            logger.info("Raport Telegram trimis.")

    # ─────────────────────────────────────────────
    #  RUN — DOUBLE LOOP
    # ─────────────────────────────────────────────

    def run(self):
        """
        Double-loop:
        - CHECK (10s): check_filled_orders — SL/TP plasat imediat
        - SCAN  (90s): scaneaza simboluri — 90s alterneaza cu 4H (60s)
        """
        logger.info("Reconciliere cu Binance...")
        self.om.reconcile_with_binance()
        logger.info("Bot 1H pornit. Ctrl+C pentru oprire.")

        PENDING_INTERVAL = 10   # verifica ordine umplute la 10s
        ACTIVE_INTERVAL  = 30   # verifica pozitii inchise la 30s (mai rar)
        SCAN_INTERVAL    = 90   # alterneaza cu 4H la 60s

        last_pending = 0
        last_active  = 0
        last_scan    = 0

        while True:
            try:
                now = time.time()

                # ── PENDING CHECK (10s) — detecta umpleri rapid ──
                if now - last_pending >= PENDING_INTERVAL:
                    try:
                        # Doar pending + expire (nu position_information)
                        c1 = self.om._check_pending()
                        c3 = self.om._expire_old_orders()
                        if c1 or c3:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code == -1003:
                            logger.warning("Rate limit check pending — skip")
                        else:
                            logger.error(f"Pending check error: {e}")
                    except Exception as e:
                        logger.error(f"Pending check error: {e}")
                    last_pending = time.time()

                # ── ACTIVE CHECK (30s) — detecta pozitii inchise ──
                if now - last_active >= ACTIVE_INTERVAL:
                    try:
                        c2 = self.om._check_active_positions()
                        if c2:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code == -1003:
                            logger.warning("Rate limit check active — skip")
                        else:
                            logger.error(f"Active check error: {e}")
                    except Exception as e:
                        logger.error(f"Active check error: {e}")
                    last_active = time.time()

                # ── SCAN LOOP (90s) ───────────────────────────
                if now - last_scan >= SCAN_INTERVAL:
                    capital = self._get_capital()
                    active  = self.om.count_active_trades()
                    pending = len(self.om.pending_orders)

                    if active >= config.MAX_OPEN_TRADES:
                        logger.info(f"PAUZA — {active}/{config.MAX_OPEN_TRADES} pozitii")

                    elif self._dll_active(capital):
                        logger.info(f"PAUZA ZILNICA — DLL activ | {active} pozitii deschise")

                    else:
                        symbols = self.get_symbols()[:150]
                        logger.info(f"Scanez {len(symbols)} perechi | "
                                    f"Pozitii: {active}/{config.MAX_OPEN_TRADES} | "
                                    f"Pending: {pending} | "
                                    f"DLL azi: {self.om.daily_pnl.get(self._today(),0):+.2f} USDT")

                        for sym in symbols:
                            if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
                                logger.info("Limita atinsa — opresc scanarea")
                                break
                            if self._dll_active(capital):
                                break
                            try:
                                self.scan_symbol(sym, capital)
                            except BinanceAPIException as e:
                                if e.code == -1003:
                                    logger.warning("Rate limit scan — astept 60s...")
                                    time.sleep(60)
                                    break
                                else:
                                    logger.error(f"[{sym}] BinanceError: {e}")
                            except Exception as e:
                                logger.error(f"[{sym}] Eroare: {e}")
                            time.sleep(0.20)

                        logger.info(f"Ciclu complet | {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
                                    f"Pozitii: {self.om.count_active_trades()}/{config.MAX_OPEN_TRADES} | "
                                    f"Pending: {len(self.om.pending_orders)}")

                    self.check_and_send_report()
                    last_scan = time.time()

                time.sleep(2)

            except KeyboardInterrupt:
                logger.info("Bot oprit.")
                break
            except Exception as e:
                logger.error(f"Eroare loop: {e}")
                notify_error("Loop 1H", str(e))
                time.sleep(10)


if __name__ == "__main__":
    FVGBot1H().run()
