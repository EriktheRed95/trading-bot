"""Shared technical indicators. Single source of truth for RSI / MACD / BBands / ADX."""
import numpy as np
import pandas as pd

_EPS = 1e-9


def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / (avg_loss + _EPS)
    return (100 - (100 / (1 + rs))).fillna(50)


def calculate_macd(series, fast=12, slow=26, signal=9):
    k = series.ewm(span=fast, adjust=False).mean()
    d = series.ewm(span=slow, adjust=False).mean()
    macd = k - d
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig


def calculate_bbands(series, length=20, std=2):
    sma = series.rolling(window=length).mean()
    std_dev = series.rolling(window=length).std()
    return sma + std * std_dev, sma - std * std_dev


def calculate_adx(df, period=14):
    df = df.copy()
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = (df['high'] - df['close'].shift(1)).abs()
    df['l-pc'] = (df['low'] - df['close'].shift(1)).abs()
    tr = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    up = df['high'] - df['high'].shift(1)
    down = df['low'].shift(1) - df['low']
    plus_dm = np.where((up > down) & (up > 0), up, 0)
    minus_dm = np.where((down > up) & (down > 0), down, 0)
    alpha = 1 / period
    tr_s = tr.ewm(alpha=alpha, adjust=False).mean().replace(0, _EPS)
    plus_dm_s = pd.Series(plus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean()
    minus_dm_s = pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * (plus_dm_s / tr_s)
    minus_di = 100 * (minus_dm_s / tr_s)
    denom = (plus_di + minus_di).replace(0, _EPS)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(20)
