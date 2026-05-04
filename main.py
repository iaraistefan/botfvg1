"""
═══════════════════════════════════════════════════════════
  FVG BOT 1H — v12 (REST + Anti-Restart + Anti-Rate-Limit)
═══════════════════════════════════════════════════════════
Strategie de design: **PROFESIONAL ȘI PRUDENT**

Schimbări față de v10/v11:
  ✓ ELIMINAT WebSocket (Binance Futures WS public blocat pe IP datacenter)
  ✓ ANTI-RESTART-LOOP: niciodată Exit cu non-zero — sleep infinit la failure
    (împiedică Render auto-restart pe IP banat = ban prelungit)
  ✓ Skip reconcile dacă state file e recent (<10 min) — economie weight
  ✓ Delay scan 1.0s (de la 0.4s) — burst weight redus la 300/min (din 750)
  ✓ Cache exchange_info 60 min (de la default reload) — economie weight
  ✓ Backoff respectos: parsez timestamp ban din mesaj -1003, aștept exact
  ✓ Cache floating loss 30s (deja era în v10)
  ✓ Notify Telegram la startup și la rate limit prelungit
"""
import sys, io, time, logging, os, json
from datetime import datetime, timezone
from typing import Optional

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


# ─── PARAMETRI ANTI-RATE-LIMIT (sigure profesional) ──────
SCAN_DELAY_SEC      = 1.0    # delay între simboluri (era 0.4 → 1.0 = sub 300 weight/min)
SCAN_INTERVAL_SEC   = 900    # 15 min (era 700 → 900 ca să avem buffer la finishing)
PENDING_INTERVAL    = 30
ACTIVE_INTERVAL     = 60
EXCHANGE_INFO_TTL   = 3600   # 60 min cache
RECONCILE_TTL_SEC   = 600    # skip reconcile dacă state file < 10 min vechime


def _parse_ban_timestamp(error_msg: str) -> Optional[int]:
    """Extrage timestamp-ul de unban din mesaj '-1003'."""
    try:
        # Format: "...IP(X.X.X.X) banned until 1777835098800. Please use..."
        if "banned until" in error_msg:
            parts = error_msg.split("banned until")
            num_str = parts[1].strip().split(".")[0].split(" ")[0]
            return int(num_str)
    except Exception:
        pass
    return None


