"""
═══════════════════════════════════════════════════════════
  FVG DETECTOR — v10 (după backtest v4)
═══════════════════════════════════════════════════════════
Modificări față de v9:
  ✓ Filtru NOU: MIN_GAP_ATR_MULT — gap relativ la volatilitate (ATR)
  ✓ Filtru NOU: RSI_BULL_MAX (overbought protection)
  ✓ Logică entry NOUĂ: ENTRY_FILL_RATIO (mid-gap în loc de gap_top)
  ✓ Câmpuri noi: gap_top, gap_bot, atr (transmise la guardian via state)
  ✓ SL/TP cosmetic eliminat — Guardian gestionează exit dinamic
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
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
    Setup FVG detectat. Conține TOATE info-urile necesare pentru:
    - Plasarea ordinului LIMIT (entry)
    - Tracking-ul de către Guardian (gap_top, gap_bot, atr)
    """
    symbol:      str
    direction:   str
    entry:       float           # Preț LIMIT (calculat din ENTRY_FILL_RATIO)
    gap_top:     float           # Vârful gap-ului (referință Guardian)
    gap_bot:     float           # Baza gap-ului (referință Guardian)
    gap_height:  float
    rsi:         float
    ema_fast:    float
    ema_slow:    float
    slope_fast:  float
    atr:         float           # NOU — pentru context Guardian
    candle_time: pd.Timestamp


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
    """ATR (Average True Range) — pentru filtru gap relativ la volatilitate."""
    high_low   = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close  = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()


