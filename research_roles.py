"""Five deterministic research roles. Pure functions: no network, clock or state.

Each role reads a completed-session OHLCV panel (and, for depth, a real public
order book) and returns one finding per symbol. A finding always carries:

    role, symbol, session, observed_at, session_age_hours, evidence,
    observations_used, proposed_action, abstained, abstention_reason, source

There is deliberately NO confidence score. A score invented from a threshold
distance reads as information and carries none. What a reader gets instead is
the measurement, how many completed observations it came from, how old the
observation is, and the explicit reason when a role declines to say anything.

Nothing here forecasts a return, ranks an instrument for purchase, or alters the
frozen strategy. Every action a role can propose is advisory text.

THRESHOLDS are fixed in advance and stated here so they can be argued with. They
were not searched against outcomes, and none of them is a strategy parameter:
the core's own windows, gates and sizing are untouched by this module.
"""
import math
import numpy as np
import pandas as pd

ROLE_ACTIVITY = 'unusual-activity'
ROLE_ENTRY = 'entry-quality'
ROLE_FILTER = 'signal-noise'
ROLE_LIQUIDITY = 'liquidity-depth'
ROLE_SUPERVISOR = 'supervisor'

ROLE_TITLES = {
    ROLE_ACTIVITY: 'Unusual volume and activity',
    ROLE_ENTRY: 'Entry quality and timing',
    ROLE_FILTER: 'Independent signal and noise filter',
    ROLE_LIQUIDITY: 'Liquidity and visible depth',
    ROLE_SUPERVISOR: 'Supervisor report',
}

# --- Role 1: unusual activity -------------------------------------------------
VOLUME_WINDOW = 60          # completed sessions compared against, excluding the observed one
VOLUME_HIGH_RATIO = 3.0     # observed volume at or above this multiple of its own median
VOLUME_THIN_RATIO = 1 / 3   # observed volume at or below this multiple of its own median

# --- Role 2: entry quality ----------------------------------------------------
RANGE_WINDOW = 20           # sessions used for the high/low range position
TREND_WINDOW = 200          # same length as the core's own trend measure, read-only
EXTENDED_RANGE_PCT = 90.0   # close sitting in the top decile of its 20-session range
EXTENDED_TREND_PCT = 10.0   # and more than this far above its 200-session average

# --- Role 3: signal and noise -------------------------------------------------
INTEGRITY_WINDOW = 252
INTEGRITY_MIN_OBS = 60
STALE_FRACTION_MAX = 0.10       # share of sessions printing exactly zero change
DEGENERATE_OPEN_MAX = 0.10      # share of sessions whose open equals the prior close
EXTREME_MOVE = 0.50             # single-session move treated as a data fault
PRICE_FLOOR = 1.00
CROSS_CHECK_TOLERANCE_PCT = 0.5  # independent fetch vs the core's own price

# --- Role 4: liquidity and depth ----------------------------------------------
DOLLAR_VOLUME_WINDOW = 60
DOLLAR_VOLUME_FLOOR = 20_000_000.0   # advisory size-review floor for this megacap pool
DEPTH_BANDS_BPS = (25, 100)

NO_ACTION = 'No action proposed'
EQUITY_SPREAD_UNAVAILABLE = ('Unavailable: no equity quote or order-book source is wired into '
                             'this layer, so no spread or depth figure is reported for listed names.')
EQUITY_DEPTH_UNAVAILABLE = ('Unavailable: order-book depth for listed names requires a quote feed '
                            'this layer does not have. Traded dollar volume is history, not depth, '
                            'and is never converted into one.')


def _finding(role, symbol, session, observed_at, session_age_hours, *, evidence=None,
             observations_used=0, proposed_action=None, abstention_reason=None,
             source=None, verdict=None):
    return {'role': role, 'symbol': symbol, 'session': session, 'observed_at': observed_at,
            'session_age_hours': session_age_hours, 'evidence': evidence or {},
            'observations_used': int(observations_used), 'verdict': verdict,
            'proposed_action': None if abstention_reason else (proposed_action or NO_ACTION),
            'abstained': bool(abstention_reason), 'abstention_reason': abstention_reason,
            'source': source}


