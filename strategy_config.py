"""Shared strategy parameters. Both backtest_engine.py and main.py read from here
so live and backtest behavior stay in sync."""

# Per-identity entry/exit thresholds.
THRESHOLDS = {
    'ROCKET':   {'entry_long': 3,   'exit_long': -4, 'rsi_take': 80},
    'GRINDER':  {'entry_long': 4.5, 'exit_long': -5, 'entry_short': -4.5, 'exit_short': 5},
    'FORTRESS': {'entry_long': 5,   'exit_long': -2},
    # Default used when an identity is unknown (e.g. forex / crypto in main.py).
    'DEFAULT':  {'entry_long': 4,   'exit_long': -4},
}

# Classifier cutoffs.
ROCKET_VOL_MIN = 35.0   # annualized %
ROCKET_ADX_MIN = 25.0
GRINDER_VOL_MIN = 20.0

# Allocation between Core (buy-and-hold) and Satellite (active) buckets.
CORE_ALLOCATION = {'ROCKET': 0.80, 'GRINDER': 0.00, 'FORTRESS': 0.90}

# Execution costs applied to every fill in the backtester.
SLIPPAGE_BPS = 5      # 5 basis points = 0.05% per side
COMMISSION_BPS = 1    # 1 bp commission per side

# Synthetic-options model parameters (acknowledged as approximate).
OPTION_LEVERAGE = 3.0
OPTION_THETA_PER_HOUR = 0.00007  # ~0.17%/day. Real short-dated calls bleed faster.
OPTION_BLOWOUT_THRESHOLD = 0.10  # position closed if value drops below 10% of entry.