def _wait_until_ban_expires(error_msg: str, max_wait: int = 7200):
    """Așteaptă PASIV până când banul expiră. Maxim 2 ore."""
    ban_ts = _parse_ban_timestamp(error_msg)
    if ban_ts is None:
        wait_s = 300  # 5 min default
        logger.warning(f"Ban timestamp ne-parsabil. Aștept {wait_s}s default.")
    else:
        now_ms = int(time.time() * 1000)
        wait_ms = max(60_000, ban_ts - now_ms + 30_000)  # min 60s, +30s buffer
        wait_s = min(max_wait, wait_ms // 1000)
        ban_dt = datetime.fromtimestamp(ban_ts/1000, tz=timezone.utc)
        logger.warning(
            f"Ban până la {ban_dt.strftime('%H:%M:%S')} UTC. "
            f"Aștept PASIV {wait_s}s (NU fac API calls)."
        )
    time.sleep(wait_s)


class FVGBot1H:
    def __init__(self):
        self.client           = Client(config.API_KEY, config.API_SECRET)
        self.om               = OrderManager(self.client)
        self.last_candle_ts   = {}
        self.last_report_time = time.time()
        self.stats = {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

        # Cache
        self._cap_cache = None
        self._cap_ts    = 0
        self._floating_loss_cache = 0.0
        self._floating_loss_ts    = 0

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG BOT 1H — v12 (REST + Anti-Rate-Limit FINAL)")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  Detector: GAP%≥{config.MIN_GAP_PCT*100:.2f} | ATR_MULT≥{config.MIN_GAP_ATR_MULT}")
        logger.info(f"            RSI∈[{config.RSI_BULL_MIN},{config.RSI_BULL_MAX}] | AGGR={config.AGGR_FACTOR}")
        logger.info(f"            Wick≤{config.MAX_WICK_RATIO} | EMA slope≥{config.EMA_MIN_SLOPE*100:.2f}%")
        logger.info(f"  Entry: ENTRY_FILL_RATIO={config.ENTRY_FILL_RATIO} (mid-gap)")
        logger.info(f"  Max poziții: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  Scan delay: {SCAN_DELAY_SEC}s | Interval: {SCAN_INTERVAL_SEC}s")
        logger.info(f"  DLL: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% din capital/zi")
        logger.info("═══════════════════════════════════════════════════════")

    # ─── CAPITAL (cache 600s) ────────────────────────────────

    def _get_capital(self) -> float:
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

    def _get_floating_loss(self) -> float:
        """Cache 30s pentru floating loss — evită 1 call/simbol."""
        now_ts = time.time()
        if (now_ts - self._floating_loss_ts) < 30:
            return self._floating_loss_cache

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
                return self._floating_loss_cache
        except Exception:
            return self._floating_loss_cache

        self._floating_loss_cache = floating_loss
        self._floating_loss_ts    = now_ts
        return floating_loss

    def _dll_active(self, capital: float) -> bool:
        today        = self._today()
        closed_loss  = self.om.daily_pnl.get(today, 0.0)
        floating_loss = self._get_floating_loss()
        total_loss = closed_loss + floating_loss
        limit      = -(capital * config.DAILY_LOSS_LIMIT_PCT)

        if total_loss <= limit:
            logger.info(
                f"⛔ DLL activ: închise={closed_loss:.2f} + "
                f"flotante={floating_loss:.2f} = {total_loss:.2f} (limită: {limit:.2f})"
            )
            return True
        return False

    # ─── SIMBOLURI (cache 60 min) ───────────────────────────

    def get_symbols(self) -> list:
        now_ts = time.time()
        cache  = getattr(self, "_symbols_cache", [])
        if cache and (now_ts - getattr(self, "_symbols_ts", 0) < EXCHANGE_INFO_TTL):
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
                logger.warning("get_symbols: rate limit (-1003)")
                _wait_until_ban_expires(str(e))
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
                # Rate limit detectat → așteptăm pasiv până banul expiră
                logger.warning(f"[{symbol}] rate limit la klines")
                _wait_until_ban_expires(str(e))
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
            f"Entry={setup.entry:.6f} | Gap={setup.gap_bot:.6f}↔{setup.gap_top:.6f} | "
            f"ATR={setup.atr:.6f} | Slope={setup.slope_fast:+.3f}%"
        )

        if self._dll_active(capital):
            logger.info(f"[{symbol}] SKIP — DLL activ")
            return

        if self.om.has_symbol(symbol):
            return

        if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
            logger.info(f"[{symbol}] SKIP — limită {config.MAX_OPEN_TRADES} atinsă")
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
                "be":             bstats.get("be", 0),
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

    # ─── SMART RECONCILE ─────────────────────────────────────

    def _smart_reconcile(self):
        """
        Skip reconcile dacă state file e recent (<10 min vechime).
        Asta economisește 45+ weight la fiecare restart Render.
        """
        sf = getattr(config, "STATE_FILE", "bot_state_1h.json")
        try:
            if os.path.exists(sf):
                mtime = os.path.getmtime(sf)
                age = time.time() - mtime
                if age < RECONCILE_TTL_SEC:
                    logger.info(
                        f"State file recent ({age:.0f}s). SKIP reconcile "
                        f"(economie ~45 weight)."
                    )
                    return
        except Exception:
            pass

        logger.info("State file vechi sau lipsă → fac reconcile cu Binance...")
        try:
            self.om.reconcile_with_binance()
        except Exception as e:
            logger.warning(f"Reconcile error: {e}")
            if "-1003" in str(e):
                _wait_until_ban_expires(str(e))

    # ═══════════════════════════════════════════════════════
    #  RUN — REST polling cu anti-restart
    # ═══════════════════════════════════════════════════════

    def _wait_passive_forever(self, reason: str):
        """Sleep infinit pasiv — pentru a preveni Render auto-restart."""
        logger.error(f"BOT INTRĂ ÎN SLEEP INFINIT: {reason}")
        logger.error("Pentru a-l reporni: Render Dashboard → Manual Deploy.")
        try:
            notify_error("Bot 1H — Sleep mode", reason[:200])
        except Exception:
            pass
        while True:
            time.sleep(3600)

    def run(self):
        # ── PASUL 1: Smart reconcile (skip dacă state file recent) ─
        self._smart_reconcile()

        # ── PASUL 2: Get symbols (cu retry pasiv robust) ──
        symbols = []
        attempt = 0
        max_attempts = 20

        while not symbols and attempt < max_attempts:
            attempt += 1
            symbols = self.get_symbols()
            if symbols:
                break

            wait_s = min(120 * attempt, 1800)
            logger.warning(
                f"Nu am simboluri (incercare {attempt}/{max_attempts}). "
                f"Aștept PASIV {wait_s}s — NU fac API calls."
            )
            if attempt == 5:
                try:
                    notify_error("Bot 1H startup",
                                 f"Rate limit persistent — incercare {attempt}")
                except Exception:
                    pass
            time.sleep(wait_s)

        if not symbols:
            self._wait_passive_forever(
                "Nu pot obtine simboluri după 20 incercări (~5h cumulativ). "
                "IP probabil banat persistent. Manual Deploy când vrei să încerci din nou."
            )
            return  # never reached

        logger.info(f"✓ {len(symbols)} simboluri obținute")
        try:
            notify_error("Bot 1H pornit", f"v12 REST | {len(symbols)} simboluri")
        except Exception:
            pass

        # ── PASUL 3: Loop principal cu auto-recovery ────────
        last_pending = 0
        last_active  = 0
        last_scan    = 0

        while True:
            try:
                now = time.time()

                # PENDING (30s)
                if now - last_pending >= PENDING_INTERVAL:
                    try:
                        c1 = self.om._check_pending()
                        c3 = self.om._expire_old_orders()
                        if c1 or c3:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code == -1003:
                            _wait_until_ban_expires(str(e))
                        else:
                            logger.error(f"Pending check: {e}")
                    except Exception as e:
                        logger.error(f"Pending check: {e}")
                    last_pending = time.time()

                # ACTIVE (60s)
                if now - last_active >= ACTIVE_INTERVAL:
                    try:
                        c2 = self.om._check_active_positions()
                        if c2:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code == -1003:
                            _wait_until_ban_expires(str(e))
                        else:
                            logger.error(f"Active check: {e}")
                    except Exception as e:
                        logger.error(f"Active check: {e}")
                    last_active = time.time()

                # SCAN (900s = 15 min)
                if now - last_scan >= SCAN_INTERVAL_SEC:
                    active  = self.om.count_active_trades()
                    pending = len(self.om.pending_orders)

                    if active >= config.MAX_OPEN_TRADES:
                        logger.info(f"PAUZĂ — {active}/{config.MAX_OPEN_TRADES} poziții")
                        self.check_and_send_report()
                        last_scan = time.time()
                        continue

                    capital = self._get_capital()

                    if self._dll_active(capital):
                        logger.info(f"PAUZĂ ZILNICĂ — DLL activ | {active} poziții")
                        self.check_and_send_report()
                        last_scan = time.time()
                        continue

                    symbols    = self.get_symbols()
                    if not symbols:
                        logger.warning("Scan skipped — fără simboluri")
                        last_scan = time.time()
                        continue
                    
                    scan_start = time.time()
                    logger.info(
                        f"SCAN COMPLET: {len(symbols)} perechi | "
                        f"Poziții: {active}/{config.MAX_OPEN_TRADES} | "
                        f"Pending: {pending} | "
                        f"DLL azi: {self.om.daily_pnl.get(self._today(), 0):+.2f} USDT"
                    )

                    scanned = 0
                    for sym in symbols:
                        if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
                            logger.info("Limită atinsă — opresc scan")
                            break
                        if self._dll_active(capital):
                            logger.info("DLL atins — opresc scan")
                            break
                        try:
                            self.scan_symbol(sym, capital)
                            scanned += 1
                        except BinanceAPIException as e:
                            if e.code == -1003:
                                _wait_until_ban_expires(str(e))
                                break  # opresc scan, reîncerc next cycle
                            else:
                                logger.error(f"[{sym}] BinanceError: {e}")
                        except Exception as e:
                            logger.error(f"[{sym}] Eroare: {e}")
                        time.sleep(SCAN_DELAY_SEC)

                    scan_dur = time.time() - scan_start
                    logger.info(
                        f"Ciclu complet | "
                        f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
                        f"Scanate: {scanned}/{len(symbols)} în {scan_dur:.0f}s | "
                        f"Poziții: {self.om.count_active_trades()}/{config.MAX_OPEN_TRADES}"
                    )

                    self.check_and_send_report()
                    last_scan = time.time()

                time.sleep(2)

            except KeyboardInterrupt:
                logger.info("Bot oprit manual.")
                break
            except Exception as e:
                # CRITIC: NU IES — log și continui
                logger.error(f"Eroare iterație loop (continui): {type(e).__name__}: {e}")
                try:
                    notify_error("Loop 1H", str(e)[:200])
                except Exception:
                    pass
                time.sleep(30)


if __name__ == "__main__":
    try:
        FVGBot1H().run()
    except KeyboardInterrupt:
        logger.info("Botul s-a oprit la cerere utilizator.")
    except Exception as e:
        # Last resort: sleep infinit pentru a preveni Render auto-restart loop
        logger.critical(f"FATAL: {type(e).__name__}: {e}")
        logger.critical("Sleep infinit pentru a preveni Render auto-restart loop.")
        try:
            notify_error("Bot 1H FATAL", str(e)[:200])
        except Exception:
            pass
        while True:
            time.sleep(3600)
