"""
═══════════════════════════════════════════════════════════
  FVG DETECTOR — v11 (după audit main.py v13 + order_manager v11)
═══════════════════════════════════════════════════════════
Fix-uri față de v10:
  ✓ FIX CRITIC: BEAR entry logic corectă — entry la gap_top (retest plafon)
                în loc de gap_bot (v10 era semantic invers)
  ✓ FIX CRITIC: prepare_df() guard pentru raw_klines gol / < 3 rânduri
  ✓ FIX IMPORTANT: filtru gap_filled — skip dacă prețul curent a depășit
                   deja gap-ul (gap mitificat înainte de intrare)
  ✓ FIX IMPORTANT: calc_atr() fill_value=0 pentru primul rând
  ✓ FIX IMPORTANT: candle_time fallback dacă indexul nu e datetime
  ✓ FIX MINOR: EMA calculat o singură dată în detect_fvg(), pasat la helpers
  ✓ FIX MINOR: slope_fast comentariu explicit (valoare în %, nu fracție)
  ✓ Toate filtrele v10 păstrate neschimbate (gap%, atr_mult, wick, rsi, ema, overext)
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional

from config import (
    MIN_GAP_PCT, MIN_GAP_ATR_MULT, MAX_WICK_RATIO, AGGR_FACTOR, AVG_BODY_PERIOD,
    RSI_PERIOD, RSI_BULL_MIN, RSI_BULL_MAX,
    EMA_FAST, EMA_SLOW, EMA_SLOPE_BARS, EMA_MIN_SLOPE,
    EMA_PARALLEL_MIN, EMA_PARALLEL_MAX, MAX_CONSEC_AGGR,
    ENTRY_FILL_RATIO, ATR_PERIOD,
)


@dataclass
class FVGSetup:
    """
    Setup FVG detectat.

    slope_fast — valoare în PROCENTE (ex: 0.697 înseamnă +0.697%)
                 calculată ca (ema_fast_now - ema_fast_prev) / ema_fast_prev * 100
    """
    symbol:      str
    direction:   str
    entry:       float
    gap_top:     float
    gap_bot:     float
    gap_height:  float
    rsi:         float
    ema_fast:    float
    ema_slow:    float
    slope_fast:  float   # în % (nu fracție)
    atr:         float
    candle_time: object  # pd.Timestamp sau str fallback


# ══════════════════════════════════════════════════════════
#  CALCULE TEHNICE
# ══════════════════════════════════════════════════════════

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, min_periods=period, adjust=False).mean()


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low   = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(fill_value=0)).abs()
    low_close  = (df["low"]  - df["close"].shift(fill_value=0)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()


def prepare_df(raw_klines: list) -> pd.DataFrame:
    """
    Convertește klines brute în DataFrame.
    Returnează DataFrame gol dacă input-ul e insuficient.
    """
    if not raw_klines or len(raw_klines) < 3:
        return pd.DataFrame()

    df = pd.DataFrame(raw_klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(subset=["open", "high", "low", "close"], inplace=True)
    df["body"]  = (df["close"] - df["open"]).abs()
    df["range"] = df["high"] - df["low"]
    return df


# ══════════════════════════════════════════════════════════
#  FILTRE HELPER
# ══════════════════════════════════════════════════════════

def _check_ema_filters(
    df: pd.DataFrame,
    direction: str,
    ema_f: pd.Series,
    ema_s: pd.Series,
):
    """
    Primește seriile EMA pre-calculate (nu le recalculează).
    Returnează (ok, reason, ef_now, es_now, slope_fast_pct)
    slope_fast_pct este în PROCENTE.
    """
    ef_now  = ema_f.iloc[-1]
    ef_prev = ema_f.iloc[-(EMA_SLOPE_BARS + 1)]
    es_now  = ema_s.iloc[-1]
    es_prev = ema_s.iloc[-(EMA_SLOPE_BARS + 1)]

    if any(pd.isna(v) for v in [ef_now, ef_prev, es_now, es_prev]):
        return False, "EMA insuficient calculata", 0.0, 0.0, 0.0

    if ef_prev == 0 or es_prev == 0:
        return False, "EMA prev = 0", 0.0, 0.0, 0.0

    slope_fast = (ef_now - ef_prev) / ef_prev   # fracție
    slope_slow = (es_now - es_prev) / es_prev   # fracție

    if direction == "BULL":
        if slope_fast <= 0 or slope_slow <= 0:
            return False, "EMA descrescatoare", ef_now, es_now, slope_fast
        if ef_now < es_now:
            return False, "EMA50 sub EMA100 in BULL", ef_now, es_now, slope_fast
    else:  # BEAR
        if slope_fast >= 0 or slope_slow >= 0:
            return False, "EMA crescatoare", ef_now, es_now, slope_fast
        if ef_now > es_now:
            return False, "EMA50 deasupra EMA100 in BEAR", ef_now, es_now, slope_fast

    if abs(slope_fast) < EMA_MIN_SLOPE:
        return False, f"Panta EMA50 prea mica ({abs(slope_fast):.4f})", ef_now, es_now, slope_fast
    if abs(slope_slow) < EMA_MIN_SLOPE * 0.5:
        return False, f"Panta EMA100 prea mica ({abs(slope_slow):.4f})", ef_now, es_now, slope_fast

    ratio = abs(slope_fast) / abs(slope_slow)
    if ratio < EMA_PARALLEL_MIN or ratio > EMA_PARALLEL_MAX:
        return False, f"EMA nu sunt paralele (ratio={ratio:.2f})", ef_now, es_now, slope_fast

    return True, "OK", ef_now, es_now, slope_fast


def _check_overextension(
    df: pd.DataFrame,
    avg_body: float,
    direction: str,
) -> tuple:
    consec = 0
    for i in range(3, 3 + MAX_CONSEC_AGGR + 2):
        if i >= len(df):
            break
        candle = df.iloc[-i]
        body   = candle["body"]
        is_bull = candle["close"] > candle["open"]
        is_bear = candle["close"] < candle["open"]
        is_aggr = body >= avg_body * AGGR_FACTOR

        if direction == "BULL" and is_bull and is_aggr:
            consec += 1
        elif direction == "BEAR" and is_bear and is_aggr:
            consec += 1
        else:
            break

    if consec >= MAX_CONSEC_AGGR:
        return False, f"Supraextindere: {consec} lumanari agresive consecutive"
    return True, "OK"


# ══════════════════════════════════════════════════════════
#  DETECT FVG — ENTRY POINT PRINCIPAL
# ══════════════════════════════════════════════════════════

def detect_fvg(symbol: str, df: pd.DataFrame) -> Optional[FVGSetup]:
    """
    Detectează un setup FVG valid pe ultimele 3 lumânări închise.

    Structura de indexare:
        c2 = df.iloc[-3]  — lumânarea STÂNGA (cea mai veche dintre 3)
        c1 = df.iloc[-2]  — lumânarea MIJLOC (cea agresivă — corpul FVG)
        c0 = df.iloc[-1]  — lumânarea DREAPTA (cea mai recentă)

    BULL FVG: c1 este bullish agresivă, gap = c2.high ↔ c0.low
    BEAR FVG: c1 este bearish agresivă, gap = c0.high ↔ c2.low
    """
    if df is None or df.empty:
        return None

    min_len = max(
        AVG_BODY_PERIOD,
        RSI_PERIOD * 3,
        EMA_SLOW + EMA_SLOPE_BARS,
        ATR_PERIOD * 3,
    ) + 10
    if len(df) < min_len:
        return None

    # ── Calcule tehnice (o singură dată) ─────────────────
    df = df.copy()
    df["rsi"] = calc_rsi(df["close"], RSI_PERIOD)
    df["atr"] = calc_atr(df, ATR_PERIOD)
    ema_f     = calc_ema(df["close"], EMA_FAST)
    ema_s     = calc_ema(df["close"], EMA_SLOW)

    # ── Lumânările relevante ──────────────────────────────
    c0 = df.iloc[-1]   # dreapta
    c1 = df.iloc[-2]   # mijloc (agresiva)
    c2 = df.iloc[-3]   # stânga

    rsi_c1  = c1["rsi"]
    atr_now = c0["atr"]

    if pd.isna(rsi_c1) or pd.isna(atr_now) or atr_now <= 0:
        return None

    avg_body = df["body"].iloc[-(AVG_BODY_PERIOD + 3):-3].mean()
    if pd.isna(avg_body) or avg_body <= 0:
        return None

    # ── Validare lumânare mijloc (agresivă) ───────────────
    mid_body  = c1["body"]
    mid_range = c1["range"]
    if mid_range <= 0:
        return None

    wick_ratio = (mid_range - mid_body) / mid_range
    if not (mid_body >= avg_body * AGGR_FACTOR and wick_ratio <= MAX_WICK_RATIO):
        return None

    current_price = c0["close"]

    # ══════════════════════════════════════════════════════
    #  DETECTARE GAP
    # ══════════════════════════════════════════════════════

    direction = gap_top = gap_bot = None

    if c1["close"] > c1["open"]:
        # ── BULL FVG ──────────────────────────────────────
        # Gap = spațiu între high-ul lui c2 (stânga) și low-ul lui c0 (dreapta)
        # RSI: zona bullish ∈ [RSI_BULL_MIN, RSI_BULL_MAX]
        if not (RSI_BULL_MIN <= rsi_c1 <= RSI_BULL_MAX):
            return None

        direction = "BULL"
        gap_bot   = c2["high"]   # limita inferioară a gap-ului
        gap_top   = c0["low"]    # limita superioară a gap-ului

        if gap_top <= gap_bot:
            return None  # nu există gap real (prețul s-a suprapus)

        # Filtru: gap nu a fost deja mitificat de current_price
        # Dacă prețul a coborât deja sub gap_bot → gap depășit, prea târziu
        if current_price < gap_bot:
            return None

    elif c1["close"] < c1["open"]:
        # ── BEAR FVG ──────────────────────────────────────
        # Gap = spațiu între high-ul lui c0 (dreapta) și low-ul lui c2 (stânga)
        # RSI: zona bearish ∈ [100 - RSI_BULL_MAX, 100 - RSI_BULL_MIN]
        rsi_bear_min = 100.0 - RSI_BULL_MAX
        rsi_bear_max = 100.0 - RSI_BULL_MIN
        if not (rsi_bear_min <= rsi_c1 <= rsi_bear_max):
            return None

        direction = "BEAR"
        gap_bot   = c0["high"]   # limita inferioară (high-ul lumânării drepte)
        gap_top   = c2["low"]    # limita superioară (low-ul lumânării stângi)

        if gap_top <= gap_bot:
            return None  # nu există gap real

        # Filtru: gap nu a fost deja mitificat
        # Dacă prețul a urcat deja peste gap_top → gap depășit
        if current_price > gap_top:
            return None

    else:
        return None  # doji — nu e agresivă

    # ══════════════════════════════════════════════════════
    #  FILTRE GAP
    # ══════════════════════════════════════════════════════

    gap_height = gap_top - gap_bot

    # Filtru 1: gap% minim față de prețul curent
    gap_pct = gap_height / current_price
    if gap_pct < MIN_GAP_PCT:
        return None

    # Filtru 2: gap relativ la ATR (volatilitate)
    if MIN_GAP_ATR_MULT > 0:
        if gap_height / atr_now < MIN_GAP_ATR_MULT:
            return None

    # ══════════════════════════════════════════════════════
    #  FILTRE EMA + SUPRAEXTINDERE
    # ══════════════════════════════════════════════════════

    ema_ok, _reason, ef_val, es_val, slope_frac = _check_ema_filters(
        df, direction, ema_f, ema_s
    )
    if not ema_ok:
        return None

    ext_ok, _ = _check_overextension(df, avg_body, direction)
    if not ext_ok:
        return None

    # ══════════════════════════════════════════════════════
    #  CALCUL ENTRY — ENTRY_FILL_RATIO
    # ══════════════════════════════════════════════════════
    #
    # BULL:
    #   RATIO=0.0 → entry = gap_top  (intrare la marginea superioară — agresiv)
    #   RATIO=0.5 → entry = mid-gap  (recomandat backtest)
    #   RATIO=1.0 → entry = gap_bot  (intrare la marginea inferioară — conservator)
    #
    # BEAR:
    #   RATIO=0.0 → entry = gap_top  (retest la plafonul gap-ului — conservator)
    #   RATIO=0.5 → entry = mid-gap  (recomandat backtest)
    #   RATIO=1.0 → entry = gap_bot  (intrare imediat sub gap — agresiv)
    #
    # Formula BULL și BEAR sunt IDENTICE deoarece:
    #   - BULL: gap_top > gap_bot → entry scade pe măsură ce RATIO crește
    #   - BEAR: gap_top > gap_bot → entry scade pe măsură ce RATIO crește
    #     → la RATIO=0 intri la gap_top (plafon BEAR = SHORT agresiv), CORECT

    entry = gap_top - (gap_top - gap_bot) * ENTRY_FILL_RATIO

    # ── Candle time cu fallback ───────────────────────────
    try:
        candle_time = c0.name  # pd.Timestamp din index
        if pd.isna(candle_time):
            candle_time = "N/A"
    except Exception:
        candle_time = "N/A"

    return FVGSetup(
        symbol      = symbol,
        direction   = direction,
        entry       = entry,
        gap_top     = gap_top,
        gap_bot     = gap_bot,
        gap_height  = gap_height,
        rsi         = round(float(rsi_c1), 1),
        ema_fast    = round(float(ef_val), 6),
        ema_slow    = round(float(es_val), 6),
        slope_fast  = round(float(slope_frac) * 100, 3),  # stocat în %
        atr         = round(float(atr_now), 6),
        candle_time = candle_time,
    )