def _window(frame, symbol, session, length):
    """The last `length` completed rows up to `session`, or a refusal reason.

    Fail closed: a missing value inside the window, or no bar for the observed
    session, abstains. Nothing is forward filled and no shorter window is
    silently substituted.
    """
    if symbol not in frame.columns:
        return None, 'symbol is not present in the research intake snapshot'
    series = frame[symbol].loc[:pd.Timestamp(session)]
    if series.empty:
        return None, 'no completed observations in the research intake snapshot'
    if pd.Timestamp(series.index[-1]) != pd.Timestamp(session):
        return None, (f'no bar for the observed session {session}; latest available is '
                      f'{pd.Timestamp(series.index[-1]).date()}')
    if len(series) < length:
        return None, f'needs {length} completed observations, has {len(series)}'
    tail = series.iloc[-length:]
    missing = int(tail.isna().sum())
    if missing:
        return None, f'{missing} of the last {length} completed observations are missing'
    return tail.astype(float), None


def unusual_activity(panel, symbols, session, observed_at, session_age_hours, *,
                     window=VOLUME_WINDOW, source=None):
    """How heavy was the latest completed session against this name's own history?

    The comparison window EXCLUDES the observed session, so the reading compares
    a bar against strictly prior data and cannot borrow from itself.
    """
    out = []
    for symbol in symbols:
        tail, reason = _window(panel['Volume'], symbol, session, window + 1)
        if reason:
            out.append(_finding(ROLE_ACTIVITY, symbol, session, observed_at, session_age_hours,
                                abstention_reason=reason, source=source))
            continue
        latest = float(tail.iloc[-1])
        prior = tail.iloc[:-1]
        median = float(prior.median())
        if not math.isfinite(latest) or latest <= 0:
            out.append(_finding(ROLE_ACTIVITY, symbol, session, observed_at, session_age_hours,
                                abstention_reason='the observed session reports zero or invalid volume',
                                source=source))
            continue
        if not math.isfinite(median) or median <= 0:
            out.append(_finding(ROLE_ACTIVITY, symbol, session, observed_at, session_age_hours,
                                abstention_reason='the trailing median volume is zero or invalid',
                                source=source))
            continue
        ratio = latest / median
        heavier = int((prior >= latest).sum())
        evidence = {'session_volume': latest, 'trailing_median_volume': median,
                    'ratio_to_median': ratio, 'comparison_sessions': int(len(prior)),
                    'prior_sessions_at_or_above': heavier,
                    'high_ratio_threshold': VOLUME_HIGH_RATIO,
                    'thin_ratio_threshold': VOLUME_THIN_RATIO}
        if ratio >= VOLUME_HIGH_RATIO:
            verdict = 'unusually heavy'
            action = (f'Flag for entry review: {symbol} traded {ratio:.1f} times its median volume '
                      f'of the prior {len(prior)} sessions, and only {heavier} of those sessions '
                      f'were as heavy.')
        elif ratio <= VOLUME_THIN_RATIO:
            verdict = 'unusually thin'
            action = (f'Flag for liquidity review: {symbol} traded {ratio:.2f} times its median '
                      f'volume of the prior {len(prior)} sessions.')
        else:
            verdict, action = 'ordinary', None
        out.append(_finding(ROLE_ACTIVITY, symbol, session, observed_at, session_age_hours,
                            evidence=evidence, observations_used=len(tail), verdict=verdict,
                            proposed_action=action, source=source))
    return out


