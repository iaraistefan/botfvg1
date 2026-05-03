"""
═══════════════════════════════════════════════════════════
  WEBSOCKET DATA MANAGER — STREAMING REAL-TIME OHLCV
═══════════════════════════════════════════════════════════
Gestionează conexiuni WebSocket Binance Futures pentru toate simbolurile
+ buffer OHLCV per simbol cu istoric pentru detector.

Arhitectură:
  1. Pornim 1 conexiune WS per BATCH de simboluri (max 200/conexiune)
  2. Pe fiecare update kline ("kline" event), stocăm ultima bară
  3. Când o bară se ÎNCHIDE (k.x == True), o adăugăm în buffer și
     declanșăm callback-ul "on_candle_close(symbol, df)" 
  4. Bot-ul primește callback-ul și rulează detector pe buffer-ul actualizat

Avantaje:
  - Zero weight Binance pentru market data
  - Latență <100ms pe candle close
  - Nu mai e nevoie de scan secvențial 551 simboluri

Pentru istoric (200 bare pentru indicatori), folosim REST O DATĂ la pornire,
apoi WebSocket pentru actualizări live.
"""
import asyncio
import json
import logging
import time
import threading
from collections import deque
from typing import Callable, Optional
import pandas as pd
import numpy as np

import websockets
from binance.client import Client
from binance.exceptions import BinanceAPIException

logger = logging.getLogger("FVGBot1H")

WS_BASE = "wss://fstream.binance.com/stream"
MAX_STREAMS_PER_CONNECTION = 200  # Binance limit ~1024, dar 200 e safer
HISTORY_BARS = 200                # bare pentru indicatori
RECONNECT_DELAY_SEC = 5
PING_INTERVAL_SEC = 30


