"""A/B test the name-level risk overlay on Strategy C.

Same daily engine throughout (run_with_overlay), toggling each protection so the
comparison is clean: does a trailing stop + per-name / sector caps lift Calmar
(return per unit of drawdown) without gutting return?
"""
from metrics import compute_metrics
from plot_equity import plot_equity
from strategy_c import (BROAD_UNIVERSE, MARKET, RISK_OFF_TICKERS, SECTORS,
                        _rebased, load_prices, run_with_overlay)

CONFIGS = {
    "Baseline (no overlay)": dict(),
    "+ Per-name cap 15%":    dict(max_weight=0.15),
    "+ Trailing stop 20%":   dict(trailing_stop=0.20),
    "+ Sector cap 30%":      dict(sector_map=SECTORS, sector_cap=0.30),
    "+ All overlays":        dict(max_weight=0.15, trailing_stop=0.20,
                                  sector_map=SECTORS, sector_cap=0.30),
}

if __name__ == "__main__":
    print("Loading data...")
    close = load_prices(BROAD_UNIVERSE + list(RISK_OFF_TICKERS))
    start = close[MARKET].dropna().index[200]

    print(f"\nWindow: {start.date()} -> {close.index[-1].date()}\n")
    print(f"{'Config':<24}{'CAGR':>7}{'Sharpe':>8}{'maxDD':>8}{'Calmar':>8}{'turn':>7}")
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

    plot_equity(curves["+ All overlays"], spy,
                title=(f"Strategy C + risk overlay (stop + caps) vs baseline & SPY\n"
                       f"{start.date()} -> {close.index[-1].date()}"),
                out_path="strategy_c_overlay.png",
                extras=[(curves["Baseline (no overlay)"], "Baseline (no overlay)", "#e8710a")])