def entry_quality(panel, symbols, session, observed_at, session_age_hours, *, source=None):
    """Where in its own recent range is this name being bought?

    This describes the CURRENT position of the last completed close. It is not a
    prediction, and it does not rank names against each other.
    """
    out = []
    for symbol in symbols:
        close, reason = _window(panel['Close'], symbol, session, TREND_WINDOW + 1)
        if reason:
            out.append(_finding(ROLE_ENTRY, symbol, session, observed_at, session_age_hours,
                                abstention_reason=reason, source=source))
            continue
        high, high_reason = _window(panel['High'], symbol, session, RANGE_WINDOW)
        low, low_reason = _window(panel['Low'], symbol, session, RANGE_WINDOW)
        open_, open_reason = _window(panel['Open'], symbol, session, RANGE_WINDOW)
        bad = high_reason or low_reason or open_reason
        if bad:
            out.append(_finding(ROLE_ENTRY, symbol, session, observed_at, session_age_hours,
                                abstention_reason=f'session range unavailable: {bad}', source=source))
            continue
        last = float(close.iloc[-1])
        average = float(close.iloc[-TREND_WINDOW:].mean())
        if not math.isfinite(average) or average <= 0 or last <= 0:
            out.append(_finding(ROLE_ENTRY, symbol, session, observed_at, session_age_hours,
                                abstention_reason='invalid price or trailing average', source=source))
            continue
        top, bottom = float(high.max()), float(low.min())
        if not (top > bottom):
            out.append(_finding(ROLE_ENTRY, symbol, session, observed_at, session_age_hours,
                                abstention_reason=f'the {RANGE_WINDOW}-session high and low are '
                                                  f'equal, so range position is undefined',
                                source=source))
            continue
        position_pct = (last - bottom) / (top - bottom) * 100
        trend_pct = (last / average - 1) * 100
        prior_close = float(close.iloc[-2])
        gap_pct = (float(open_.iloc[-1]) / prior_close - 1) * 100 if prior_close > 0 else None
        ranges = (high - low).astype(float)
        median_range = float(ranges.median())
        session_range = float(ranges.iloc[-1])
        evidence = {'close': last, 'range_high': top, 'range_low': bottom,
                    'range_position_pct': position_pct, 'range_window': RANGE_WINDOW,
                    'pct_above_trailing_average': trend_pct, 'trend_window': TREND_WINDOW,
                    'overnight_gap_pct': gap_pct, 'session_range': session_range,
                    'median_session_range': median_range,
                    'session_range_vs_median': (session_range / median_range)
                    if median_range > 0 else None,
                    'extended_range_threshold_pct': EXTENDED_RANGE_PCT,
                    'extended_trend_threshold_pct': EXTENDED_TREND_PCT}
        if trend_pct < 0:
            verdict = 'below its trailing average'
            action = (f'Note for the supervisor: {symbol} closed {abs(trend_pct):.1f}% below its '
                      f'{TREND_WINDOW}-session average. The frozen strategy already excludes this case.')
        elif position_pct >= EXTENDED_RANGE_PCT and trend_pct >= EXTENDED_TREND_PCT:
            verdict = 'extended'
            action = (f'Flag for entry review: {symbol} closed in the top '
                      f'{100 - position_pct:.0f}% of its {RANGE_WINDOW}-session range and '
                      f'{trend_pct:.1f}% above its {TREND_WINDOW}-session average.')
        else:
            verdict, action = 'inside its recent range', None
        out.append(_finding(ROLE_ENTRY, symbol, session, observed_at, session_age_hours,
                            evidence=evidence, observations_used=len(close), verdict=verdict,
                            proposed_action=action, source=source))
    return out


