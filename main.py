"""
FVG BOT 1H — v6 FINAL
- Scaneaza TOATE simbolurile (551 USDT futures)
- Triple-loop: PENDING 30s + ACTIVE 60s + SCAN 700s
- SCAN_INTERVAL 700s — mai mare decat durata reala (~500s) pentru a evita
  suprapunerea cu botul 4H si rate limit-urile Binance
- _get_capital() cu cache 600s — apelat O SINGURA DATA per ciclu scan,
  nu la fiecare simbol
- DLL persistent + flotante
- Delay 0.4s intre simboluri (safe rate)
"""
import sys, io, time, logging
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

        # Cache capital — initializat la None, incarcat prima data in run()
        self._cap_cache = None
        self._cap_ts    = 0

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG BOT 1H — v6 (scan complet, rate limit fix)")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  EMA: {config.EMA_FAST}/{config.EMA_SLOW} | Slope: {config.EMA_MIN_SLOPE*100:.1f}%/{config.EMA_SLOPE_BARS}bars")
        logger.info(f"  Max pozitii: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  DLL: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% din capital/zi")
        logger.info(f"  SCAN interval: 700s (evita suprapunere cu 4H)")
        logger.info("═══════════════════════════════════════════════════════")

    # ─── CAPITAL (cache 600s) ────────────────────────────────

    def _get_capital(self) -> float:
        """
        Returneaza capitalul din cont.
        Cache 600s — apelat O SINGURA DATA la inceputul fiecarui ciclu scan,
        NU la fiecare simbol (evita rate limit).
        """
        now_ts = time.time()
        if self._cap_cache and (now_ts - self._cap_ts < 600):
            return self._cap_cache

        try:
            bal = self.client.futures_account_balance()
            cap = 0.0
            for b in bal:
                if b.get("asset") == "USDT":
                    v = float(b.get("walletBalance") or b.get("balance") or 0)
                    if v > 0:
                        cap = v
                        break
            if cap < 10:
                cap = config.USDT_PER_TRADE * config.MAX_OPEN_TRADES
            self._cap_cache = cap
            self._cap_ts    = now_ts
            logger.info(f"Capital actualizat: {cap:.2f} USDT")
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("_get_capital: rate limit — folosesc cache")
            else:
                logger.warning(f"_get_capital error: {e}")
            # Foloseste cache-ul vechi sau fallback
            if not self._cap_cache:
                self._cap_cache = config.USDT_PER_TRADE * config.MAX_OPEN_TRADES
        except Exception as e:
            logger.warning(f"_get_capital error: {e}")
            if not self._cap_cache:
                self._cap_cache = config.USDT_PER_TRADE * config.MAX_OPEN_TRADES

        return self._cap_cache

    # ─── DLL ────────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _dll_active(self, capital: float) -> bool:
        """
        Verifica Daily Loss Limit.
        Primeste capitalul ca parametru — NU face apel API suplimentar.
        """
        today        = self._today()
        closed_loss  = self.om.daily_pnl.get(today, 0.0)
        floating_loss = 0.0

        try:
            if self.om.active_positions:
                positions = self.client.futures_position_information()
                for p in positions:
                    if p["symbol"] in self.om.active_positions:
                        u = float(p.get("unRealizedProfit", 0))
                        if u < 0:
                            floating_loss += u
        except BinanceAPIException as e:
            if e.code == -1003:
                pass  # skip flotante la rate limit
        except Exception:
            pass

        total_loss = closed_loss + floating_loss
        limit      = -(capital * config.DAILY_LOSS_LIMIT_PCT)

        if total_loss <= limit:
            logger.info(
                f"⛔ DLL activ: inchise={closed_loss:.2f} + "
                f"flotante={floating_loss:.2f} = {total_loss:.2f} "
                f"(limita: {limit:.2f})"
            )
            return True
        return False

    # ─── SIMBOLURI + KLINES ─────────────────────────────────

    def get_symbols(self) -> list:
        now_ts = time.time()
        cache  = getattr(self, "_symbols_cache", [])
        if cache and (now_ts - getattr(self, "_symbols_ts", 0) < 900):
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
                logger.warning("get_symbols: rate limit — sleep 30s + retry")
                time.sleep(30)
                try:
                    info = self.client.futures_exchange_info()
                    syms = [
                        s["symbol"] for s in info["symbols"]
                        if s["symbol"].endswith("USDT")
                        and s["status"] == "TRADING"
                        and s["symbol"] not in config.BLACKLIST
                    ]
                    self._symbols_cache = syms
                    self._symbols_ts    = time.time()
                    return syms
                except Exception:
                    return cache
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
                time.sleep(2)
                return []
            if e.code != -1121:
                logger.warning(f"[{symbol}] klines: {e}")
            return []
        except Exception as e:
            logger.warning(f"[{symbol}] klines: {e}")
            return []

    # ─── SCAN ───────────────────────────────────────────────

    def scan_symbol(self, symbol: str, capital: float):
        klines = self.get_klines(symbol)
        if not klines:
            return

        df      = prepare_df(klines)
        last_ts = df.index[-1]

        if self.last_candle_ts.get(symbol) == last_ts:
            return

        setup = detect_fvg(symbol, df)
        self.last_candle_ts[symbol] = last_ts

        if setup is None:
            return

        logger.info(
            f"[{symbol}] FVG {setup.direction} | RSI={setup.rsi} | "
            f"Entry={setup.entry:.6f} | SL={setup.sl:.6f} | "
            f"TP={setup.tp:.6f} | Slope={setup.slope_fast:+.3f}%"
        )

        # DLL check — capital deja calculat la inceputul ciclului
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

    # ─── RAPORT ─────────────────────────────────────────────

    def check_and_send_report(self):
        if time.time() - self.last_report_time >= config.TELEGRAM_REPORT_HOURS * 3600:
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
                "commission_paid": 0.0,
                "start_time":     self.stats["start"],
                "dll_today":      dll_today,
                "timeframe":      config.TIMEFRAME,
            })
            self.last_report_time = time.time()
            logger.info("Raport Telegram trimis.")

    # ─── RUN — TRIPLE LOOP ──────────────────────────────────

    def run(self):
        """
        Triple loop:
        - PENDING (30s):  batch check ordine umplute
        - ACTIVE  (60s):  check pozitii inchise + SL watchdog
        - SCAN    (700s): scaneaza TOATE simbolurile

        700s ales pentru ca scan-ul real dureaza 470-700s.
        Astfel 1H si 4H nu se suprapun niciodata pe acelasi IP Render.

        Capital citit O SINGURA DATA la inceputul fiecarui ciclu SCAN
        si transmis ca parametru — NU la fiecare simbol.
        """
        logger.info("Reconciliere cu Binance...")
        self.om.reconcile_with_binance()
        logger.info("Bot 1H pornit. Ctrl+C pentru oprire.")

        PENDING_INTERVAL = 120   # 120s — scan dureaza 800-1000s, 30s era prea agresiv
        ACTIVE_INTERVAL  = 120   # 120s — sincronizat cu pending
        SCAN_INTERVAL    = 1200  # 1200s — mai mare decat durata reala (~1000s)

        last_pending = 0
        last_active  = 0
        last_scan    = 0

        while True:
            try:
                now = time.time()

                # ── PENDING (30s) ────────────────────────────
                if now - last_pending >= PENDING_INTERVAL:
                    try:
                        c1 = self.om._check_pending()
                        c3 = self.om._expire_old_orders()
                        if c1 or c3:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code != -1003:
                            logger.error(f"Pending check: {e}")
                    except Exception as e:
                        logger.error(f"Pending check: {e}")
                    last_pending = time.time()

                # ── ACTIVE (60s) — check pozitii inchise de Guardian ─
                if now - last_active >= ACTIVE_INTERVAL:
                    try:
                        c2 = self.om._check_active_positions()
                        if c2:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code != -1003:
                            logger.error(f"Active check: {e}")
                    except Exception as e:
                        logger.error(f"Active check: {e}")
                    last_active = time.time()

                # ── SCAN (700s) — TOATE SIMBOLURILE ──────────
                if now - last_scan >= SCAN_INTERVAL:
                    active  = self.om.count_active_trades()
                    pending = len(self.om.pending_orders)

                    if active >= config.MAX_OPEN_TRADES:
                        logger.info(f"PAUZA — {active}/{config.MAX_OPEN_TRADES} pozitii")
                        self.check_and_send_report()
                        last_scan = time.time()
                        continue

                    # Capital citit O SINGURA DATA per ciclu ──
                    capital = self._get_capital()

                    if self._dll_active(capital):
                        logger.info(f"PAUZA ZILNICA — DLL activ | {active} pozitii")
                        self.check_and_send_report()
                        last_scan = time.time()
                        continue

                    symbols    = self.get_symbols()
                    scan_start = time.time()
                    logger.info(
                        f"SCAN COMPLET: {len(symbols)} perechi | "
                        f"Pozitii: {active}/{config.MAX_OPEN_TRADES} | "
                        f"Pending: {pending} | "
                        f"DLL azi: {self.om.daily_pnl.get(self._today(), 0):+.2f} USDT"
                    )

                    scanned = 0
                    for sym in symbols:
                        if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
                            logger.info("Limita atinsa — opresc scan")
                            break
                        if self._dll_active(capital):
                            logger.info("DLL atins — opresc scan")
                            break
                        try:
                            self.scan_symbol(sym, capital)
                            scanned += 1
                        except BinanceAPIException as e:
                            logger.error(f"[{sym}] BinanceError: {e}")
                        except Exception as e:
                            logger.error(f"[{sym}] Eroare: {e}")
                        time.sleep(0.40)

                    scan_dur = time.time() - scan_start
                    logger.info(
                        f"Ciclu complet | "
                        f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
                        f"Scanate: {scanned}/{len(symbols)} in {scan_dur:.0f}s | "
                        f"Pozitii: {self.om.count_active_trades()}/{config.MAX_OPEN_TRADES}"
                    )

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
