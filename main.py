"""
FVG BOT 1H — Main
Rulat LOCAL pe PC
Features: DLL 8%, state persistent, Telegram notificari
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
        self._symbols_cache   = []

        # Daily Loss Limit
        self.daily_pnl        = defaultdict(float)  # {date: pnl}

        self.stats = {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG BOT 1H pornit — LOCAL PC")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  EMA: {config.EMA_FAST}/{config.EMA_SLOW} | Slope: {config.EMA_MIN_SLOPE*100:.1f}%/{config.EMA_SLOPE_BARS}bars")
        logger.info(f"  Max pozitii: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  Daily Loss Limit: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% din capital/zi")
        logger.info("═══════════════════════════════════════════════════════")

        # Reconciliere cu Binance la startup
        logger.info("Reconciliere cu Binance...")
        self.om.reconcile_with_binance()
        logger.info("Bot pornit. Ctrl+C pentru oprire.")

    # ─────────────────────────────────────────────
    #  DAILY LOSS LIMIT
    # ─────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _dll_active(self, capital: float) -> bool:
        """Returneaza True daca limita zilnica de pierdere a fost atinsa."""
        today_loss = self.daily_pnl.get(self._today(), 0.0)
        limit      = capital * config.DAILY_LOSS_LIMIT_PCT
        if today_loss <= -limit:
            logger.info(
                f"⛔ DAILY LOSS LIMIT activ: {today_loss:.2f} USDT "
                f"(limita: -{limit:.2f} USDT) — nu deschid ordine azi"
            )
            return True
        return False

    def _update_dll(self, pnl: float):
        """Actualizeaza PNL-ul zilei curente."""
        self.daily_pnl[self._today()] += pnl

    # ─────────────────────────────────────────────
    #  SIMBOLURI
    # ─────────────────────────────────────────────

    def get_symbols(self) -> list:
        # Cache 15 minute — reduce API calls, evita IP ban
        now_ts = time.time()
        cache  = getattr(self,"_symbols_cache",[])
        if cache and (now_ts - getattr(self,"_symbols_ts",0) < 900):
            return cache
        try:
            info = self.client.futures_exchange_info()
            syms = [
                s["symbol"] for s in info["symbols"]
                if s["symbol"].endswith("USDT")
                and s["status"] == "TRADING"
                and s["symbol"] not in config.BLACKLIST
            ]
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
                    self._symbols_cache = syms; self._symbols_ts = time.time()
                    return syms
                except Exception: return cache
            else:
                logger.error(f"get_symbols: {e}")
            return cache

    def get_klines(self, symbol: str) -> list:
        try:
            # Pe 1H avem nevoie de mai multe bare pentru EMA100 + slope
            klines = self.client.futures_klines(
                symbol=symbol, interval=config.TIMEFRAME, limit=200
            )
            return klines[:-1]  # exclude lumanarea curenta (incompleta)
        except BinanceAPIException as e:
            if e.code not in (-1121, -1003):
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

        # Nu procesa aceeasi lumanare de doua ori
        if self.last_candle_ts.get(symbol) == last_ts:
            return

        setup = detect_fvg(symbol, df)
        if setup is None:
            return

        logger.info(
            f"[{symbol}] FVG {setup.direction} | "
            f"RSI={setup.rsi} | Entry={setup.entry:.6f} | "
            f"SL={setup.sl:.6f} | TP={setup.tp:.6f} | "
            f"Slope={setup.slope_fast:+.3f}%"
        )

        # Verifica DLL
        if self._dll_active(capital):
            self.last_candle_ts[symbol] = last_ts
            return

        # Verifica daca simbolul e deja deschis
        if self.om.has_symbol(symbol):
            logger.info(f"[{symbol}] SKIP — ordin/pozitie deja exista")
            self.last_candle_ts[symbol] = last_ts
            return

        # Verifica limita MAX_OPEN_TRADES
        if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
            logger.info(f"[{symbol}] SKIP — limita {config.MAX_OPEN_TRADES} atinsa")
            return

        notify_setup(setup)
        success = self.om.place_fvg_trade(setup)
        notify_trade(setup, success)

        if success:
            self.last_candle_ts[symbol] = last_ts

    # ─────────────────────────────────────────────
    #  RAPORT TELEGRAM
    # ─────────────────────────────────────────────

    def check_and_send_report(self):
        interval = config.TELEGRAM_REPORT_HOURS * 3600
        if time.time() - self.last_report_time >= interval:
            bstats = self.om.get_bot_stats()

            # Adauga DLL info in raport
            today_pnl = self.daily_pnl.get(self._today(), 0.0)

            send_statistics_report({
                "total_trades":    bstats["total"],
                "wins":            bstats["wins"],
                "losses":          bstats["losses"],
                "expired_orders":  bstats["expired"],
                "pending":         bstats["pending"],
                "open_positions":  bstats["active"],
                "pnl_total":       bstats["pnl_total"],
                "pnl_today":       bstats["pnl_today"],
                "win_rate":        bstats["win_rate"],
                "best_trade":      bstats["best"],
                "worst_trade":     bstats["worst"],
                "commission_paid": 0.0,
                "start_time":      self.stats["start"],
                "dll_today":       today_pnl,
                "timeframe":       config.TIMEFRAME,
            })
            self.last_report_time = time.time()
            logger.info("Raport Telegram trimis.")

    # ─────────────────────────────────────────────
    #  CICLU PRINCIPAL
    # ─────────────────────────────────────────────

    def run_scan(self):
        """Scanare simboluri si deschidere ordine noi."""
        active  = self.om.count_active_trades()
        pending = len(self.om.pending_orders)

        if active >= config.MAX_OPEN_TRADES:
            logger.info(f"PAUZA — {active}/{config.MAX_OPEN_TRADES} pozitii | {pending} pending")
            return

        # 2. Capital estimat — cache 10 minute (reduce API calls)
        now_ts = time.time()
        if not hasattr(self, "_capital_cache") or now_ts - getattr(self,"_capital_ts",0) > 600:
            try:
                balance = self.client.futures_account_balance()
                capital = 0.0
                for b in balance:
                    if b.get("asset") == "USDT":
                        val = float(b.get("walletBalance") or b.get("balance") or 0)
                        if val > 0:
                            capital = val; break
                if capital < 10:
                    capital = config.USDT_PER_TRADE * config.MAX_OPEN_TRADES
                self._capital_cache = capital
                self._capital_ts    = now_ts
            except Exception as e:
                logger.warning(f"Balance fetch error: {e}")
                capital = getattr(self,"_capital_cache",
                                  config.USDT_PER_TRADE * config.MAX_OPEN_TRADES)
        else:
            capital = self._capital_cache

        # 3. DLL check global
        if self._dll_active(capital):
            logger.info(f"PAUZA ZILNICA — DLL activ | {active} pozitii deschise")
            return

        # 4. Scaneaza simboluri
        symbols = self.get_symbols()
        logger.info(
            f"Scanez {len(symbols)} perechi | "
            f"Pozitii: {active}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {pending} | "
            f"DLL azi: {self.daily_pnl.get(self._today(), 0):+.2f} USDT"
        )

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
                    logger.warning("Rate limit in scan — astept 3 minute...")
                    time.sleep(180)
                    break
                else:
                    logger.error(f"[{sym}] BinanceError: {e}")
            except Exception as e:
                logger.error(f"[{sym}] Eroare: {e}")
            time.sleep(0.5)  # 0.5s intre simboluri

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info(
            f"Ciclu complet | {now_str} UTC | "
            f"Pozitii: {self.om.count_active_trades()}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {len(self.om.pending_orders)}"
        )
        self.check_and_send_report()

    # ─────────────────────────────────────────────
    #  RUN
    # ─────────────────────────────────────────────

    def run(self):
        """
        Doua loop-uri independente:
        1. CHECK LOOP (la 10s) — verifica ordine umplute si plaseaza SL/TP rapid
        2. SCAN LOOP  (la 60s) — scaneaza simboluri si deschide ordine noi

        Separarea e critica pe 1H: prețul poate trece de SL in 60 secunde
        daca verificam doar o data pe minut.
        """
        logger.info("Bot 1H pornit. Ctrl+C pentru oprire.")

        CHECK_INTERVAL = 10   # verifica umpleri la fiecare 10 secunde
        SCAN_INTERVAL  = config.SCAN_INTERVAL_SEC  # scaneaza la fiecare 60 secunde

        last_check = 0
        last_scan  = 0

        while True:
            try:
                now = time.time()

                # ── CHECK LOOP (10s) ──────────────────────────
                if now - last_check >= CHECK_INTERVAL:
                    try:
                        before = len(self.om.closed_trades)
                        self.om.check_filled_orders()
                        after  = len(self.om.closed_trades)

                        # Actualizeaza DLL cu trade-urile noi inchise
                        if after > before:
                            for tr in self.om.closed_trades[before:after]:
                                if tr.get("result") in ("TP","SL"):
                                    self._update_dll(tr.get("pnl", 0.0))

                    except BinanceAPIException as e:
                        if e.code == -1003:
                            logger.warning("Rate limit check — astept 30s...")
                            time.sleep(30)
                        else:
                            logger.error(f"Check error: {e}")
                    except Exception as e:
                        logger.error(f"Check error: {e}")

                    last_check = time.time()

                # ── SCAN LOOP (60s) ───────────────────────────
                if now - last_scan >= SCAN_INTERVAL:
                    try:
                        self.run_scan()
                    except BinanceAPIException as e:
                        if e.code == -1003:
                            logger.warning("Rate limit scan — astept 3 minute...")
                            time.sleep(180)
                        else:
                            logger.error(f"Scan error: {e}")
                    except Exception as e:
                        logger.error(f"Scan error: {e}")
                        notify_error("Scan 1H", str(e))

                    self.check_and_send_report()
                    last_scan = time.time()

                time.sleep(2)  # sleep mic — loop rapid

            except KeyboardInterrupt:
                logger.info("Bot oprit de utilizator.")
                break
            except Exception as e:
                logger.error(f"Eroare loop: {e}")
                time.sleep(5)


if __name__ == "__main__":
    FVGBot1H().run()