class WSDataManager:
    """
    Gestionează datele OHLCV via WebSocket pentru un singur TF.
    
    Folosire:
      manager = WSDataManager(client, "1h", on_candle_close=callback_func)
      manager.start(symbols)
      # ... bot-ul rulează ...
      manager.stop()
    
    callback_func primește (symbol: str, df: pd.DataFrame) și e apelat
    pe firul WebSocket, deci callback-ul TREBUIE să fie thread-safe.
    """
    
    def __init__(self, client: Client, timeframe: str,
                 on_candle_close: Optional[Callable[[str, pd.DataFrame], None]] = None):
        self.client = client
        self.timeframe = timeframe
        self.on_candle_close = on_candle_close
        
        # Buffer per simbol — list de dicts cu OHLCV + timestamp
        self.buffers: dict[str, deque] = {}
        self._buffer_lock = threading.Lock()
        
        # Tracking conexiuni WebSocket
        self._ws_threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Stats pentru monitoring
        self.stats = {
            "candles_received":   0,
            "candles_closed":     0,
            "callbacks_fired":    0,
            "reconnects":         0,
            "errors":             0,
            "last_message_time":  0.0,
            "active_connections": 0,
        }
    
    # ─── INIT BUFFER cu istoric REST ──────────────────────
    
    def _fetch_history_rest(self, symbol: str) -> Optional[deque]:
        """Descarcă HISTORY_BARS bare istorice via REST (apel unic la pornire)."""
        try:
            klines = self.client.futures_klines(
                symbol=symbol, interval=self.timeframe, limit=HISTORY_BARS + 1
            )
            # Excludem ultima (incompletă)
            klines = klines[:-1]
            
            buf = deque(maxlen=HISTORY_BARS)
            for k in klines:
                buf.append({
                    "timestamp": int(k[0]),
                    "open":      float(k[1]),
                    "high":      float(k[2]),
                    "low":       float(k[3]),
                    "close":     float(k[4]),
                    "volume":    float(k[5]),
                })
            return buf
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning(f"[{symbol}] history rate limit — skip")
            else:
                logger.warning(f"[{symbol}] history error: {e}")
            return None
        except Exception as e:
            logger.warning(f"[{symbol}] history error: {e}")
            return None
    
    def init_buffers(self, symbols: list[str], delay_per_symbol: float = 0.6):
        """
        Descarcă istoric pentru toate simbolurile via REST.
        DOAR LA PORNIRE — apoi totul e prin WebSocket.
        
        delay_per_symbol: pauză între request-uri pentru a nu lua ban.
        """
        logger.info(f"[WS] Inițializez buffer-uri OHLCV pentru {len(symbols)} simboluri (REST)...")
        loaded = 0
        failed = 0
        
        for i, sym in enumerate(symbols):
            if self._stop_event.is_set():
                break
            buf = self._fetch_history_rest(sym)
            if buf is not None and len(buf) >= 100:
                with self._buffer_lock:
                    self.buffers[sym] = buf
                loaded += 1
            else:
                failed += 1
            
            # Progress la fiecare 50
            if (i + 1) % 50 == 0:
                logger.info(f"[WS] Init progress: {i+1}/{len(symbols)} (OK={loaded}, fail={failed})")
            
            time.sleep(delay_per_symbol)
        
        logger.info(f"[WS] Inițializare completă: {loaded} OK | {failed} eșuate")
    
    # ─── WEBSOCKET RUNNER ─────────────────────────────────
    
    def _build_stream_url(self, symbols_batch: list[str]) -> str:
        """
        Construiește URL multistream pentru un batch de simboluri.
        Format: wss://fstream.binance.com/stream?streams=btcusdt@kline_1h/ethusdt@kline_1h/...
        """
        streams = "/".join(f"{s.lower()}@kline_{self.timeframe}" for s in symbols_batch)
        return f"{WS_BASE}?streams={streams}"
    
    async def _ws_handler(self, symbols_batch: list[str], conn_id: int):
        """Handler pentru o singură conexiune WebSocket cu un batch de simboluri."""
        url = self._build_stream_url(symbols_batch)
        url_short = url[:80] + "..." if len(url) > 80 else url
        
        while not self._stop_event.is_set():
            try:
                logger.info(f"[WS#{conn_id}] Conectare la {len(symbols_batch)} simboluri...")
                async with websockets.connect(
                    url, 
                    ping_interval=PING_INTERVAL_SEC,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**22  # 4 MB max message
                ) as ws:
                    self.stats["active_connections"] += 1
                    logger.info(f"[WS#{conn_id}] ✓ Conectat. Așteptare candle close-uri...")
                    
                    async for message in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            self._handle_message(message)
                        except Exception as e:
                            logger.error(f"[WS#{conn_id}] handle error: {e}")
                            self.stats["errors"] += 1
                    
                    self.stats["active_connections"] -= 1
            
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"[WS#{conn_id}] Conexiune închisă: {e}. Reconnect în {RECONNECT_DELAY_SEC}s...")
                self.stats["reconnects"] += 1
            except Exception as e:
                logger.error(f"[WS#{conn_id}] Eroare conexiune: {e}. Reconnect în {RECONNECT_DELAY_SEC}s...")
                self.stats["errors"] += 1
            
            if not self._stop_event.is_set():
                await asyncio.sleep(RECONNECT_DELAY_SEC)
    
    def _handle_message(self, message: str):
        """Procesează un mesaj WebSocket."""
        self.stats["last_message_time"] = time.time()
        self.stats["candles_received"] += 1
        
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        
        # Format: {"stream": "btcusdt@kline_1h", "data": {...}}
        payload = data.get("data", {})
        if payload.get("e") != "kline":
            return
        
        k = payload.get("k", {})
        symbol = payload.get("s", "").upper()
        if not symbol or not k:
            return
        
        is_closed = k.get("x", False)
        if not is_closed:
            return  # Doar candele ÎNCHISE — nu interesează cele in-progress
        
        # Candela închisă — o adăugăm în buffer
        candle = {
            "timestamp": int(k["t"]),
            "open":      float(k["o"]),
            "high":      float(k["h"]),
            "low":       float(k["l"]),
            "close":     float(k["c"]),
            "volume":    float(k["v"]),
        }
        
        with self._buffer_lock:
            if symbol not in self.buffers:
                # Simbol fără istoric init — ignorăm
                return
            
            buf = self.buffers[symbol]
            # Verificăm că nu e duplicat (poate primim aceeași candle de 2 ori la reconnect)
            if buf and buf[-1]["timestamp"] == candle["timestamp"]:
                return
            buf.append(candle)
            df = self._buffer_to_df(buf)
        
        self.stats["candles_closed"] += 1
        
        # Callback (în firul WebSocket — userul trebuie să fie thread-safe)
        if self.on_candle_close:
            try:
                self.on_candle_close(symbol, df)
                self.stats["callbacks_fired"] += 1
            except Exception as e:
                logger.error(f"[WS] Callback error pentru {symbol}: {e}")
                self.stats["errors"] += 1
    
    def _buffer_to_df(self, buf: deque) -> pd.DataFrame:
        """Convertește buffer (deque de dict-uri) într-un DataFrame compatibil cu detector."""
        if not buf:
            return pd.DataFrame()
        df = pd.DataFrame(list(buf))
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["body"]  = abs(df["close"] - df["open"])
        df["range"] = df["high"] - df["low"]
        return df
    
    # ─── PORNIRE / OPRIRE ─────────────────────────────────
    
    def start(self, symbols: list[str]):
        """
        Pornește WebSocket-urile.
        Trebuie să fi apelat init_buffers() înainte!
        """
        if not self.buffers:
            logger.warning("[WS] init_buffers() nu a fost apelat — buffer-urile sunt goale!")
            return
        
        # Filtrăm doar simbolurile cu buffer init
        active_symbols = [s for s in symbols if s in self.buffers]
        
        # Împărțim în batch-uri
        batches = [active_symbols[i:i + MAX_STREAMS_PER_CONNECTION] 
                   for i in range(0, len(active_symbols), MAX_STREAMS_PER_CONNECTION)]
        
        logger.info(f"[WS] Pornire {len(batches)} conexiuni pentru {len(active_symbols)} simboluri")
        
        # Pornim event loop pe firul nostru
        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            
            tasks = [self._ws_handler(batch, i) for i, batch in enumerate(batches)]
            try:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            except Exception as e:
                logger.error(f"[WS] Loop error: {e}")
            finally:
                loop.close()
        
        thread = threading.Thread(target=run_loop, daemon=True, name="WSDataManager")
        thread.start()
        self._ws_threads.append(thread)
        
        # Așteptăm 2 secunde să se conecteze
        time.sleep(2)
        logger.info(f"[WS] Pornit. Conexiuni active: {self.stats['active_connections']}")
    
    def stop(self):
        """Oprește toate conexiunile WebSocket."""
        logger.info("[WS] Oprire...")
        self._stop_event.set()
        
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        
        for t in self._ws_threads:
            t.join(timeout=5)
        
        logger.info("[WS] Oprit.")
    
    # ─── ACCESSOR-I PUBLICI ───────────────────────────────
    
    def get_df(self, symbol: str) -> Optional[pd.DataFrame]:
        """Returnează DataFrame-ul OHLCV curent pentru un simbol."""
        with self._buffer_lock:
            buf = self.buffers.get(symbol)
            if buf is None:
                return None
            return self._buffer_to_df(buf)
    
    def get_status(self) -> dict:
        """Returnează stats pentru monitorizare."""
        last_msg_age = time.time() - self.stats["last_message_time"] if self.stats["last_message_time"] > 0 else None
        return {
            **self.stats,
            "symbols_tracked":      len(self.buffers),
            "last_message_age_sec": round(last_msg_age, 1) if last_msg_age else None,
            "is_healthy":           last_msg_age is not None and last_msg_age < 300,  # mesaj în <5 min
        }
    
    def has_symbol(self, symbol: str) -> bool:
        with self._buffer_lock:
            return symbol in self.buffers
