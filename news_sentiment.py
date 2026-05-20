"""Bot 2 — news & sentiment engine (system 1 of 2).

Produces a per-ticker sentiment signal from recent headlines. Designed so the
news source is pluggable: a free yfinance provider works out of the box, and a
paid provider (Finnhub / Alpha Vantage / Polygon) can be dropped in later for
deeper history and coverage.

HONESTY NOTE — this is a *live / forward* signal, not a backtestable one yet.
Free sources only return recent headlines, so you cannot honestly backtest a
sentiment strategy without point-in-time historical news (a paid dataset).
Sentiment signals are also weak and decay fast; treat this as a *tilt* on top of
the systematic core (Strategy C), not a standalone alpha, until proven on
point-in-time data.

The companion system (industry / cross-company linkage — e.g. "who benefits from
the SpaceX IPO") lives in industry_map.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import yfinance as yf
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_ANALYZER = SentimentIntensityAnalyzer()


@dataclass
class Headline:
    ticker: str
    title: str
    publisher: str
    published: float  # epoch seconds (0 if unknown)
    url: str


# --- News providers (pluggable) ----------------------------------------------

def fetch_headlines_yfinance(ticker: str, limit: int = 20) -> list[Headline]:
    """Free provider: recent headlines via yfinance. Recent-only (no history)."""
    out: list[Headline] = []
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception as e:
        print(f"  news fetch failed for {ticker}: {e}")
        return out
    for item in raw[:limit]:
        # yfinance has shifted the news schema across versions; handle both.
        content = item.get("content", item)
        title = content.get("title") or item.get("title") or ""
        if not title:
            continue
        provider = content.get("provider") or {}
        publisher = (provider.get("displayName") if isinstance(provider, dict)
                     else None) or item.get("publisher") or "?"
        published = item.get("providerPublishTime", 0) or 0
        url = (content.get("canonicalUrl") or {}).get("url", "") if isinstance(
            content.get("canonicalUrl"), dict) else item.get("link", "")
        out.append(Headline(ticker, title, publisher, float(published), url))
    return out


PROVIDERS = {"yfinance": fetch_headlines_yfinance}


# --- Sentiment scoring -------------------------------------------------------

def score_text(text: str) -> float:
    """VADER compound score in [-1, 1]."""
    return _ANALYZER.polarity_scores(text)["compound"]


def ticker_sentiment(ticker: str, provider: str = "yfinance", limit: int = 20) -> dict:
    """Aggregate sentiment for one ticker from recent headlines."""
    heads = PROVIDERS[provider](ticker, limit=limit)
    scored = [(h, score_text(h.title)) for h in heads]
    if not scored:
        return {"ticker": ticker, "n": 0, "mean": 0.0, "pos": 0, "neg": 0,
                "headlines": []}
    scores = [s for _, s in scored]
    return {
        "ticker": ticker,
        "n": len(scores),
        "mean": sum(scores) / len(scores),
        "pos": sum(1 for s in scores if s > 0.2),
        "neg": sum(1 for s in scores if s < -0.2),
        "headlines": [(h.title, round(s, 3)) for h, s in scored],
    }


def universe_sentiment(tickers, provider: str = "yfinance", limit: int = 20,
                       pause: float = 0.3) -> dict:
    """Sentiment for many tickers -> {ticker: mean_score}. Polite pause between calls."""
    out = {}
    for t in tickers:
        out[t] = ticker_sentiment(t, provider=provider, limit=limit)
        time.sleep(pause)
    return out


if __name__ == "__main__":
    import sys

    tickers = sys.argv[1:] or ["NVDA", "TSLA", "PLTR", "AAPL"]
    print(f"Sentiment from recent headlines (provider=yfinance)\n{'-' * 60}")
    for t in tickers:
        s = ticker_sentiment(t)
        tag = "POS" if s["mean"] > 0.1 else "NEG" if s["mean"] < -0.1 else "  ."
        print(f"[{tag}] {t:<6} mean={s['mean']:+.3f}  n={s['n']:<3} "
              f"(+{s['pos']}/-{s['neg']})")
        for title, sc in s["headlines"][:3]:
            print(f"      {sc:+.2f}  {title[:70]}")
