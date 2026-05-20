"""Bot 2 — industry & cross-company linkage (system 2 of 2).

Answers "who else moves when X moves?" — the SpaceX-IPO question: SpaceX isn't
public, but a basket of public names (launch, satellite, defense, comms) tends
to react to space-sector catalysts. This maps those relationships two ways:

  1. THEMATIC  — a hand-seeded map of themes -> public tickers (extend freely).
  2. STATISTICAL — data-driven peers via return correlation (no opinion needed;
                   surfaces names that actually co-move, including non-obvious ones).

Combine with news_sentiment.py: when a catalyst hits a theme (e.g. a SpaceX IPO),
score sentiment across the theme's members and the correlated peers to find who
benefits — then let Strategy C's trend filter decide if/when to actually buy.

NOTE: thematic maps are a starting opinion, not ground truth. Statistical peers
are descriptive of the past window. Neither is a trade signal on its own — they
generate a *candidate set* for the systematic core to vet.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

# Hand-seeded thematic baskets (public tickers). Extend as you research.
THEMES = {
    'space':       ['RKLB', 'ASTS', 'LMT', 'NOC', 'BA', 'HII', 'PL', 'IRDM', 'GSAT'],
    'ai_compute':  ['NVDA', 'AMD', 'AVGO', 'SMCI', 'MU', 'MRVL', 'ARM', 'TSM'],
    'ai_software': ['PLTR', 'MSFT', 'GOOGL', 'META', 'CRM', 'NOW', 'SNOW'],
    'ev':          ['TSLA', 'RIVN', 'GM', 'F', 'LCID'],
    'semis_equip': ['AMAT', 'LRCX', 'KLAC', 'ASML'],
    'cloud':       ['AMZN', 'MSFT', 'GOOGL', 'ORCL', 'SNOW'],
    'nuclear_power': ['CEG', 'VST', 'SMR', 'OKLO', 'GEV'],
}


def themes_for(ticker: str) -> list[str]:
    return [name for name, members in THEMES.items() if ticker in members]


def theme_members(theme: str) -> list[str]:
    return THEMES.get(theme, [])


def load_panel(tickers, period="2y", interval="1d") -> pd.DataFrame:
    data = yf.download(sorted(set(tickers)), period=period, interval=interval,
                       auto_adjust=True, progress=False)
    close = data['Close'] if isinstance(data.columns, pd.MultiIndex) else data[['Close']]
    return close.sort_index()


def correlated_peers(close: pd.DataFrame, ticker: str, top_k=8, window=252):
    """Top names whose daily returns co-move with `ticker` over the window."""
    if ticker not in close.columns:
        return []
    rets = close.pct_change().tail(window)
    corr = rets.corrwith(rets[ticker]).drop(labels=[ticker], errors='ignore').dropna()
    return [(t, round(float(c), 3)) for t, c in
            corr.sort_values(ascending=False).head(top_k).items()]


def beneficiaries(close: pd.DataFrame, theme: str, top_k=8):
    """Candidate beneficiaries of a theme catalyst: the seeded members plus the
    names most correlated with the theme's average move."""
    members = [t for t in theme_members(theme) if t in close.columns]
    if not members:
        return {'members': theme_members(theme), 'correlated': []}
    theme_ret = close[members].pct_change().mean(axis=1).tail(252)
    others = [c for c in close.columns if c not in members]
    corr = (close[others].pct_change().tail(252)
            .apply(lambda col: col.corr(theme_ret)).dropna())
    return {
        'members': members,
        'correlated': [(t, round(float(c), 3))
                       for t, c in corr.sort_values(ascending=False).head(top_k).items()],
    }


if __name__ == "__main__":
    # Demo: the SpaceX-IPO question. Load space basket + a broad set to find
    # which names co-move with the space theme.
    from strategy_c import BROAD_UNIVERSE

    extra = sorted({t for members in THEMES.values() for t in members})
    print("Loading 2y panel for theme + broad universe...")
    close = load_panel(BROAD_UNIVERSE + extra)

    print(f"\nThemes for NVDA: {themes_for('NVDA')}")
    print(f"\nStatistical peers of NVDA (last 1y return correlation):")
    for t, c in correlated_peers(close, 'NVDA'):
        print(f"   {t:<6} {c:+.2f}")

    print(f"\n'space' theme - candidate beneficiaries of a SpaceX-IPO catalyst:")
    b = beneficiaries(close, 'space')
    print(f"   seeded members: {', '.join(b['members'])}")
    print(f"   most correlated outside the basket:")
    for t, c in b['correlated']:
        print(f"   {t:<6} {c:+.2f}")
