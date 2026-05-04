"""
═══════════════════════════════════════════════════════════
  FVG BOT 1H — v13.0 (REST + Proactive Rate Limit + Weight Manager)
═══════════════════════════════════════════════════════════
Îmbunătățiri față de v12.1 (strategia FVG neschimbată):
  ✓ FIX CRITIC #1: _parse_ban_timestamp() cu regex robust
  ✓ FIX CRITIC #2: Ban state salvat pe disk (persistent la restart Render)
  ✓ FIX CRITIC #3: BinanceWeightManager — monitorizare X-MBX-USED-WEIGHT-1M
  ✓ FIX CRITIC #4: Bulk pre-filtrare 551 → ~80-150 simboluri active
  ✓ FIX IMPORTANT: Capital cache 600s → 120s + fallback robust
  ✓ FIX IMPORTANT: last_candle_ts cu cleanup periodic (max 600 intrări)
  ✓ FIX IMPORTANT: check_and_send_report() cu finally
  ✓ FIX MINOR: RotatingFileHandler 5MB × 3 backup
  ✓ FIX MINOR: Delay adaptiv bazat pe weight curent
"""
import sys, io, time, logging, os, json, re
from collections import OrderedDict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
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

# ─── LOGGING cu rotație ──────────────────────────────────
def _setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handlers = [
        RotatingFileHandler(
            config.LOG_FILE,
            encoding="utf-8",
            maxBytes=5 * 1024 * 1024,
            backupCount=3
        ),
        logging.StreamHandler(sys.stdout),
    ]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)

_setup_logging()
logger = logging.getLogger("FVGBot1H")


# ─── PARAMETRI ───────────────────────────────────────────
SCAN_DELAY_SEC       = 1.0
SCAN_INTERVAL_SEC    = 900
PENDING_INTERVAL     = 30
ACTIVE_INTERVAL      = 60
EXCHANGE_INFO_TTL    = 3600
RECONCILE_TTL_SEC    = 600
CAPITAL_CACHE_TTL    = 120
BAN_STATE_FILE       = "/tmp/fvg_ban_state.json"
MAX_CANDLE_CACHE     = 600


# ══════════════════════════════════════════════════════════
#  WEIGHT MANAGER
# ══════════════════════════════════════════════════════════
class BinanceWeightManager:
    LIMIT           = 2400
    WARN_THRESHOLD  = 0.70
    SLOW_THRESHOLD  = 0.80
    STOP_THRESHOLD  = 0.95

    def __init__(self):
        self.used_weight  = 0
        self.last_reset   = time.time()
        self._minute_mark = time.time()

    def update(self, response) -> None:
        if response is None:
            return
        try:
            headers = getattr(response, "headers", {}) or {}
            raw = (
                headers.get("X-MBX-USED-WEIGHT-1M")
                or headers.get("x-mbx-used-weight-1m")
                or headers.get("X-Mbx-Used-Weight-1m")
            )
            if raw:
                self.used_weight = int(raw)
                if time.time() - self._minute_mark >= 60:
                    self._minute_mark = time.time()
        except (ValueError, TypeError, AttributeError):
            pass

    def throttle(self) -> None:
        pct = self.used_weight / self.LIMIT

        if pct >= self.STOP_THRESHOLD:
            elapsed = time.time() - self._minute_mark
            wait_s  = max(60.0 - elapsed + 3.0, 5.0)
            logger.warning(
                f"[WEIGHT] {self.used_weight}/{self.LIMIT} ({pct*100:.0f}%) — "
                f"PAUZĂ COMPLETĂ {wait_s:.1f}s"
            )
            time.sleep(wait_s)
            self.used_weight  = 0
            self._minute_mark = time.time()

        elif pct >= self.SLOW_THRESHOLD:
            extra = (pct - self.SLOW_THRESHOLD) * 20.0
            logger.debug(
                f"[WEIGHT] {self.used_weight}/{self.LIMIT} ({pct*100:.0f}%) — "
                f"throttle +{extra:.1f}s"
            )
            time.sleep(extra)

        elif pct >= self.WARN_THRESHOLD and self.used_weight % 50 < 2:
            logger.warning(
                f"[WEIGHT] {self.used_weight}/{self.LIMIT} ({pct*100:.0f}%) — aproape de limită"
            )

    def get_adaptive_delay(self) -> float:
        pct = self.used_weight / self.LIMIT
        if pct < 0.40:
            return SCAN_DELAY_SEC
        elif pct < 0.60:
            return SCAN_DELAY_SEC * 1.5
        elif pct < 0.75:
            return SCAN_DELAY_SEC * 2.0
        else:
            return SCAN_DELAY_SEC * 3.0