def prepare_df(raw_klines: list) -> pd.DataFrame:
    df = pd.DataFrame(raw_klines, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["body"]  = abs(df["close"] - df["open"])
    df["range"] = df["high"] - df["low"]
    return df


def _check_ema_filters(df: pd.DataFrame, direction: str):
    ema_f = calc_ema(df["close"], EMA_FAST)
    ema_s = calc_ema(df["close"], EMA_SLOW)

    ef_now  = ema_f.iloc[-1]
    ef_prev = ema_f.iloc[-(EMA_SLOPE_BARS + 1)]
    es_now  = ema_s.iloc[-1]
    es_prev = ema_s.iloc[-(EMA_SLOPE_BARS + 1)]

    if pd.isna(ef_now) or pd.isna(es_now) or pd.isna(ef_prev) or pd.isna(es_prev):
        return False, "EMA insuficient calculata", 0, 0, 0

    slope_fast = (ef_now - ef_prev) / ef_prev
    slope_slow = (es_now - es_prev) / es_prev

    if direction == "BULL":
        if slope_fast <= 0 or slope_slow <= 0:
            return False, "EMA descrescatoare", ef_now, es_now, slope_fast
        if ef_now < es_now:
            return False, "EMA50 sub EMA100 in BULL", ef_now, es_now, slope_fast
    else:
        if slope_fast >= 0 or slope_slow >= 0:
            return False, "EMA crescatoare", ef_now, es_now, slope_fast
        if ef_now > es_now:
            return False, "EMA50 deasupra EMA100 in BEAR", ef_now, es_now, slope_fast

    if abs(slope_fast) < EMA_MIN_SLOPE:
        return False, f"Panta EMA50 prea mica ({abs(slope_fast):.4f})", ef_now, es_now, slope_fast
    if abs(slope_slow) < EMA_MIN_SLOPE * 0.5:
        return False, f"Panta EMA100 prea mica ({abs(slope_slow):.4f})", ef_now, es_now, slope_fast

    if abs(slope_slow) > 0:
        ratio = abs(slope_fast) / abs(slope_slow)
        if ratio < EMA_PARALLEL_MIN or ratio > EMA_PARALLEL_MAX:
            return False, f"EMA nu sunt paralele (ratio={ratio:.2f})", ef_now, es_now, slope_fast

    return True, "OK", ef_now, es_now, slope_fast


def _check_overextension(df: pd.DataFrame, avg_body: float, direction: str):
    consec = 0
    for i in range(3, 3 + MAX_CONSEC_AGGR + 2):
        if i >= len(df):
            break
        candle = df.iloc[-i]
        body   = candle["body"]
        if direction == "BULL" and candle["close"] > candle["open"] and body >= avg_body * AGGR_FACTOR:
            consec += 1
        elif direction == "BEAR" and candle["close"] < candle["open"] and body >= avg_body * AGGR_FACTOR:
            consec += 1
        else:
            break

    if consec >= MAX_CONSEC_AGGR:
        return False, f"Supraextindere: {consec} lumanari agresive consecutive"
    return True, "OK"


def detect_fvg(symbol: str, df: pd.DataFrame) -> Optional[FVGSetup]:
    min_len = max(AVG_BODY_PERIOD, RSI_PERIOD * 3, EMA_SLOW + EMA_SLOPE_BARS, ATR_PERIOD * 3) + 10
    if len(df) < min_len:
        return None

    df = df.copy()
    df["rsi"] = calc_rsi(df["close"], RSI_PERIOD)
    df["atr"] = calc_atr(df, ATR_PERIOD)

    c0 = df.iloc[-1]
    c1 = df.iloc[-2]
    c2 = df.iloc[-3]

    rsi_c1 = c1["rsi"]
    atr_now = c0["atr"]
    if pd.isna(rsi_c1) or pd.isna(atr_now) or atr_now <= 0:
        return None

    avg_body = df["body"].iloc[-(AVG_BODY_PERIOD + 3):-3].mean()
    if avg_body <= 0:
        return None

    mid_body   = c1["body"]
    mid_range  = c1["range"]
    wick_ratio = (mid_range - mid_body) / mid_range if mid_range > 0 else 1.0

    if not (mid_body >= avg_body * AGGR_FACTOR and wick_ratio <= MAX_WICK_RATIO):
        return None

    current_price = c0["close"]
    direction = gap_top = gap_bot = None

    if c1["close"] > c1["open"]:
        # ── BULL FVG ──
        # RSI bands: BULL ∈ [RSI_BULL_MIN, RSI_BULL_MAX]
        if rsi_c1 < RSI_BULL_MIN or rsi_c1 > RSI_BULL_MAX:
            return None
        direction = "BULL"
        gap_bot   = c2["high"]   # baza gap (mai jos)
        gap_top   = c0["low"]    # vârf gap (mai sus)
        if gap_top <= gap_bot:
            return None

    elif c1["close"] < c1["open"]:
        # ── BEAR FVG ──
        # Pentru BEAR: simetric → RSI ∈ [100-RSI_BULL_MAX, 100-RSI_BULL_MIN]
        rsi_bear_min = 100 - RSI_BULL_MAX
        rsi_bear_max = 100 - RSI_BULL_MIN
        if rsi_c1 < rsi_bear_min or rsi_c1 > rsi_bear_max:
            return None
        direction = "BEAR"
        # Pentru BEAR: gap se inversează (vârf sus, bază jos pentru convenție comună)
        gap_top   = c2["low"]    # vârf gap (mai sus pentru BEAR e c2.low)
        gap_bot   = c0["high"]   # bază gap (mai jos pentru BEAR e c0.high)
        if gap_top <= gap_bot:
            return None
    else:
        return None

    # ── FILTRU GAP minim (% din preț) ──
    gap_height = gap_top - gap_bot
    gap_pct    = gap_height / current_price
    if gap_pct < MIN_GAP_PCT:
        return None

    # ── FILTRU NOU: GAP minim relativ la ATR ──
    if MIN_GAP_ATR_MULT > 0:
        gap_atr_mult = gap_height / atr_now
        if gap_atr_mult < MIN_GAP_ATR_MULT:
            return None

    # ── FILTRE EMA ──
    ema_ok, _, ef_val, es_val, slope_f = _check_ema_filters(df, direction)
    if not ema_ok:
        return None

    # ── FILTRU SUPRAEXTINDERE ──
    ext_ok, _ = _check_overextension(df, avg_body, direction)
    if not ext_ok:
        return None

    # ── CALCUL ENTRY cu ENTRY_FILL_RATIO ──
    # ENTRY_FILL_RATIO=0.0 → entry la gap_top (BULL) / gap_bot (BEAR) — fill agresiv
    # ENTRY_FILL_RATIO=0.5 → entry la mid-gap (recomandat din backtest)
    # ENTRY_FILL_RATIO=1.0 → entry la gap_bot (BULL) / gap_top (BEAR) — fill foarte conservator
    if direction == "BULL":
        entry = gap_top - (gap_top - gap_bot) * ENTRY_FILL_RATIO
    else:
        entry = gap_bot + (gap_top - gap_bot) * ENTRY_FILL_RATIO

    return FVGSetup(
        symbol      = symbol,
        direction   = direction,
        entry       = entry,
        gap_top     = gap_top,
        gap_bot     = gap_bot,
        gap_height  = gap_height,
        rsi         = round(rsi_c1, 1),
        ema_fast    = round(ef_val, 6),
        ema_slow    = round(es_val, 6),
        slope_fast  = round(slope_f * 100, 3),
        atr         = round(atr_now, 6),
        candle_time = c0.name
    )