def signal_noise(panel, symbols, session, observed_at, session_age_hours, *,
                 core_prices=None, source=None):
    """Is this price series real enough to compute anything on?

    An independent check on the intake's own fetch, using the failure modes this
    repository already found the hard way: stale prints, opens that equal the
    prior close, impossible single-session moves and sub-dollar prices. It also
    cross-checks the independently fetched close against the price the frozen
    core used, and reports a disagreement rather than choosing a winner.
    """
    out = []
    core_prices = core_prices or {}
    for symbol in symbols:
        close, reason = _window(panel['Close'], symbol, session, INTEGRITY_MIN_OBS)
        if reason:
            out.append(_finding(ROLE_FILTER, symbol, session, observed_at, session_age_hours,
                                abstention_reason=reason, source=source))
            continue
        series = panel['Close'][symbol].loc[:pd.Timestamp(session)].iloc[-INTEGRITY_WINDOW:]
        opens = panel['Open'][symbol].reindex(series.index)
        available = series.dropna()
        changes = available.pct_change().dropna()
        if changes.empty:
            out.append(_finding(ROLE_FILTER, symbol, session, observed_at, session_age_hours,
                                abstention_reason='no usable session-to-session changes in the window',
                                source=source))
            continue
        zero_fraction = float((changes.abs() < 1e-12).mean())
        prior = available.shift(1)
        pairs = pd.concat([opens.reindex(available.index), prior], axis=1).dropna()
        degenerate = (float(((pairs.iloc[:, 0] / pairs.iloc[:, 1] - 1).abs() < 1e-12).mean())
                      if len(pairs) else None)
        extreme = int((changes.abs() > EXTREME_MOVE).sum())
        minimum = float(available.min())
        missing = int(series.isna().sum())
        failures = []
        if zero_fraction > STALE_FRACTION_MAX:
            failures.append(f'{zero_fraction:.0%} of sessions print no change at all, above the '
                            f'{STALE_FRACTION_MAX:.0%} limit')
        if degenerate is not None and degenerate > DEGENERATE_OPEN_MAX:
            failures.append(f'{degenerate:.0%} of sessions open exactly at the prior close, above '
                            f'the {DEGENERATE_OPEN_MAX:.0%} limit')
        if extreme:
            failures.append(f'{extreme} session move(s) beyond '
                            f'{EXTREME_MOVE:.0%}, which this repository treats as a data fault')
        if minimum < PRICE_FLOOR:
            failures.append(f'a close of {minimum:.2f} sits under the {PRICE_FLOOR:.2f} floor')
        if missing:
            failures.append(f'{missing} session(s) in the window have no price')
        evidence = {'window_sessions': int(len(series)), 'priced_sessions': int(len(available)),
                    'zero_change_fraction': zero_fraction,
                    'degenerate_open_fraction': degenerate,
                    'extreme_move_count': extreme, 'minimum_close': minimum,
                    'missing_sessions': missing,
                    'stale_fraction_limit': STALE_FRACTION_MAX,
                    'degenerate_open_limit': DEGENERATE_OPEN_MAX,
                    'extreme_move_limit': EXTREME_MOVE, 'price_floor': PRICE_FLOOR}
        core_price = core_prices.get(symbol)
        if core_price is not None and math.isfinite(core_price) and core_price > 0:
            difference = (float(available.iloc[-1]) / float(core_price) - 1) * 100
            evidence['core_snapshot_price'] = float(core_price)
            evidence['research_price'] = float(available.iloc[-1])
            evidence['price_difference_pct'] = difference
            evidence['cross_check_tolerance_pct'] = CROSS_CHECK_TOLERANCE_PCT
            if abs(difference) > CROSS_CHECK_TOLERANCE_PCT:
                failures.append(f'this independent fetch closes {difference:+.2f}% away from the '
                                f'price the strategy used for the same session')
        else:
            evidence['core_snapshot_price'] = None
            evidence['price_difference_pct'] = None
        verdict = 'usable' if not failures else 'suspect'
        action = None if not failures else (
            f'Exclude {symbol} from advisory use this session: ' + '; '.join(failures) + '.')
        out.append(_finding(ROLE_FILTER, symbol, session, observed_at, session_age_hours,
                            evidence={**evidence, 'failures': failures},
                            observations_used=len(available), verdict=verdict,
                            proposed_action=action, source=source))
    return out