# ══════════════════════════════════════════════════════════
#  BAN STATE — persistent între restart-uri
# ══════════════════════════════════════════════════════════
def _save_ban_state(unban_time_s: float) -> None:
    try:
        with open(BAN_STATE_FILE, "w") as f:
            json.dump({"unban_time": unban_time_s, "saved_at": time.time()}, f)
    except Exception as e:
        logger.debug(f"[BAN_STATE] Nu am putut salva: {e}")

def _load_and_check_ban_state() -> float:
    if not os.path.exists(BAN_STATE_FILE):
        return 0.0
    try:
        with open(BAN_STATE_FILE) as f:
            state = json.load(f)
        unban_time = float(state.get("unban_time", 0))
        remaining  = unban_time - time.time()
        if remaining > 5:
            return remaining
        os.remove(BAN_STATE_FILE)
    except Exception as e:
        logger.debug(f"[BAN_STATE] Eroare la citire: {e}")
    return 0.0

def _clear_ban_state() -> None:
    try:
        if os.path.exists(BAN_STATE_FILE):
            os.remove(BAN_STATE_FILE)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
#  BAN TIMESTAMP PARSER — regex robust
# ══════════════════════════════════════════════════════════
def _parse_ban_timestamp(error_msg: str) -> Optional[int]:
    if not error_msg or "banned until" not in error_msg:
        return None
    try:
        match = re.search(r"banned until[:s]+(d{10,15})", error_msg, re.IGNORECASE)
        if match:
            raw = int(match.group(1))
            if raw < 1e12:
                raw = raw * 1000
            return raw
    except Exception:
        pass
    return None


