"""
FVG WITH-TREND BOT v3 — versiune stabila
"""
import sys
import io
import time
import logging
from datetime import datetime, timezone

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
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("FVGBot")


class FVGBot:
    def __init__(self):
        self.client           = Client(config.API_KEY, config.API_SECRET)
        self.om               = OrderManager(self.client)
        self.last_candle_ts   = {}
        self.last_report_time = time.time()

        # Statistici simple
        self.stats = {
            "total": 0, "wins": 0, "losses": 0,
            "pnl": 0.0, "start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG WITH-TREND BOT v3 pornit")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  EMA: {config.EMA_FAST}/{config.EMA_SLOW} | Slope: {config.EMA_MIN_SLOPE*100:.1f}%/{config.EMA_SLOPE_BARS}bars")
        logger.info(f"  Max pozitii: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info("═══════════════════════════════════════════════════════")

    def get_symbols(self) -> list:
        # Cache 15 minute — reduce API calls, evita IP ban
        now_ts = time.time()
        cache  = getattr(self,"_symbols_cache",[])
        cache_age = now_ts - getattr(self,"_symbols_ts",0)

        # Returneaza cache daca e valid si nu e gol
        if cache and cache_age < 900:
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
                logger.warning(f"Rate limit get_symbols — astept 60s...")
                time.sleep(60)
                # Retry o singura data
                try:
                    info = self.client.futures_exchange_info()
                    syms = [s["symbol"] for s in info["symbols"]
                            if s["symbol"].endswith("USDT")
                            and s["status"]=="TRADING"
                            and s["symbol"] not in config.BLACKLIST]
                    self._symbols_cache = syms
                    self._symbols_ts    = time.time()
                    return syms
                except Exception:
                    return cache  # returneaza ce avem
            else:
                logger.error(f"get_symbols error: {e}")
                return cache
        except Exception as e:
            logger.error(f"get_symbols error: {e}")
            return cache

    def get_klines(self, symbol: str) -> list:
        try:
            klines = self.client.futures_klines(
                symbol=symbol, interval=config.TIMEFRAME, limit=200
            )
            return klines[:-1]
        except BinanceAPIException as e:
            if e.code == -1003:
                raise  # propagheaza rate limit in sus la run_scan
            if e.code != -1121:
                logger.warning(f"[{symbol}] klines: {e}")
            return []
        except Exception as e:
            logger.warning(f"[{symbol}] klines: {e}")
            return []

    def count_active(self) -> int:
        """Numara pozitiile + ordinele active ale botului."""
        return self.om.count_active_trades()

    def scan_symbol(self, symbol: str):
        klines = self.get_klines(symbol)
        if not klines:
            return

        df      = prepare_df(klines)
        last_ts = df.index[-1]

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

        # Verifica daca avem deja ordin/pozitie pe simbol
        open_pos    = self.om.get_open_positions()
        open_orders = self.om.get_open_orders()

        if symbol in open_pos or symbol in open_orders:
            logger.info(f"[{symbol}] SKIP — ordin/pozitie deja exista")
            self.last_candle_ts[symbol] = last_ts
            return

        # Verifica limita MAX_OPEN_TRADES
        active = self.count_active()
        if active >= config.MAX_OPEN_TRADES:
            logger.info(f"[{symbol}] SKIP — limita {config.MAX_OPEN_TRADES} atinsa ({active} active)")
            return

        notify_setup(setup)
        success = self.om.place_fvg_trade(setup)
        notify_trade(setup, success)

        if success:
            self.last_candle_ts[symbol] = last_ts

    def check_and_send_report(self):
        interval = config.TELEGRAM_REPORT_HOURS * 3600
        if time.time() - self.last_report_time >= interval:
            # Statistici DOAR ale acestui bot
            bstats = self.om.get_bot_stats()
            stats_data = {
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
            }
            send_statistics_report(stats_data)
            self.last_report_time = time.time()
            logger.info("Raport Telegram trimis.")

    def run_cycle(self):
        # 1. Verifica ordine + pozitii active ale botului
        try:
            self.om.check_filled_orders()
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("Ban activ — astept 3 minute...")
                time.sleep(180)
                return
            raise

        active = self.count_active()
        pending = len(self.om.pending_orders)

        # 2. Pauza daca la capacitate maxima
        if active >= config.MAX_OPEN_TRADES:
            logger.info(f"PAUZA — {active}/{config.MAX_OPEN_TRADES} pozitii | {pending} pending")
            return

        # 3. Scaneaza
        symbols = self.get_symbols()
        logger.info(
            f"Scanez {len(symbols)} perechi | "
            f"Pozitii: {active}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {pending}"
        )

        for sym in symbols:
            if self.count_active() >= config.MAX_OPEN_TRADES:
                logger.info("Limita atinsa — opresc scanarea")
                break
            try:
                self.scan_symbol(sym)
            except BinanceAPIException as e:
                if e.code == -1003:
                    logger.warning("Rate limit / ban — astept 3 minute...")
                    time.sleep(180)
                    break
                else:
                    logger.error(f"[{sym}] BinanceError: {e}")
            except Exception as e:
                logger.error(f"[{sym}] Eroare: {e}")
            time.sleep(0.5)

        logger.info(
            f"Ciclu complet | {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
            f"Pozitii: {self.count_active()}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {len(self.om.pending_orders)}"
        )

        self.check_and_send_report()

    def run(self):
        # Reconciliaza cu Binance la startup (recupereaza pozitii dupa restart)
        logger.info("Reconciliere cu Binance...")
        self.om.reconcile_with_binance()
        logger.info("Bot pornit. Ctrl+C pentru oprire.")
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Bot oprit.")
                break
            except Exception as e:
                logger.error(f"Eroare ciclu: {e}")
                notify_error("Ciclu principal", str(e))

            logger.info(f"Astept {config.SCAN_INTERVAL_SEC}s...")
            time.sleep(config.SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    FVGBot().run()