def visible_depth(book, bands_bps=DEPTH_BANDS_BPS):
    """Visible resting size within price bands of the mid, from a real book.

    Returns the summed notional per side per band and whether the snapshot's
    levels actually reached the far edge of the band. When they do not, the
    figure is a LOWER BOUND on visible size, which the caller must say out loud.
    """
    bids, asks = book['bids'], book['asks']
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 10_000 if mid > 0 else None
    bands = {}
    for band in bands_bps:
        floor, ceiling = mid * (1 - band / 10_000), mid * (1 + band / 10_000)
        bid_notional = sum(p * s for p, s in bids if p >= floor)
        ask_notional = sum(p * s for p, s in asks if p <= ceiling)
        bands[str(band)] = {
            'bid_usd': bid_notional, 'ask_usd': ask_notional,
            'bid_levels': sum(1 for p, _ in bids if p >= floor),
            'ask_levels': sum(1 for p, _ in asks if p <= ceiling),
            # A band is fully covered only if the snapshot extends past its edge.
            'band_fully_covered': bool(bids[-1][0] < floor and asks[-1][0] > ceiling),
        }
    return {'best_bid': best_bid, 'best_ask': best_ask, 'mid': mid, 'spread_bps': spread_bps,
            'levels_in_snapshot': {'bids': len(bids), 'asks': len(asks)},
            'bands_bps': bands}


def liquidity_depth(panel, symbols, session, observed_at, session_age_hours, *,
                    books=None, source=None, book_source=None):
    """Two separate questions, never conflated.

    For listed names: how many dollars actually traded, from reported volume. A
    traded-dollar history is not order-book depth; no depth or spread is reported
    for listed names because no quote source is wired in.

    For crypto references: real visible book depth from a public exchange
    snapshot, labelled as resting orders that can be cancelled.
    """
    out = []
    for symbol in symbols:
        close, close_reason = _window(panel['Close'], symbol, session, DOLLAR_VOLUME_WINDOW)
        volume, volume_reason = _window(panel['Volume'], symbol, session, DOLLAR_VOLUME_WINDOW)
        reason = close_reason or volume_reason
        if reason:
            out.append(_finding(ROLE_LIQUIDITY, symbol, session, observed_at, session_age_hours,
                                abstention_reason=reason, source=source))
            continue
        dollars = (close * volume).astype(float)
        median = float(dollars.median())
        if not math.isfinite(median) or median <= 0:
            out.append(_finding(ROLE_LIQUIDITY, symbol, session, observed_at, session_age_hours,
                                abstention_reason='median traded dollar volume is zero or invalid',
                                source=source))
            continue
        evidence = {'median_dollar_volume': median, 'window_sessions': DOLLAR_VOLUME_WINDOW,
                    'session_dollar_volume': float(dollars.iloc[-1]),
                    'size_review_floor': DOLLAR_VOLUME_FLOOR,
                    'quoted_spread': EQUITY_SPREAD_UNAVAILABLE,
                    'visible_book_depth': EQUITY_DEPTH_UNAVAILABLE,
                    'measure': 'dollars actually traded per session (close x reported volume)'}
        if median < DOLLAR_VOLUME_FLOOR:
            verdict = 'thin for this pool'
            action = (f'Flag for size review: {symbol} traded a median of '
                      f'${median / 1e6:.1f}M per session over {DOLLAR_VOLUME_WINDOW} sessions, '
                      f'under the ${DOLLAR_VOLUME_FLOOR / 1e6:.0f}M advisory floor.')
        else:
            verdict, action = 'ample traded volume', None
        out.append(_finding(ROLE_LIQUIDITY, symbol, session, observed_at, session_age_hours,
                            evidence=evidence, observations_used=DOLLAR_VOLUME_WINDOW,
                            verdict=verdict, proposed_action=action, source=source))
    for product, book in sorted((books or {}).items()):
        if not isinstance(book, dict) or book.get('error') or not book.get('bids'):
            out.append(_finding(ROLE_LIQUIDITY, product, session, observed_at, None,
                                abstention_reason=(book or {}).get('error', 'no order book snapshot'),
                                source=book_source, evidence={'depth_policy': (book or {}).get('depth_policy')}))
            continue
        measured = visible_depth(book)
        partial = [band for band, values in measured['bands_bps'].items()
                   if not values['band_fully_covered']]
        evidence = {**measured, 'book_time': book.get('book_time'),
                    'snapshot_fetched_at': book.get('fetched_at'),
                    'snapshot_cached': book.get('cached'),
                    'snapshot_age_minutes': book.get('age_minutes'),
                    'depth_policy': book.get('depth_policy'),
                    'lower_bound_bands_bps': partial}
        tight = measured['bands_bps'][str(DEPTH_BANDS_BPS[0])]
        verdict = 'visible book measured'
        action = (f'Observation only: {product} shows ${tight["bid_usd"]:,.0f} of visible bids and '
                  f'${tight["ask_usd"]:,.0f} of visible asks within {DEPTH_BANDS_BPS[0]} basis '
                  f'points of the mid, at a {measured["spread_bps"]:.1f} basis point spread. '
                  f'Resting orders can be cancelled; this is not an executable guarantee.')
        out.append(_finding(ROLE_LIQUIDITY, product, session, observed_at, None,
                            evidence=evidence, observations_used=1, verdict=verdict,
                            proposed_action=action, source=book_source))
    return out