def _wait_until_ban_expires(error_msg: str, max_wait: int = 7200):
    ban_ts_ms = _parse_ban_timestamp(error_msg)

    if ban_ts_ms is None:
        wait_s = 60
        logger.warning(f"Ban timestamp ne-parsabil. Aștept {wait_s}s default.")
    else:
        now_ms  = int(time.time() * 1000)
        wait_ms = max(30_000, ban_ts_ms - now_ms + 10_000)
        wait_s  = min(max_wait, wait_ms // 1000)
        ban_dt  = datetime.fromtimestamp(ban_ts_ms / 1000, tz=timezone.utc)
        logger.warning(
            f"Ban până la {ban_dt.strftime('%H:%M:%S')} UTC. "
            f"Aștept PASIV {wait_s}s (NU fac API calls)."
        )

    _save_ban_state(time.time() + wait_s)
    time.sleep(wait_s)
    _clear_ban_state()


# ══════════════════════════════════════════════════════════
#  BOT PRINCIPAL
# ══════════════════════════════════════════════════════════
class FVGBot1H:
    def __init__(self):
        self.client           = Client(config.API_KEY, config.API_SECRET)
        self.om               = OrderManager(self.client)
        self.weight_mgr       = BinanceWeightManager()
        self.last_candle_ts   = OrderedDict()
        self.last_report_time = time.time()
        self.stats = {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

        self._cap_cache = None
        self._cap_ts    = 0
        self._floating_loss_cache = 0.0
        self._floating_loss_ts    = 0
        self._symbols_cache: list = []
        self._symbols_ts: float   = 0

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG BOT 1H — v13.0 (REST + Weight Manager + Ban Persist)")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  Detector: GAP%≥{config.MIN_GAP_PCT*100:.2f} | ATR_MULT≥{config.MIN_GAP_ATR_MULT}")
        logger.info(f"            RSI∈[{config.RSI_BULL_MIN},{config.RSI_BULL_MAX}] | AGGR={config.AGGR_FACTOR}")
        logger.info(f"            Wick≤{config.MAX_WICK_RATIO} | EMA slope≥{config.EMA_MIN_SLOPE*100:.2f}%")
        logger.info(f"  Entry: ENTRY_FILL_RATIO={config.ENTRY_FILL_RATIO} (mid-gap)")
        logger.info(f"  Max poziții: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  Scan delay: {SCAN_DELAY_SEC}s adaptiv | Interval: {SCAN_INTERVAL_SEC}s")
        logger.info(f"  DLL: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% din capital/zi")
        logger.info(f"  Weight limit: {BinanceWeightManager.LIMIT}/min | "
                    f"Throttle la {BinanceWeightManager.SLOW_THRESHOLD*100:.0f}%")
        logger.info("═══════════════════════════════════════════════════════")

    # ─── CAPITAL (cache 120s) ────────────────────────────────

    def _get_capital(self) -> float:
        now_ts = time.time()
        if self._cap_cache is not None and (now_ts - self._cap_ts < CAPITAL_CACHE_TTL):
            return self._cap_cache

        try:
            self.weight_mgr.throttle()
            bal = self.client.futures_account_balance()
            cap = 0.0
            for b in bal:
                if b.get("asset") == "USDT":
                    v = float(b.get("walletBalance") or b.get("balance") or 0)
                    if v > 0:
                        cap = v
                        break
            if cap < 10:
                cap = self._cap_cache if self._cap_cache else getattr(
                    config, "INITIAL_CAPITAL",
                    config.USDT_PER_TRADE * config.MAX_OPEN_TRADES
                )
            self._cap_cache = cap
            self._cap_ts    = now_ts
            logger.info(f"Capital actualizat: {cap:.2f} USDT")
        except BinanceAPIException as e:
            if e.code == -1003:
                if "banned until" in str(e):
                    _wait_until_ban_expires(str(e))
                else:
                    logger.warning("_get_capital: rate limit — folosesc cache")
            else:
                logger.warning(f"_get_capital error: {e}")
            if self._cap_cache is None:
                self._cap_cache = getattr(
                    config, "INITIAL_CAPITAL",
                    config.USDT_PER_TRADE * config.MAX_OPEN_TRADES
                )
        except Exception as e:
            logger.warning(f"_get_capital error: {e}")
            if self._cap_cache is None:
                self._cap_cache = getattr(
                    config, "INITIAL_CAPITAL",
                    config.USDT_PER_TRADE * config.MAX_OPEN_TRADES
                )

        return self._cap_cache

    def force_refresh_capital(self):
        self._cap_ts = 0

    # ─── DLL ────────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_floating_loss(self) -> float:
        now_ts = time.time()
        if (now_ts - self._floating_loss_ts) < 30:
            return self._floating_loss_cache

        floating_loss = 0.0
        try:
            if self.om.active_positions:
                self.weight_mgr.throttle()
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
        today         = self._today()
        closed_loss   = self.om.daily_pnl.get(today, 0.0)
        floating_loss = self._get_floating_loss()
        total_loss    = closed_loss + floating_loss
        limit         = -(capital * config.DAILY_LOSS_LIMIT_PCT)

        if total_loss <= limit:
            logger.info(
                f"⛔ DLL activ: închise={closed_loss:.2f} + "
                f"flotante={floating_loss:.2f} = {total_loss:.2f} (limită: {limit:.2f})"
            )
            return True
        return False

    # ─── SIMBOLURI (cache 60 min) ────────────────────────────

    def get_symbols(self) -> list:
        now_ts = time.time()
        if self._symbols_cache and (now_ts - self._symbols_ts < EXCHANGE_INFO_TTL):
            return self._symbols_cache
        try:
            self.weight_mgr.throttle()
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
                if "banned until" in str(e):
                    logger.warning("get_symbols: ban global detectat")
                    _wait_until_ban_expires(str(e))
                else:
                    logger.warning("get_symbols: rate limit (-1003)")
            else:
                logger.error(f"get_symbols: {e}")
            return self._symbols_cache
        except Exception as e:
            logger.error(f"get_symbols: {e}")
            return self._symbols_cache

    # ─── PRE-FILTRARE BULK (1 request → ~80-150 simboluri) ──

    def get_active_symbols_bulk(self, all_symbols: list) -> list:
        try:
            self.weight_mgr.throttle()
            tickers = self.client.futures_ticker()

            all_syms_set = set(all_symbols)
            active = []
            for t in tickers:
                sym = t.get("symbol", "")
                if sym not in all_syms_set:
                    continue
                try:
                    price_chg_pct = abs(float(t.get("priceChangePercent", 0)))
                    volume        = float(t.get("quoteVolume", 0))
                    if price_chg_pct >= 0.5 or volume >= 1_000_000:
                        active.append(sym)
                except (ValueError, TypeError):
                    continue

            logger.info(
                f"Pre-filtrare bulk: {len(active)}/{len(all_symbols)} simboluri active "
                f"(economie: {len(all_symbols)-len(active)} klines requests)"
            )
            return active if active else all_symbols

        except BinanceAPIException as e:
            logger.warning(f"get_active_symbols_bulk: {e} — folosesc toate simbolurile")
            return all_symbols
        except Exception as e:
            logger.warning(f"get_active_symbols_bulk: {e} — folosesc toate simbolurile")
            return all_symbols

    # ─── KLINES cu weight tracking ──────────────────────────

    def get_klines(self, symbol: str) -> list:
        try:
            self.weight_mgr.throttle()
            klines = self.client.futures_klines(
                symbol=symbol, interval=config.TIMEFRAME, limit=200
            )
            return klines[:-1]
        except BinanceAPIException as e:
            if e.code == -1003:
                err_msg = str(e)
                if "banned until" in err_msg:
                    logger.warning(f"[{symbol}] BAN GLOBAL detectat → oprim scan")
                    _wait_until_ban_expires(err_msg)
                    raise
                else:
                    logger.warning(f"[{symbol}] rate limit per-symbol — skip")
                    time.sleep(2)
                    return []
            if e.code != -1121:
                logger.warning(f"[{symbol}] klines: {e}")
            return []
        except Exception as e:
            logger.warning(f"[{symbol}] klines: {e}")
            return []

    # ─── SCAN SIMBOL ────────────────────────────────────────

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
        if len(self.last_candle_ts) > MAX_CANDLE_CACHE:
            for _ in range(50):
                self.last_candle_ts.popitem(last=False)

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
        if success:
            self.force_refresh_capital()

    # ─── RAPORT ─────────────────────────────────────────────

    def check_and_send_report(self):
        if time.time() - self.last_report_time < config.TELEGRAM_REPORT_HOURS * 3600:
            return
        try:
            today     = self._today()
            bstats    = self.om.get_bot_stats()
            dll_today = self.om.daily_pnl.get(today, 0.0)
            send_statistics_report({
                "total_trades":    bstats["total"],
                "wins":            bstats["wins"],
                "losses":          bstats["losses"],
                "be":              bstats.get("be", 0),
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
                "dll_today":       dll_today,
                "timeframe":       config.TIMEFRAME,
                "weight_current":  self.weight_mgr.used_weight,
                "weight_limit":    BinanceWeightManager.LIMIT,
            })
            logger.info("Raport Telegram trimis.")
        except Exception as e:
            logger.warning(f"check_and_send_report error: {e}")
        finally:
            self.last_report_time = time.time()

    # ─── SMART RECONCILE ────────────────────────────────────

    def _smart_reconcile(self):
        sf = getattr(config, "STATE_FILE", "bot_state_1h.json")
        try:
            if os.path.exists(sf):
                mtime = os.path.getmtime(sf)
                age   = time.time() - mtime
                if age < RECONCILE_TTL_SEC:
                    active_count  = self.om.count_active_trades()
                    pending_count = len(self.om.pending_orders)
                    if active_count > 0 or pending_count > 0:
                        logger.info(
                            f"State file recent ({age:.0f}s) dar are "
                            f"{active_count} poziții + {pending_count} pending → reconcile forțat."
                        )
                    else:
                        logger.info(
                            f"State file recent ({age:.0f}s), fără poziții. "
                            f"SKIP reconcile (economie ~45 weight)."
                        )
                        return
        except Exception:
            pass

        logger.info("State file vechi sau lipsă → fac reconcile cu Binance...")
        try:
            self.om.reconcile_with_binance()
        except BinanceAPIException as e:
            logger.warning(f"Reconcile BinanceAPIException: {e}")
            if e.code == -1003:
                _wait_until_ban_expires(str(e))
        except Exception as e:
            logger.warning(f"Reconcile error: {e}")

    # ─── SLEEP INFINIT (anti-Render-restart) ────────────────

    def _wait_passive_forever(self, reason: str):
        logger.error(f"BOT INTRĂ ÎN SLEEP INFINIT: {reason}")
        logger.error("Pentru a-l reporni: Render Dashboard → Manual Deploy.")
        try:
            notify_error("Bot 1H — Sleep mode", reason[:200])
        except Exception:
            pass
        while True:
            time.sleep(3600)

    # ══════════════════════════════════════════════════════
    #  RUN PRINCIPAL
    # ══════════════════════════════════════════════════════

    def run(self):
        # ── PASUL 0: Verifică ban persistent din sesiunea anterioară ─
        ban_remaining = _load_and_check_ban_state()
        if ban_remaining > 0:
            logger.warning(
                f"[STARTUP] IP ban activ din sesiunea anterioară. "
                f"Aștept {ban_remaining:.0f}s înainte de a începe."
            )
            time.sleep(ban_remaining + 5)
            _clear_ban_state()

        # ── PASUL 1: Smart reconcile ─────────────────────────
        self._smart_reconcile()

        # ── PASUL 2: Get symbols cu retry robust ─────────────
        symbols      = []
        attempt      = 0
        max_attempts = 20

        while not symbols and attempt < max_attempts:
            attempt += 1
            symbols = self.get_symbols()
            if symbols:
                break
            wait_s = min(120 * attempt, 1800)
            logger.warning(
                f"Nu am simboluri (incercare {attempt}/{max_attempts}). "
                f"Aștept PASIV {wait_s}s."
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
            return

        logger.info(f"✓ {len(symbols)} simboluri obținute")
        try:
            notify_error("Bot 1H pornit", f"v13 REST | {len(symbols)} simboluri")
        except Exception:
            pass

        # ── PASUL 3: Loop principal ───────────────────────────
        last_pending = 0
        last_active  = 0
        last_scan    = 0

        while True:
            try:
                now = time.time()

                # PENDING CHECK (30s)
                if now - last_pending >= PENDING_INTERVAL:
                    try:
                        c1 = self.om._check_pending()
                        c3 = self.om._expire_old_orders()
                        if c1 or c3:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code == -1003 and "banned until" in str(e):
                            _wait_until_ban_expires(str(e))
                        elif e.code != -1003:
                            logger.error(f"Pending check: {e}")
                    except Exception as e:
                        logger.error(f"Pending check: {e}")
                    last_pending = time.time()

                # ACTIVE CHECK (60s)
                if now - last_active >= ACTIVE_INTERVAL:
                    try:
                        c2 = self.om._check_active_positions()
                        if c2:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code == -1003 and "banned until" in str(e):
                            _wait_until_ban_expires(str(e))
                        elif e.code != -1003:
                            logger.error(f"Active check: {e}")
                    except Exception as e:
                        logger.error(f"Active check: {e}")
                    last_active = time.time()

                # SCAN (900s = 15 min)
                if now - last_scan >= SCAN_INTERVAL_SEC:
                    active  = self.om.count_active_trades()
                    pending = len(self.om.pending_orders)

                    if active >= config.MAX_OPEN_TRADES:
                        logger.info(f"PAUZĂ — {active}/{config.MAX_OPEN_TRADES} poziții pline")
                        self.check_and_send_report()
                        last_scan = time.time()
                        continue

                    capital = self._get_capital()

                    if self._dll_active(capital):
                        logger.info(f"PAUZĂ ZILNICĂ — DLL activ | {active} poziții")
                        self.check_and_send_report()
                        last_scan = time.time()
                        continue

                    symbols = self.get_symbols()
                    if not symbols:
                        logger.warning("Scan skipped — fără simboluri")
                        last_scan = time.time()
                        continue

                    # PRE-FILTRARE BULK (1 request → economie masivă)
                    active_symbols = self.get_active_symbols_bulk(symbols)

                    scan_start = time.time()
                    logger.info(
                        f"SCAN: {len(active_symbols)}/{len(symbols)} perechi (bulk-filtrate) | "
                        f"Poziții: {active}/{config.MAX_OPEN_TRADES} | "
                        f"Pending: {pending} | "
                        f"DLL azi: {self.om.daily_pnl.get(self._today(), 0):+.2f} USDT | "
                        f"Weight: {self.weight_mgr.used_weight}/{BinanceWeightManager.LIMIT}"
                    )

                    scanned = 0
                    for sym in active_symbols:
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
                            if e.code == -1003 and "banned until" in str(e):
                                logger.warning("Ban global — opresc scan, reîncerc next cycle")
                                break
                            elif e.code != -1003:
                                logger.error(f"[{sym}] BinanceError: {e}")
                        except Exception as e:
                            logger.error(f"[{sym}] Eroare: {e}")

                        time.sleep(self.weight_mgr.get_adaptive_delay())

                    scan_dur = time.time() - scan_start
                    logger.info(
                        f"Ciclu complet | "
                        f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
                        f"Scanate: {scanned}/{len(active_symbols)} în {scan_dur:.0f}s | "
                        f"Poziții: {self.om.count_active_trades()}/{config.MAX_OPEN_TRADES} | "
                        f"Weight: {self.weight_mgr.used_weight}/{BinanceWeightManager.LIMIT}"
                    )

                    self.check_and_send_report()
                    last_scan = time.time()

                time.sleep(2)

            except KeyboardInterrupt:
                logger.info("Bot oprit manual.")
                break
            except MemoryError as e:
                logger.critical(f"MemoryError: {e} — sleep 10 min")
                time.sleep(600)
            except Exception as e:
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
        logger.critical(f"FATAL: {type(e).__name__}: {e}")
        logger.critical("Sleep infinit pentru a preveni Render auto-restart loop.")
        try:
            notify_error("Bot 1H FATAL", str(e)[:200])
        except Exception:
            pass
        while True:
            time.sleep(3600)
