"""
═══════════════════════════════════════════════════════════
  FVG BOT 1H — v11 (WebSocket streaming)
═══════════════════════════════════════════════════════════
Schimbări față de v10:
  ✓ DATA prin WebSocket (zero rate limit, latență <100ms)
  ✓ NU mai există scan secvențial — reacționăm la fiecare candle close
  ✓ NU mai există SCAN_INTERVAL — fiecare simbol e detectat când 
    candela lui se închide (la XX:00 UTC pentru 1H)
  ✓ Păstrăm: PENDING check 30s, ACTIVE check 60s (REST necesar)
  ✓ Păstrăm: cache capital 600s, cache floating loss 30s

Comportament:
  - Pornire: descarcă istoric 200 bare/simbol via REST (~6 min)
  - După: WS connection permanent, callback la fiecare candle close
  - PENDING/ACTIVE checks rămân REST (necesare pentru orderuri)
"""
import sys, io, time, logging, threading
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException

import config
from detector import detect_fvg
from order_manager import OrderManager
from notifier import notify_setup, notify_trade, notify_error, send_statistics_report
from ws_data_manager import WSDataManager

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
        self.last_report_time = time.time()
        self.stats = {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

        # Cache
        self._cap_cache = None
        self._cap_ts    = 0
        self._floating_loss_cache = 0.0
        self._floating_loss_ts    = 0

        # Lock pentru a serializa apelurile API din threads diferite
        self._api_lock = threading.Lock()
        # Lock pentru _on_candle_close (rulat din WS thread)
        self._scan_lock = threading.Lock()

        # WebSocket data manager
        self.ws: Optional[WSDataManager] = None

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG BOT 1H — v11 (WebSocket streaming)")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  Detector: GAP%≥{config.MIN_GAP_PCT*100:.2f} | ATR_MULT≥{config.MIN_GAP_ATR_MULT}")
        logger.info(f"            RSI∈[{config.RSI_BULL_MIN},{config.RSI_BULL_MAX}] | AGGR={config.AGGR_FACTOR}")
        logger.info(f"            Wick≤{config.MAX_WICK_RATIO} | EMA slope≥{config.EMA_MIN_SLOPE*100:.2f}%")
        logger.info(f"  Entry: ENTRY_FILL_RATIO={config.ENTRY_FILL_RATIO} (mid-gap)")
        logger.info(f"  Max poziții: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  DLL: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}% din capital/zi")
        logger.info("═══════════════════════════════════════════════════════")

    # ─── CAPITAL (cache 600s) ────────────────────────────────

    def _get_capital(self) -> float:
        now_ts = time.time()
        if self._cap_cache and (now_ts - self._cap_ts < 600):
            return self._cap_cache

        with self._api_lock:
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
        now_ts = time.time()
        if (now_ts - self._floating_loss_ts) < 30:
            return self._floating_loss_cache

        floating_loss = 0.0
        with self._api_lock:
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

    # ─── SIMBOLURI ──────────────────────────────────────────

    def get_symbols(self) -> list:
        now_ts = time.time()
        cache  = getattr(self, "_symbols_cache", [])
        if cache and (now_ts - getattr(self, "_symbols_ts", 0) < 900):
            return cache
        with self._api_lock:
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
                    return cache
                logger.error(f"get_symbols: {e}")
                return cache
            except Exception as e:
                logger.error(f"get_symbols: {e}")
                return cache

    # ─── CALLBACK CANDLE CLOSE (din WS thread) ──────────────

    def _on_candle_close(self, symbol: str, df: pd.DataFrame):
        """
        Apelat de WSDataManager când o candelă se ÎNCHIDE.
        Rulează detector pe df și plasează ordin dacă e cazul.
        ATENȚIE: Rulează pe fir WebSocket — folosim _scan_lock pentru thread-safety.
        """
        with self._scan_lock:
            try:
                if df is None or len(df) < 100:
                    return
                
                setup = detect_fvg(symbol, df)
                if setup is None:
                    return
                
                logger.info(
                    f"[{symbol}] FVG {setup.direction} | RSI={setup.rsi} | "
                    f"Entry={setup.entry:.6f} | Gap={setup.gap_bot:.6f}↔{setup.gap_top:.6f} | "
                    f"ATR={setup.atr:.6f} | Slope={setup.slope_fast:+.3f}%"
                )
                
                # Capital + DLL
                capital = self._get_capital()
                if self._dll_active(capital):
                    logger.info(f"[{symbol}] SKIP — DLL activ")
                    return
                
                if self.om.has_symbol(symbol):
                    return
                
                if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
                    logger.info(f"[{symbol}] SKIP — limită {config.MAX_OPEN_TRADES} atinsă")
                    return
                
                # Plasează ordinul (cu API lock)
                with self._api_lock:
                    notify_setup(setup)
                    success = self.om.place_fvg_trade(setup)
                    notify_trade(setup, success)
            
            except Exception as e:
                logger.error(f"[{symbol}] _on_candle_close error: {e}")

    # ─── RAPORT ─────────────────────────────────────────────

    def check_and_send_report(self):
        if time.time() - self.last_report_time >= config.TELEGRAM_REPORT_HOURS * 3600:
            today    = self._today()
            bstats   = self.om.get_bot_stats()
            dll_today = self.om.daily_pnl.get(today, 0.0)
            ws_status = self.ws.get_status() if self.ws else {}
            
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
            logger.info(f"Raport Telegram trimis. WS status: {ws_status.get('candles_closed',0)} candele primite, healthy={ws_status.get('is_healthy')}")

    # ─── RUN ────────────────────────────────────────────────

    def run(self):
        """
        Triple loop:
        - PENDING (30s):  REST check ordine umplute
        - ACTIVE  (60s):  REST check pozitii inchise
        - WS:             primeste candele live -> callback -> detector + plasare
        """
        logger.info("Reconciliere cu Binance...")
        self.om.reconcile_with_binance()
        
        # Pornire WebSocket
        symbols = self.get_symbols()
        if not symbols:
            logger.error("Nu am putut obtine lista simboluri. Stop.")
            return
        
        logger.info(f"Inițializare WebSocket pentru {len(symbols)} simboluri...")
        self.ws = WSDataManager(self.client, config.TIMEFRAME, on_candle_close=self._on_candle_close)
        
        # Init buffers (REST, ~6 min cu delay 0.6s)
        self.ws.init_buffers(symbols, delay_per_symbol=0.6)
        
        # Pornire WebSocket
        self.ws.start(symbols)
        
        logger.info("Bot 1H pornit (WebSocket streaming). Ctrl+C pentru oprire.")

        PENDING_INTERVAL = 30
        ACTIVE_INTERVAL  = 60
        WS_HEALTH_INTERVAL = 300  # health check la 5 min

        last_pending = 0
        last_active  = 0
        last_health  = time.time()

        try:
            while True:
                now = time.time()

                # PENDING (30s) — REST necesar
                if now - last_pending >= PENDING_INTERVAL:
                    try:
                        with self._api_lock:
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

                # ACTIVE (60s) — REST necesar
                if now - last_active >= ACTIVE_INTERVAL:
                    try:
                        with self._api_lock:
                            c2 = self.om._check_active_positions()
                        if c2:
                            self.om._save()
                    except BinanceAPIException as e:
                        if e.code != -1003:
                            logger.error(f"Active check: {e}")
                    except Exception as e:
                        logger.error(f"Active check: {e}")
                    last_active = time.time()

                # WS HEALTH (5 min) — printez status pentru monitorizare
                if now - last_health >= WS_HEALTH_INTERVAL:
                    if self.ws:
                        s = self.ws.get_status()
                        active = self.om.count_active_trades()
                        pending = len(self.om.pending_orders)
                        logger.info(
                            f"WS status: {s['candles_closed']} candele închise | "
                            f"{s['callbacks_fired']} detectoari rulați | "
                            f"reconnects={s['reconnects']} | errors={s['errors']} | "
                            f"healthy={s['is_healthy']} | "
                            f"Pozitii: {active}/{config.MAX_OPEN_TRADES} | Pending: {pending} | "
                            f"DLL azi: {self.om.daily_pnl.get(self._today(), 0):+.2f}"
                        )
                    self.check_and_send_report()
                    last_health = time.time()

                time.sleep(2)

        except KeyboardInterrupt:
            logger.info("Bot oprit manual.")
        except Exception as e:
            logger.error(f"Eroare loop: {e}")
            notify_error("Loop 1H", str(e))
        finally:
            if self.ws:
                self.ws.stop()


if __name__ == "__main__":
    FVGBot1H().run()