def stamp_findings(findings, observed_at, session_age_hours):
    """Assign the decision-ready observation time to every reading.

    The roles above are pure and read no clock, so their readings arrive
    unstamped. The caller stamps them once, AFTER the data was fetched and the
    measurements computed, so a reading's timestamp is the instant the reading
    actually existed rather than the instant its cycle began. Those differ by the
    whole fetch, and a fill guard reads the difference.
    """
    for finding in findings:
        finding['observed_at'] = observed_at
        finding['session_age_hours'] = session_age_hours
    return findings


def deferral_symbols(findings):
    """Symbols any role asked to keep out of a new entry this session.

    Deterministic and stated in one place so the shadow book's rule is auditable:
    an unusually heavy or thin session, an extended close, a suspect series or a
    thin traded volume all defer a NEW entry. An abstention never defers, because
    "we could not look" is not evidence against a name.
    """
    defer = {}
    for finding in findings:
        if finding['abstained'] or finding['verdict'] in (None, 'ordinary', 'usable',
                                                          'ample traded volume',
                                                          'inside its recent range',
                                                          'visible book measured'):
            continue
        if finding['verdict'] == 'below its trailing average':
            continue  # the frozen strategy already excludes this case; no advisory needed
        defer.setdefault(finding['symbol'], []).append(
            {'role': finding['role'], 'verdict': finding['verdict'],
             'reason': finding['proposed_action']})
    return defer


FRESHNESS_BASIS = (
    'Freshness is measured from the conservative 13:00 New York completion bound the paper '
    'accounting uses for a daily session, not from the actual closing bell. A regular session '
    'closes up to three hours later, so the figure is an UPPER bound on how old the data is: the '
    'reading is at most that many hours behind the close, and on an ordinary day about three hours '
    'less. It is measured to the moment the reading was complete, not to the moment its cycle '
    'started, and both instants are recorded separately and exactly.')


