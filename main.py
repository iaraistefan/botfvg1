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

# ─── LOGGING cu rotire fișier ─────────────────────────────────
_file_handler = RotatingFileHandler(
    config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("FVGBot1H")


# ─── PARAMETRI ────────────────────────────────────────────────
SCAN_DELAY_SEC    = 1.0
SCAN_INTERVAL_SEC = 900
PENDING_INTERVAL  = 30
ACTIVE_INTERVAL   = 60
EXCHANGE_INFO_TTL = 3600
RECONCILE_TTL_SEC = 600

# ─── FLAG GLOBAL BAN IP ───────────────────────────────────────
# Setat de _wait_until_ban_expires() — văzut de TOATE funcțiile
_global_ban_until: float = 0.0


# ════════════════════════════════════════════════════════════════
#  FUNCȚII ANTI-RATE-LIMIT
# ════════════════════════════════════════════════════════════════

def _parse_ban_timestamp(error_msg: str) -> Optional[float]:
    """
    Extrage secunde de așteptat din ORICE variantă de mesaj Binance -1003.
    Prinde: cu/fără IP în mesaj, timestamp ms sau secunde, orice prefix.

    Exemple mesaje Binance:
      - "Way too many requests; IP(1.2.3.4) banned until 1746357542800."
      - "(-1003) banned until 1746357542800."
      - "banned until 1746357542"  (secunde)
    """
    m = re.search(r'banneds+untils+(d{10,15})', error_msg, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        now_ms = int(time.time() * 1000)
        if val > 1_000_000_000_000:           # timestamp în milisecunde
            wait_ms = val - now_ms + 5000     # +5s buffer de siguranță
        else:                                  # timestamp în secunde
            wait_ms = (val * 1000) - now_ms + 5000
        return max(wait_ms / 1000, 5.0)       # minim 5s
    return None


def _wait_until_ban_expires(error_msg: str, max_wait: int = 7200):
    """
    Așteaptă PASIV până când banul de IP expiră.
    CRITIC: Setează _global_ban_until ca _check_pending și _check_active
            să nu mai bată API-ul în zadar în perioada de ban!
    """
    global _global_ban_until

    wait_s = _parse_ban_timestamp(error_msg)

    if wait_s is None:
        wait_s = 60.0
        logger.warning(f"Ban timestamp ne-parsabil. Aștept {wait_s:.0f}s default.")
    else:
        wait_s = min(float(wait_s), float(max_wait))
        ban_expire_ts = time.time() + wait_s
        ban_dt = datetime.fromtimestamp(ban_expire_ts, tz=timezone.utc)
        logger.warning(
            f"Ban IP până la {ban_dt.strftime('%H:%M:%S')} UTC. "
            f"Aștept PASIV {wait_s:.0f}s (ZERO API calls în acest timp)."
        )

    # Setăm flag-ul ÎNAINTE de sleep — toate funcțiile îl văd imediat
    _global_ban_until = time.time() + wait_s + 2.0
    time.sleep(wait_s)
    _global_ban_until = 0.0
    logger.info("Ban expirat — reluăm operațiunile normale.")


def _is_banned() -> bool:
    """Verifică rapid dacă suntem în perioadă de ban IP."""
    return time.time() < _global_ban_until


# ════════════════════════════════════════════════════════════════
#  WEIGHT MANAGER
# ════════════════════════════════════════════════════════════════

class BinanceWeightManager:
    """
    Monitorizează X-MBX-USED-WEIGHT-1M și throttle proactiv.
    Limita Futures: 2400/min.
    """
    LIMIT    = 2400
    WARN_PCT = 0.70
    SLOW_PCT = 0.80
    STOP_PCT = 0.95

    def __init__(self):
        self._used  = 0
        self._reset = time.time()

    def update(self, headers: dict):
        val = headers.get("X-MBX-USED-WEIGHT-1M", "0")
        try:
            self._used = int(val)
        except (ValueError, TypeError):
            pass

    @property
    def used(self) -> int:
        return self._used

    def throttle(self):
        """Blochează dacă suntem aproape de limită. Apelează ÎNAINTE de fiecare request."""
        used = self._used
        pct  = used / self.LIMIT

        if pct >= self.STOP_PCT:
            elapsed = time.time() - self._reset
            wait    = max(62.0 - elapsed, 5.0)
            logger.warning(
                f"[WEIGHT] {used}/{self.LIMIT} ({pct*100:.0f}%) — "
                f"pauză completă {wait:.0f}s până la reset."
            )
            time.sleep(wait)
            self._used  = 0
            self._reset = time.time()

        elif pct >= self.SLOW_PCT:
            extra = (pct - self.SLOW_PCT) / (1.0 - self.SLOW_PCT) * 3.0
            logger.debug(f"[WEIGHT] {used}/{self.LIMIT} — delay extra {extra:.1f}s")
            time.sleep(extra)

        elif pct >= self.WARN_PCT:
            logger.info(f"[WEIGHT] {used}/{self.LIMIT} ({pct*100:.0f}%) — zona galbenă")

    def status_str(self) -> str:
        return f"{self._used}/{self.LIMIT}"


weight_mgr = BinanceWeightManager()


# ════════════════════════════════════════════════════════════════
#  CLASA PRINCIPALĂ
# ════════════════════════════════════════════════════════════════

class FVGBot1H:
    def __init__(self):
        self.client           = Client(config.API_KEY, config.API_SECRET)
        self.om               = OrderManager(self.client)
        self.last_candle_ts   = OrderedDict()
        self.last_report_time = time.time()
        self.stats = {
            "start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

        self._cap_cache: Optional[float] = None
        self._cap_ts: float = 0

        self._floating_loss_cache: float = 0.0
        self._floating_loss_ts: float = 0

        self._symbols_cache: list = []
        self._symbols_ts: float = 0

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG BOT 1H — v13.0 (REST + Global Ban Fix)")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  Detector: GAP%≥{config.MIN_GAP_PCT*100:.2f} | ATR_MULT≥{config.MIN_GAP_ATR_MULT}")
        logger.info(f"            RSI∈[{config.RSI_BULL_MIN},{config.RSI_BULL_MAX}] | AGGR={config.AGGR_FACTOR}")
        logger.info(f"            Wick≤{config.MAX_WICK_RATIO} | EMA slope≥{config.EMA_MIN_SLOPE*100:.2f}%")
        logger.info(f"  Entry: ENTRY_FILL_RATIO={config.ENTRY_FILL_RATIO} (mid-gap)")
        logger.info(f"  Max poziții: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  Scan delay: {SCAN_DELAY_SEC}s | Interval: {SCAN_INTERVAL_SEC}s")
        logger.info(f"  DLL: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% din capital/zi")
        logger.info("═══════════════════════════════════════════════════════")

    # ─── CAPITAL (cache 120s) ──────────────────────────────────────

    def _get_capital(self) -> float:
        now_ts = time.time()
        if self._cap_cache and (now_ts - self._cap_ts < 120):
            return self._cap_cache

        if _is_banned():
            logger.debug("_get_capital: skip — ban activ, folosesc cache")
            return self._cap_cache or config.USDT_PER_TRADE * config.MAX_OPEN_TRADES

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
                if "banned until" in str(e):
                    _wait_until_ban_expires(str(e))
                else:
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

    # ─── DLL ──────────────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_floating_loss(self) -> float:
        now_ts = time.time()
        if (now_ts - self._floating_loss_ts) < 30:
            return self._floating_loss_cache

        if _is_banned():
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

    # ─── SIMBOLURI (cache 60 min) ──────────────────────────────────

    def get_symbols(self) -> list:
        now_ts = time.time()
        if self._symbols_cache and (now_ts - self._symbols_ts < EXCHANGE_INFO_TTL):
            return self._symbols_cache

        if _is_banned():
            logger.debug("get_symbols: skip — ban activ, folosesc cache")
            return self._symbols_cache

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
                if "banned until" in str(e):
                    logger.warning("get_symbols: ban global detectat")
                    _wait_until_ban_expires(str(e))
                else:
                    logger.warning("get_symbols: rate limit — folosesc cache")
            else:
                logger.error(f"get_symbols: {e}")
            return self._symbols_cache
        except Exception as e:
            logger.error(f"get_symbols: {e}")
            return self._symbols_cache

    # ─── PRE-FILTRARE BULK ────────────────────────────────────────

    def get_active_symbols_bulk(self, all_symbols: list) -> list:
        """
        Un singur request futures_ticker() pre-filtrează simbolurile active.
        Economisește ~80% din weight-ul de klines la fiecare scan.
        """
        if _is_banned():
            return all_symbols

        try:
            weight_mgr.throttle()
            tickers = self.client.futures_ticker()
            sym_set    = set(all_symbols)
            candidates = []

            for t in tickers:
                sym = t.get("symbol", "")
                if sym not in sym_set:
                    continue
                try:
                    pct    = abs(float(t.get("priceChangePercent", 0)))
                    volume = float(t.get("quoteVolume", 0))
                    if pct >= 0.5 or volume >= 500_000:
                        candidates.append(sym)
                except (ValueError, KeyError):
                    continue

            saved = len(all_symbols) - len(candidates)
            logger.info(
                f"Pre-filtrare bulk: {len(candidates)}/{len(all_symbols)} simboluri active "
                f"(economie: {saved} klines requests)"
            )
            return candidates if candidates else all_symbols

        except BinanceAPIException as e:
            if e.code == -1003:
                if "banned until" in str(e):
                    _wait_until_ban_expires(str(e))
                else:
                    logger.warning("get_active_symbols_bulk: rate limit — scanăm tot")
            else:
                logger.warning(f"get_active_symbols_bulk: {e}")
            return all_symbols
        except Exception as e:
            logger.warning(f"get_active_symbols_bulk: {e}")
            return all_symbols

    # ─── KLINES ───────────────────────────────────────────────────

    def get_klines(self, symbol: str) -> list:
        if _is_banned():
            return []

        weight_mgr.throttle()
        try:
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

    # ─── SCAN SIMBOL ──────────────────────────────────────────────

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
        if len(self.last_candle_ts) > 600:
            self.last_candle_ts.popitem(last=False)

        if setup is None:
            return

        logger.info(
            f"[{symbol}] FVG {setup.direction} | RSI={setup.rsi:.1f} | "
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

    # ─── RAPORT TELEGRAM ──────────────────────────────────────────

    def check_and_send_report(self):
        try:
            if time.time() - self.last_report_time >= config.TELEGRAM_REPORT_HOURS * 3600:
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
                })
                logger.info("Raport Telegram trimis.")
        except Exception as e:
            logger.warning(f"Raport Telegram: {e}")
        finally:
            self.last_report_time = time.time()

    # ─── SMART RECONCILE ──────────────────────────────────────────

    def _smart_reconcile(self):
        sf = getattr(config, "STATE_FILE", "bot_state_1h.json")

        if os.path.exists(sf):
            try:
                with open(sf, "r") as f:
                    state = json.load(f)
                has_active = bool(state.get("active_positions", {}))
            except Exception:
                has_active = True

            if has_active:
                logger.info("Poziții active detectate → reconcile FORȚAT cu Binance.")
                try:
                    self.om.reconcile_with_binance()
                except Exception as e:
                    logger.warning(f"Reconcile error: {e}")
                    if "-1003" in str(e):
                        _wait_until_ban_expires(str(e))
                return

            try:
                age = time.time() - os.path.getmtime(sf)
                if age < RECONCILE_TTL_SEC:
                    logger.info(
                        f"State file recent ({age:.0f}s), fără poziții active. "
                        f"SKIP reconcile (economie ~45 weight)."
                    )
                    return
            except Exception:
                pass

        logger.info("State file vechi sau lipsă → reconcile cu Binance...")
        try:
            self.om.reconcile_with_binance()
        except Exception as e:
            logger.warning(f"Reconcile error: {e}")
            if "-1003" in str(e):
                _wait_until_ban_expires(str(e))

    # ─── SLEEP INFINIT ────────────────────────────────────────────

    def _wait_passive_forever(self, reason: str):
        logger.error(f"BOT INTRĂ ÎN SLEEP INFINIT: {reason}")
        logger.error("Pentru a-l reporni: Render Dashboard → Manual Deploy.")
        try:
            notify_error("Bot 1H — Sleep mode", reason[:200])
        except Exception:
            pass
        while True:
            time.sleep(3600)

    # ════════════════════════════════════════════════════════════════
    #  RUN
    # ════════════════════════════════════════════════════════════════

    def run(self):
        global _global_ban_until
        ban_file = "/tmp/fvg_ban_state.json"

        # ── PASUL 0: Verificare ban persistent din sesiunea anterioară ──
        if os.path.exists(ban_file):
            try:
                with open(ban_file) as f:
                    ban_state = json.load(f)
                unban_time = float(ban_state.get("unban_time", 0))
                remaining  = unban_time - time.time()
                if remaining > 0:
                    logger.warning(
                        f"[STARTUP] Ban IP activ din sesiunea anterioară. "
                        f"Aștept {remaining:.0f}s înainte de primul API call."
                    )
                    _global_ban_until = unban_time
                    time.sleep(remaining + 2)
                    _global_ban_until = 0.0
                os.remove(ban_file)
            except Exception as e:
                logger.warning(f"[STARTUP] Nu am putut citi ban state: {e}")

        # ── PASUL 1: Smart reconcile ──────────────────────────────
        self._smart_reconcile()

        # ── PASUL 2: Obținere simboluri ───────────────────────────
        symbols   = []
        attempt   = 0
        max_attempts = 20

        while not symbols and attempt < max_attempts:
            attempt += 1
            symbols = self.get_symbols()
            if symbols:
                break
            wait_s = min(120 * attempt, 1800)
            logger.warning(
                f"Nu am simboluri (încercare {attempt}/{max_attempts}). "
                f"Aștept PASIV {wait_s}s."
            )
            if attempt == 5:
                try:
                    notify_error("Bot 1H startup",
                                 f"Rate limit persistent — încercare {attempt}")
                except Exception:
                    pass
            time.sleep(wait_s)

        if not symbols:
            self._wait_passive_forever(
                "Nu pot obține simboluri după 20 încercări (~5h cumulativ). "
                "IP probabil banat persistent. Manual Deploy când vrei să reîncerci."
            )
            return

        logger.info(f"✓ {len(symbols)} simboluri obținute")
        try:
            notify_error("Bot 1H pornit", f"v13 REST | {len(symbols)} simboluri")
        except Exception:
            pass

        # ── PASUL 3: Loop principal ───────────────────────────────
        last_pending = 0.0
        last_active  = 0.0
        last_scan    = 0.0

        while True:
            try:
                now = time.time()

                # ── PENDING CHECK (30s) ──────────────────────────
                if now - last_pending >= PENDING_INTERVAL:
                    if _is_banned():
                        remaining = _global_ban_until - time.time()
                        logger.debug(f"_check_pending: skip — ban activ {remaining:.0f}s")
                    else:
                        try:
                            c1 = self.om._check_pending()
                            c3 = self.om._expire_old_orders()
                            if c1 or c3:
                                self.om._save()
                        except BinanceAPIException as e:
                            if e.code == -1003:
                                if "banned until" in str(e):
                                    wait_s_est = _parse_ban_timestamp(str(e)) or 60.0
                                    try:
                                        with open(ban_file, "w") as f:
                                            json.dump(
                                                {"unban_time": time.time() + wait_s_est}, f
                                            )
                                    except Exception:
                                        pass
                                    _wait_until_ban_expires(str(e))
                                else:
                                    logger.warning("_check_pending: rate limit — skip")
                            else:
                                logger.error(f"Pending check: {e}")
                        except Exception as e:
                            logger.error(f"Pending check: {e}")
                    last_pending = time.time()

                # ── ACTIVE CHECK (60s) ───────────────────────────
                if now - last_active >= ACTIVE_INTERVAL:
                    if _is_banned():
                        remaining = _global_ban_until - time.time()
                        logger.debug(f"_check_active: skip — ban activ {remaining:.0f}s")
                    else:
                        try:
                            c2 = self.om._check_active_positions()
                            if c2:
                                self.om._save()
                        except BinanceAPIException as e:
                            if e.code == -1003:
                                if "banned until" in str(e):
                                    _wait_until_ban_expires(str(e))
                                else:
                                    logger.warning("_check_active: rate limit — skip")
                            else:
                                logger.error(f"Active check: {e}")
                        except Exception as e:
                            logger.error(f"Active check: {e}")
                    last_active = time.time()

                # ── SCAN COMPLET (900s = 15 min) ─────────────────
                if now - last_scan >= SCAN_INTERVAL_SEC:
                    active  = self.om.count_active_trades()
                    pending = len(self.om.pending_orders)

                    if active >= config.MAX_OPEN_TRADES:
                        logger.info(f"PAUZĂ — {active}/{config.MAX_OPEN_TRADES} poziții")
                        self.check_and_send_report()
                        last_scan = time.time()
                        time.sleep(2)
                        continue

                    capital = self._get_capital()

                    if self._dll_active(capital):
                        logger.info(f"PAUZĂ ZILNICĂ — DLL activ | {active} poziții")
                        self.check_and_send_report()
                        last_scan = time.time()
                        time.sleep(2)
                        continue

                    if _is_banned():
                        remaining = _global_ban_until - time.time()
                        logger.info(f"Scan amânat — ban activ {remaining:.0f}s")
                        time.sleep(2)
                        continue

                    symbols = self.get_symbols()
                    if not symbols:
                        logger.warning("Scan skipped — fără simboluri")
                        last_scan = time.time()
                        time.sleep(2)
                        continue

                    candidates = self.get_active_symbols_bulk(symbols)

                    scan_start = time.time()
                    logger.info(
                        f"SCAN: {len(candidates)}/{len(symbols)} perechi (bulk-filtrate) | "
                        f"Poziții: {active}/{config.MAX_OPEN_TRADES} | "
                        f"Pending: {pending} | "
                        f"DLL azi: {self.om.daily_pnl.get(self._today(), 0):+.2f} USDT | "
                        f"Weight: {weight_mgr.status_str()}"
                    )

                    scanned = 0
                    for sym in candidates:
                        if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
                            logger.info("Limită atinsă — opresc scan")
                            break
                        if self._dll_active(capital):
                            logger.info("DLL atins — opresc scan")
                            break
                        if _is_banned():
                            logger.warning("Ban activ în scan — opresc, reîncerc next cycle")
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

                        used_pct = weight_mgr.used / weight_mgr.LIMIT
                        if used_pct > 0.75:
                            time.sleep(SCAN_DELAY_SEC * 2.0)
                        elif used_pct > 0.60:
                            time.sleep(SCAN_DELAY_SEC * 1.5)
                        else:
                            time.sleep(SCAN_DELAY_SEC)

                    scan_dur = time.time() - scan_start
                    logger.info(
                        f"Ciclu complet | "
                        f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
                        f"Scanate: {scanned}/{len(candidates)} în {scan_dur:.0f}s | "
                        f"Poziții: {self.om.count_active_trades()}/{config.MAX_OPEN_TRADES} | "
                        f"Weight: {weight_mgr.status_str()}"
                    )

                    self.check_and_send_report()
                    last_scan = time.time()

                time.sleep(2)

            except KeyboardInterrupt:
                logger.info("Bot oprit manual.")
                break
            except Exception as e:
                logger.error(f"Eroare iterație loop (continui): {type(e).__name__}: {e}")
                try:
                    notify_error("Loop 1H", str(e)[:200])
                except Exception:
                    pass
                time.sleep(30)


# ════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        FVGBot1H().run()
    except KeyboardInterrupt:
        logger.info("Botul s-a oprit la cerere utilizator.")
    except Exception as e:
        logger.critical(f"FATAL: {type(e).__name__}: {e}")
        logger.critical("Sleep infinit — prevenim Render auto-restart loop.")
        try:
            notify_error("Bot 1H FATAL", str(e)[:200])
        except Exception:
            pass
        while True:
            time.sleep(3600)
