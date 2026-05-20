"""Test 'disaster-only' stops vs the naive trailing stop and the baseline.

The naive 20% trailing stop whipsawed normal winners. These variants try to fire
ONLY on real disasters (a confirmed 200d trend break and/or a very large drop),
so healthy pullbacks in uptrends are ignored. Same daily engine throughout.
"""
from metrics import compute_metrics
from plot_equity import plot_equity
from strategy_c import (BROAD_UNIVERSE, MARKET, RISK_OFF_TICKERS, _rebased,
                        load_prices, run_with_overlay)

CONFIGS = {
    "Baseline (no stop)":        dict(),
    "Naive trailing 20%":        dict(trailing_stop=0.20),
    "Wide trailing 40%":         dict(trailing_stop=0.40),
    "200d-break only":           dict(stop_below_sma=True),
    "200d-break AND -35%":       dict(stop_below_sma=True, trailing_stop=0.35,
                                      require_both=True),
}

if __name__ == "__main__":
    print("Loading data...")
    close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS))
    start = close[MARKET].dropna().index[200]

    print(f"\nWindow: {start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Stop variant':<24}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>8}{'Calmar':>8}{'turn':>7}")
    print("-" * 62)
    curves = {}
    for name, kw in CONFIGS.items():
        eq, stats = run_with_overlay(close, BROAD_UNIVERSE, top_n=10,
                                     risk_off='dynamic', **kw)
        eqw = _rebased(eq, start)
        curves[name] = eqw
        m = compute_metrics(eqw)
        print(f"{name:<24}{m['cagr']:>6.1f}%{m['sharpe']:>8.2f}"
              f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}{stats['avg_turnover']:>7.2f}")

    spy = _rebased(close[MARKET], start)
    m = compute_metrics(spy)
    print(f"{'SPY buy & hold':<24}{m['cagr']:>6.1f}%{m['sharpe']:>8.2f}"
          f"{m['max_drawdown']:>7.0f}%{m['calmar']:>8.2f}{'-':>7}")

    plot_equity(curves["200d-break AND -35%"], spy,
                title=(f"Disaster-only stop (200d break AND -35%) vs baseline & SPY\n"
                       f"{start.date()} -> {close.index[-1].date()}"),
                out_path="strategy_c_smartstop.png",
                extras=[(curves["Baseline (no stop)"], "Baseline (no stop)", "#e8710a")])