def supervisor(findings, session, observed_at, session_age_hours, *, core_target=None,
               intake_meta=None, shadow=None, cycle_notes=None, core_observed_at=None,
               cycle_started_at=None):
    """One plain-language digest over the other four roles.

    Reports coverage, freshness, what was proposed, what was skipped and why, and
    where two roles disagree about the same name. It does not average the roles
    into a score and it does not overrule the frozen strategy.
    """
    core_target = dict(core_target or {})
    by_role = {}
    for finding in findings:
        by_role.setdefault(finding['role'], []).append(finding)
    coverage = {}
    for role, rows in sorted(by_role.items()):
        abstained = [row for row in rows if row['abstained']]
        coverage[role] = {
            'title': ROLE_TITLES.get(role, role), 'examined': len(rows),
            'reported': len(rows) - len(abstained), 'abstained': len(abstained),
            'abstention_reasons': sorted({row['abstention_reason'] for row in abstained}),
            'actions': [row['proposed_action'] for row in rows
                        if not row['abstained'] and row['proposed_action'] != NO_ACTION],
        }
    defer = deferral_symbols(findings)
    in_target = sorted(symbol for symbol in defer if symbol in core_target)
    disagreements = []
    for symbol, flags in sorted(defer.items()):
        quiet = sorted({row['role'] for row in findings
                        if row['symbol'] == symbol and not row['abstained']
                        and row['proposed_action'] == NO_ACTION})
        if quiet:
            disagreements.append({'symbol': symbol,
                                  'flagged_by': [flag['role'] for flag in flags],
                                  'no_action_from': quiet})
    lines = []
    if core_target:
        lines.append(f'The frozen strategy is holding or queueing {len(core_target)} name(s) for '
                     f'session {session}. The helpers looked at the same session independently.')
    else:
        lines.append(f'The frozen strategy has no equity target for session {session}, so the '
                     f'helpers report on the pool without any entry to advise on.')
    if in_target:
        lines.append('Flagged inside the strategy\'s own target: ' + ', '.join(in_target) +
                     '. These are advisory notes; the strategy\'s trades are unchanged.')
    elif defer:
        lines.append(f'{len(defer)} name(s) flagged, none of them inside the strategy\'s current '
                     f'target, so nothing would have changed even if the advisory were binding.')
    else:
        lines.append('No helper proposed an action this session.')
    blind = sorted({role for role, values in coverage.items() if values['examined']
                    and values['reported'] == 0})
    if blind:
        lines.append('Reporting nothing at all this session: ' +
                     ', '.join(ROLE_TITLES.get(role, role) for role in blind) +
                     '. Their reasons are listed beside each helper.')
    if session_age_hours is not None:
        lines.append(f'Observation freshness: this reading was complete at {observed_at}, which is '
                     f'{session_age_hours:.1f} hours after the conservative 13:00 New York '
                     f'completion bound for session {session}. The actual close can be up to three '
                     f'hours later, so the data is at most {session_age_hours:.1f} hours old.')
    if cycle_started_at and cycle_started_at != observed_at:
        lines.append(f'The cycle began at {cycle_started_at} and the reading was complete at '
                     f'{observed_at}. The later instant is the one recorded and the one the virtual '
                     f'books time their decisions from, because that is when the evidence existed.')
    if core_observed_at:
        lines.append(f'The strategy itself read this session at {core_observed_at}. The helpers read '
                     f'it separately, and the virtual books time their decisions from the helpers\' '
                     f'reading, which is when an advisory actually existed.')
    if intake_meta:
        lines.append(('Data was reused from the cached snapshot fetched '
                      if intake_meta.get('cached') else 'Data was fetched fresh at ') +
                     str(intake_meta.get('fetched_at')) + '.')
    if shadow:
        lines.append(shadow.get('summary', ''))
    return {'role': ROLE_SUPERVISOR, 'title': ROLE_TITLES[ROLE_SUPERVISOR], 'session': session,
            'observed_at': observed_at, 'session_age_hours': session_age_hours,
            'core_observed_at': core_observed_at, 'cycle_started_at': cycle_started_at,
            'freshness_basis': FRESHNESS_BASIS,
            'coverage': coverage, 'deferrals': defer, 'deferrals_in_core_target': in_target,
            'disagreements': disagreements, 'core_target': core_target,
            'intake': intake_meta or {}, 'shadow': shadow or {},
            'notes': [note for note in (cycle_notes or []) if note],
            'plain_language': [line for line in lines if line],
            'scoring_policy': ('No confidence score is produced. Each reading carries its '
                               'measurement, the number of completed observations behind it, its '
                               'age, and an explicit reason when a helper declines to speak.')}
