"""Forward paper progress view: how far each family and comparison is from its sample gates.

Read-only and repeatable. It runs coverage_report.build (every SQLite file opened with mode=ro, no
network request, no forced cycle, nothing written) and reorganises the result around one question:
which comparison is closest to the minimum-sample gates, and what exactly is still missing.

For each comparison it shows the three gates (complete matched sessions, matched return intervals,
whole-window fills by the strategy account), the gate that binds, and the fewest further matched bars
per account that could still satisfy the gates if nothing else were missed. That count is arithmetic on
the calendar, not a forecast: no date, no rate, no return and no profit is projected, and bars that are
missed are never backfilled. Fills cannot be scheduled (they follow signals), so only their count is shown.

It also separates the evidence behind the gaps: provider failure, checks the collector recorded but
the bar did not appear, and stretches with no collector attempt at all (which never prove sleep).
Meeting a gate lets a difference be discussed; it is not evidence of an edge, and more accounts on the
same calendar do not add independent sessions.

    python -B scripts/forward_progress.py --until 2026-10-08T00:30:00+00:00
    python -B scripts/forward_progress.py --until 2026-10-08T00:30:00+00:00 --baseline-until 2026-10-07T00:12:00+00:00
    python -B scripts/forward_progress.py --until ... --json
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from collection_report import aware   # noqa: E402
import coverage_report as cr           # noqa: E402
from collector import CALENDAR_YEARS   # noqa: E402

UTC = timezone.utc
GATES = ('sessions', 'intervals', 'fills')
EXPOSURE_MISMATCH_PP = 10.0      # a reference this much more invested than its strategy is not an exposure-matched comparison
GUARDRAILS = [
    'Nothing here is a verdict, a forecast or a claim of an edge. Gates only decide when a difference may be discussed.',
    'Comparisons in one family share the same calendar sessions and the same collector outages, so more accounts do not add independent sessions.',
    'Fills count executed legs, not independent decisions; the number of distinct fill bars is shown beside it.',
    'References are fully invested; a strategy holding cash differs from them by exposure as well as by signal.',
    'Silence in the attempt log never proves sleep. A silence inside the span the latest process marker records (start, and stop if one is stored) shows no restart there; it is not proof of what the machine was doing.',
    'Bars still needed are arithmetic on the bundled calendar under uninterrupted matched collection. They are not a date, a rate or a forecast.',
    'A missed bar is never backfilled: a session with any missed bar cannot count, whatever is recorded afterwards.',
]


def bars_per_session(fam):
    """Bars in an ordinary session of the family: the most common size among its complete sessions.

    Only an ordinary-session equivalent. Early closes are shorter, so it is never used as a bound.
    """
    sizes = Counter(s['full_session_bars'] for s in fam['sessions'] if s['complete'])
    return sizes.most_common(1)[0][0] if sizes else None


def future_sessions(kind, report):
    """[(day, bars)] of every session on the bundled calendar after the last session the window holds, in date order."""
    fam = report['families'][kind]
    last = max((s['session'] for s in fam['sessions']), default=None)
    pin = cr.utc(report['window']['until']).date()
    day = datetime.fromisoformat(last).date() + timedelta(days=1) if last else pin
    end = datetime(max(CALENDAR_YEARS), 12, 31).date()
    out = []
    while day <= end:
        size = len(cr.full_session(kind, day.isoformat()))
        if size:
            out.append((day.isoformat(), size))
        day += timedelta(days=1)
    return out


def further_bars(c, future, per_session=None):
    """What the session and interval gates still need, as calendar arithmetic that assumes nothing further is missed.

    Three different figures, never mixed:
      next_sessions_path_bars     the bars of the next sessions on the calendar, in order, until both gates are met
                                  (exact for the earliest possible completion, including early closes);
      minimum_further_matched_bars_per_account
                                  a true lower bound: no choice of future sessions in the bundled calendar needs fewer;
      ordinary_session_equivalent_bars
                                  sessions still needed times an ordinary session, a conditional figure that early
                                  closes can undercut. It is not a bound.
    A session still open that is matched so far in full costs only its remaining bars; a pending session that
    already lost a bar cannot complete. After a missed trailing bar the first further matched bar is a new anchor
    and adds no return interval.
    """
    sessions_left = max(0, c['need']['sessions'] - c['have']['sessions'])
    intervals_left = max(0, c['need']['intervals'] - c['have']['intervals'])
    anchor = 0 if (not intervals_left or c.get('continues_at_pin')) else 1
    for_intervals = intervals_left + anchor if intervals_left else 0
    pending = sorted((p['session'], max(0, p['bars_in_session'] - p['due_bars'])) for p in c.get('pending_sessions', [])
                     if p['matched_so_far_in_full'])
    ahead = pending + list(future)
    lower = path = path_last = None
    if len(ahead) >= sessions_left:
        costs = sorted(bars for _, bars in ahead)[:sessions_left]
        by_sessions = sum(costs)
        lower = max(by_sessions, for_intervals)
        total = count = 0
        for day, bars in ahead:
            if count >= sessions_left and total >= for_intervals:
                break
            total, count, path_last = total + bars, count + 1, day
        if count >= sessions_left and total >= for_intervals:
            path = total
        else:
            path = path_last = None          # the bundled calendar ends before both gates could be met
    binding = []
    if lower:
        binding = [name for name, left in (('sessions', by_sessions), ('intervals', for_intervals)) if left == lower]
    return {'sessions_left': sessions_left, 'intervals_left': intervals_left, 'interval_anchor_bars': anchor,
            'bars_for_intervals': for_intervals,
            'next_sessions_path_bars': path, 'next_sessions_path_last_session': path_last,
            'minimum_further_matched_bars_per_account': lower, 'binding_bar_gate': binding,
            'ordinary_session_equivalent_bars': sessions_left * per_session if per_session else None, 'bars_per_session': per_session,
            'bound_basis': 'bundled calendar to the end of %d' % max(CALENDAR_YEARS)}


def ranking_bars(row):
    """Bars along the next-sessions path, used only to order accounts whose gate shares are equal; None sorts last."""
    value = row['further']['next_sessions_path_bars']
    return float('inf') if value is None else value


def comparison_row(c, per_session, future):
    gates = {name: {'have': c['have'][name], 'need': c['need'][name], 'remaining': max(0, c['need'][name] - c['have'][name]),
                    'met': c['have'][name] >= c['need'][name]} for name in GATES}
    share = {name: min(1.0, g['have'] / g['need']) if g['need'] else 1.0 for name, g in gates.items()}
    bottleneck = min(GATES, key=lambda name: (share[name], GATES.index(name)))
    return {'kind': c['kind'], 'instrument': c['pair'], 'strategy': c['strategy'], 'reference': c['reference'],
            'matched_bars': c['matched_bars'], 'observed_sessions': c['observed_sessions'],
            'complete_matched_sessions': c['complete_matched_sessions'], 'eligible_session_dates': c['eligible_session_dates'],
            'blocked_no_matched_bar': c['matched_bars'] == 0, 'verdict': c['verdict'], 'gates': gates,
            'bottleneck_gate': bottleneck, 'bottleneck_share_pct': round(100 * share[bottleneck], 1),
            'further': further_bars(c, future, per_session)}


def fill_events(info, start, until):
    """Distinct bars on which the account filled, inside the window (a rebalance of ten names is one event)."""
    bars = {o['asof'] for o in info['observations'] if o['observed_at'] and start <= o['observed_at'] <= until}
    return len({f['asof'] for f in info['fills'] if f['asof'] in bars})


def history_by_pin(info, until):
    """[(time, fill)] of the fills this account had recorded by the pin, oldest first.

    A fill is dated by the observation of its bar when there is one, otherwise by its bar label. Anything dated
    after the pin is not evidence the pin could have had, however early its label reads.
    """
    seen = {o['asof']: o['observed_at'] for o in info['observations']}
    dated = [(seen.get(f['asof']) or cr.key_time(f['asof']), f) for f in info['fills']]
    return sorted(((when, f) for when, f in dated if when <= until), key=lambda item: item[0])


def fairness(row, report, data, start, until):
    """Facts about how alike a strategy and its reference are, from what the books held at the pin.

    Universe and entry facts use only fills recorded by the pin (a later fill cannot change a pinned report);
    fees, exposure and fill counts are the whole-window economics, which already stop at the pin.
    """
    fam = report['families'][row['kind']]
    s, r = data[row['strategy']], data[row['reference']]
    es, er = fam['economics'][row['strategy']], fam['economics'][row['reference']]
    if s['error'] or r['error']:
        # An account that could not be read has no initial capital or fills here; inferring flags from that would be a guess.
        return {'flags': ['unreadable_account'], 'history_basis': 'unreadable account: nothing inferred',
                'unreadable': {row['strategy']: s['error'], row['reference']: r['error']}}
    past_s, past_r = history_by_pin(s, until), history_by_pin(r, until)
    traded_s, traded_r = {f['ticker'] for _, f in past_s}, {f['ticker'] for _, f in past_r}
    before_window = [f for when, f in past_r if when < start]
    gap = None
    if es['mean_exposure_pct'] is not None and er['mean_exposure_pct'] is not None:
        gap = round(er['mean_exposure_pct'] - es['mean_exposure_pct'], 1)
    flags = []
    if s['initial'] != r['initial']:
        flags.append('initial_capital_differs')
    if gap is not None and gap >= EXPOSURE_MISMATCH_PP:
        flags.append('reference_more_invested')
    if traded_s and traded_r and not traded_s <= traded_r:
        flags.append('different_universe')
    if before_window:
        flags.append('reference_entered_before_window')       # an actual recorded fill before the window, not an absence of fills
    return {'initial_strategy': s['initial'], 'initial_reference': r['initial'],
            'mean_exposure_pct_strategy': es['mean_exposure_pct'], 'mean_exposure_pct_reference': er['mean_exposure_pct'],
            'reference_minus_strategy_exposure_pp': gap, 'strategy_fees_in_window': es['fees_paid'], 'reference_fees_in_window': er['fees_paid'],
            'strategy_fees_pct_of_initial': es['fees_pct_of_initial'], 'strategy_traded_not_in_reference': sorted(traded_s - traded_r),
            'reference_fills_before_window': len(before_window), 'history_basis': 'fills recorded by the pin',
            'flags': flags}


def silence_summary(report):
    process = report['process']
    current = process['current']
    started, stopped = cr.utc(current['started_at']), cr.utc(current.get('stopped_at'))
    periods = [p for p in process['silent_periods'] if p['cause'] != 'collector had not started recording']
    # Only stretches wholly inside the span the marker records count; one that begins after a recorded stop, or
    # straddles it, is outside it.
    inside = [p for p in periods if started and cr.utc(p['from']) >= started and (stopped is None or cr.utc(p['to']) <= stopped)]
    after_stop = [p for p in periods if stopped is not None and cr.utc(p['to']) > stopped]
    window_minutes = report['window']['hours'] * 60
    minutes = sum(p['minutes'] for p in periods)
    first_checks = Counter()
    for p in periods:
        first_checks.update(p.get('outcomes_on_resume', {}))
    return {'periods': len(periods), 'minutes': round(minutes, 1),
            'share_of_window_pct': round(100 * minutes / window_minutes, 1) if window_minutes else None,
            'inside_recorded_process_span': {'since': current['started_at'], 'stopped_at': current.get('stopped_at'), 'pid': current['pid'],
                                             'periods': len(inside), 'minutes': round(sum(p['minutes'] for p in inside), 1)},
            'after_recorded_stop': {'periods': len(after_stop), 'minutes': round(sum(p['minutes'] for p in after_stop), 1)},
            'periods_ending_in_a_failed_first_check': sum(1 for p in periods if p.get('outcomes_on_resume', {}).get('error')
                                                          or p.get('outcomes_on_resume', {}).get('held')),
            'first_checks_after_silence': dict(first_checks),
            'interrupted_checks': len(process['recorded_interrupted_checks']),
            'note': 'Silence is the absence of attempts. It does not distinguish sleep, suspension, a stuck process or a pause; '
                    'a stretch wholly inside the span the latest process marker records (start, and stop when one is stored) shows no restart '
                    'there. The marker is stored evidence, not a check that the process is alive now.'}


def evidence_ledger(report, kind):
    fam = report['families'][kind]
    causes, classes, silent_minutes = Counter(), Counter(), 0.0
    for g in fam['gaps']:
        causes.update(g['bar_causes'])
        classes.update(g.get('bar_failure_classes', {}))
        silent_minutes += g['silent_overlap_minutes']
    return {'expected_bars': fam['expected_bars'], 'recorded_bars': fam['recorded_bars'], 'missing_bars': fam['missing_bars'],
            'readiness_windows_with_a_miss': len(fam['gaps']), 'missing_bars_by_evidence': dict(causes),
            'bars_blamed_on_a_failed_check_by_class': dict(classes),
            'missing_bars_with_no_attempt_evidence': causes.get('collector_silent', 0) + causes.get('no_attempt_recorded', 0),
            'failed_checks_in_missed_bar_windows_by_class': dict(sum((Counter(g.get('failure_classes', {})) for g in fam['gaps']), Counter())),
            'accepted_checks_without_the_bar': sum(g['accepted_checks'] for g in fam['gaps'])}


def session_ledger(report, kind):
    fam = report['families'][kind]
    comps = [c for c in report['comparisons'] if c['kind'] == kind]
    rows = []
    for s in fam['sessions']:
        touching = [g for g in fam['gaps'] if s['session'] in g['sessions']]
        rows.append({'session': s['session'], 'status': s['status'], 'recorded': s['recorded'], 'expected': s['expected'],
                     'bars_in_session': s['full_session_bars'], 'family_full': s['full'],
                     'comparisons_fully_matched': sum(1 for c in comps if s['session'] in c['eligible_session_dates']),
                     'comparisons': len(comps), 'gap_causes': sorted({cause for g in touching for cause in g['causes']})})
    return rows


def family_summary(report, kind, rows):
    fam = report['families'][kind]
    mine = [r for r in rows if r['kind'] == kind]
    econ = fam['economics']
    strategies = sorted({r['strategy'] for r in mine})
    references = sorted({r['reference'] for r in mine})

    def spread(ids, key):
        values = [econ[i][key] for i in ids if econ[i][key] is not None]
        return {'min': min(values), 'median': round(statistics.median(values), 3), 'max': max(values)} if values else None
    matched = [r['complete_matched_sessions'] for r in mine]
    return {'label': fam['label'], 'accounts': fam['accounts'], 'instruments': len({r['instrument'] for r in mine}),
            'comparisons': len(mine), 'evidence_lines': len(strategies), 'bars_per_session': bars_per_session(fam),
            'family_complete_sessions': fam['complete_sessions'], 'family_fully_recorded_sessions': fam['full_sessions'],
            'best_complete_matched_sessions': max(matched, default=0), 'median_complete_matched_sessions': statistics.median(matched) if matched else 0,
            'sessions_needed': mine[0]['gates']['sessions']['need'] if mine else None,
            'blocked_comparisons': sum(1 for r in mine if r['blocked_no_matched_bar']),
            'thresholds_met': sum(1 for r in mine if r['verdict'] != 'insufficient_sample'),
            'coverage_pct': fam['coverage_pct'], 'latency_minutes': fam['observation_latency_minutes'],
            'strategy_fills': sum(econ[i]['fills'] for i in strategies), 'strategy_fees_paid': round(sum(econ[i]['fees_paid'] for i in strategies), 2),
            'strategy_fees_pct_of_initial': spread(strategies, 'fees_pct_of_initial'),
            'strategy_mean_exposure_pct': spread(strategies, 'mean_exposure_pct'),
            'reference_mean_exposure_pct': spread(references, 'mean_exposure_pct'),
            'accounts_with_no_recorded_bar': fam['accounts_with_no_recorded_bar'],
            'ceiling_note': 'No comparison here can have more complete matched sessions than the family\'s fully recorded complete sessions.'}


def closest(rows):
    """Strategy accounts ranked by how near they are to the gates, tied accounts grouped.

    Comparisons sharing one strategy account share its sessions and fills, so they are one line of evidence.
    Accounts of one family in an identical gate state are one entry: listing dozens of equal counts one by
    one would only rank them by name.
    """
    by_strategy = {}
    for r in rows:
        by_strategy.setdefault((r['kind'], r['strategy']), []).append(r)
    states = {}
    for (kind, strategy), members in by_strategy.items():
        best = min(members, key=lambda r: (-r['bottleneck_share_pct'], ranking_bars(r), r['reference']))
        key = (kind, json.dumps(best['gates'], sort_keys=True), ranking_bars(best), best['strategy_fill_events'])
        entry = states.setdefault(key, {'kind': kind, 'best': best, 'strategy_accounts': [], 'references': set(), 'comparisons': 0, 'blocked': 0})
        entry['strategy_accounts'].append(strategy)
        entry['references'].update(r['reference'] for r in members)
        entry['comparisons'] += len(members)
        entry['blocked'] += sum(1 for r in members if r['blocked_no_matched_bar'])
    out = [{**e, 'strategy_accounts': sorted(e['strategy_accounts']), 'references': sorted(e['references']),
            'identical_across_references': len({json.dumps(r['gates'], sort_keys=True) for r in rows
                                                if r['kind'] == e['kind'] and r['strategy'] in e['strategy_accounts']}) == 1}
           for e in states.values()]
    out.sort(key=lambda x: (-x['best']['bottleneck_share_pct'], ranking_bars(x['best']),
                            x['kind'], x['strategy_accounts'][0]))
    return out


def change_since(report, baseline):
    """What moved between the baseline pin and this one (counts only; no rate, no extrapolation)."""
    before = {(c['strategy'], c['reference']): c for c in baseline['comparisons']}
    moved = []
    for c in report['comparisons']:
        old = before.get((c['strategy'], c['reference']))
        delta = c['complete_matched_sessions'] - (old['complete_matched_sessions'] if old else 0)
        if delta:
            moved.append({'kind': c['kind'], 'strategy': c['strategy'], 'reference': c['reference'], 'complete_matched_sessions_added': delta})
    families = {}
    for kind, fam in report['families'].items():
        old = baseline['families'].get(kind)
        families[kind] = {'complete_sessions_added': fam['complete_sessions'] - (old['complete_sessions'] if old else 0),
                          'fully_recorded_sessions_added': fam['full_sessions'] - (old['full_sessions'] if old else 0),
                          'bars_recorded_added': fam['recorded_bars'] - (old['recorded_bars'] if old else 0),
                          'bars_missed_added': fam['missing_bars'] - (old['missing_bars'] if old else 0)}
    return {'baseline_until': baseline['window']['until'], 'thresholds_met_baseline': baseline['summary']['thresholds_met'],
            'comparisons_baseline': baseline['summary']['comparisons'], 'families': families,
            'comparisons_that_gained_complete_matched_sessions': len(moved), 'examples': moved[:5]}


def build(runtime, since=None, until=None, baseline_until=None, now=None):
    runtime = Path(runtime)
    now = now or datetime.now(UTC)
    pinned = until is not None
    report = cr.build(runtime, since, until, now)
    start, end = cr.utc(report['window']['since']), cr.utc(report['window']['until'])
    accounts = {a['id']: a for a in cr.discover(runtime)}
    data = {i: cr.load_account(accounts[i]) for r in report['comparisons'] for i in (r['strategy'], r['reference'])}
    per_session = {kind: bars_per_session(fam) for kind, fam in report['families'].items()}
    future = {kind: future_sessions(kind, report) for kind in report['families']}
    rows = [comparison_row(c, per_session[c['kind']], future[c['kind']]) for c in report['comparisons']]
    for row in rows:
        row['fairness'] = fairness(row, report, data, start, end)
        row['strategy_fill_events'] = fill_events(data[row['strategy']], start, end)
    hold = report['held_accounts']['hourly_quality']
    blocked = [{'comparison': f"{r['strategy']} vs {r['reference']}", 'instrument': r['instrument'],
                'reason': hold['holds'].get(r['instrument'], 'no matched bar; no stored reason')} for r in rows if r['blocked_no_matched_bar']]
    out = {'generated_at': cr.iso(now), 'pinned': pinned, 'window': report['window'],
           'status': {'comparisons': len(rows), 'thresholds_met': sum(1 for r in rows if r['verdict'] != 'insufficient_sample')},
           'families': {kind: family_summary(report, kind, rows) for kind in report['families']},
           'closest': closest(rows), 'comparisons': rows,
           'sessions': {kind: session_ledger(report, kind) for kind in report['families']},
           'evidence': {kind: evidence_ledger(report, kind) for kind in report['families']},
           'silence': silence_summary(report),
           'checks': {family: {'checks': c['checks'], 'outcomes': c['outcomes'], 'reasons': c['reasons'],
                               # Counted from every raw failed attempt in coverage_report.attempt_summary, not from the capped reasons list.
                               'failed_checks_by_class': c['failed_checks_by_class']} for family, c in report['checks'].items()},
           'held': {'blocked_comparisons': blocked, 'readiness_holds': hold['readiness_holds'],
                    'market_closed_or_stale_symbols': len(hold['market_closed_or_stale']),
                    'quality_file_written_after_window_end': hold['observed_after_window_end']},
           'fairness_flags': dict(Counter(flag for r in rows for flag in r['fairness']['flags'])),
           'guardrails': GUARDRAILS, 'notes': report['notes']}
    if baseline_until:
        out['change_since_baseline'] = change_since(report, cr.build(runtime, since, baseline_until, now))
    return out


# --------------------------------------------------------------------- text

def stamp(value):
    return cr.short_time(value) if value else 'none'


def gate_text(row):
    parts = []
    for name in GATES:
        g = row['gates'][name]
        parts.append(f"{name} {g['have']}/{g['need']}" + ('' if not g['met'] else ' met'))
    return ', '.join(parts)


def render(report, top=8, sessions_shown=8):
    w = report['window']
    lines = [f"Forward paper progress, window {stamp(w['since'])} to {stamp(w['until'])} ({w['hours']} h)"
             + ('' if report['pinned'] else ' (UNPINNED: the end is now, so this will not reproduce)'),
             'Read-only. No verdict, no forecast, no profit or date projection; a count of what is missing.']
    s = report['status']
    lines += ['', f"STATUS: {s['thresholds_met']} of {s['comparisons']} comparisons meet the sample gates."]
    if 'change_since_baseline' in report:
        b = report['change_since_baseline']
        lines.append(f"  baseline pin {stamp(b['baseline_until'])}: {b['thresholds_met_baseline']} of {b['comparisons_baseline']} met; "
                     f"{b['comparisons_that_gained_complete_matched_sessions']} comparisons gained complete matched sessions since")
        for kind, d in b['families'].items():
            lines.append(f"    {report['families'][kind]['label']:22} complete sessions {d['complete_sessions_added']:+d}, fully recorded {d['fully_recorded_sessions_added']:+d}, "
                         f"bars recorded {d['bars_recorded_added']:+d}, bars missed {d['bars_missed_added']:+d}")
    lines += ['', 'FAMILY GATES (complete matched sessions are the slowest gate; the family ceiling bounds every comparison in it)']
    for kind, f in report['families'].items():
        lines.append(f"  {f['label']:22} {f['comparisons']:3} comparisons on {f['instruments']} instrument(s) / {f['evidence_lines']} strategy account(s); "
                     f"{f['bars_per_session']} bars in an ordinary session; family complete {f['family_complete_sessions']}, fully recorded "
                     f"{f['family_fully_recorded_sessions']}; best matched {f['best_complete_matched_sessions']}/{f['sessions_needed']}"
                     + (f"; {f['blocked_comparisons']} blocked (no matched bar)" if f['blocked_comparisons'] else ''))
    lines += ['', 'CLOSEST TO THE GATES (ranked by the lowest have/need over the three gates; strategy accounts in an identical state are grouped; '
                  f'{len(report["closest"])} states, top {top})']
    for rank, cl in enumerate(report['closest'][:top], 1):
        b = cl['best']
        f = b['further']
        accounts, refs = cl['strategy_accounts'], cl['references']
        named = ', '.join(accounts[:3]) + (f' +{len(accounts) - 3} more' if len(accounts) > 3 else '')
        against = ', '.join(refs[:4]) + (f' +{len(refs) - 4} more' if len(refs) > 4 else '')
        lines.append(f"  {rank}. {len(accounts)} strategy account(s) [{named}] vs {against}: {gate_text(b)}; "
                     f"bottleneck {b['bottleneck_gate']} at {b['bottleneck_share_pct']}%")
        path = ('beyond the bundled calendar' if f['next_sessions_path_bars'] is None else
                f"{f['next_sessions_path_bars']} further matched bars per account"
                + (f" (through the {f['next_sessions_path_last_session']} session)" if f['next_sessions_path_last_session'] else ''))
        floor = 'not computable' if f['minimum_further_matched_bars_per_account'] is None else f"{f['minimum_further_matched_bars_per_account']}"
        lines.append(f"       still needed: {f['sessions_left']} complete matched sessions. If the next sessions on the calendar are all fully matched: {path}; "
                     f"no set of future sessions in the {f['bound_basis']} needs fewer than {floor} bars"
                     f"{', binding ' + '+'.join(f['binding_bar_gate']) if f['binding_bar_gate'] else ''}"
                     f"{', plus 1 anchor bar after a missed trailing bar' if f['interval_anchor_bars'] else ''}. "
                     f"Ordinary-session equivalent (conditional, not a bound): {f['ordinary_session_equivalent_bars']}. "
                     f"{b['gates']['fills']['remaining']} more fills (not schedulable; {b['strategy_fill_events']} fill bar(s) so far)"
                     + ('' if cl['identical_across_references'] else '; gate state differs across its references'))
    lines += ['', 'WHERE THE SESSIONS WENT (latest complete and open sessions per family)']
    for kind, rows in report['sessions'].items():
        lines.append(f"  {report['families'][kind]['label']}")
        for r in rows[-sessions_shown:]:
            tag = 'full' if r['family_full'] else r['status']
            cause = f" [{', '.join(r['gap_causes'])}]" if r['gap_causes'] else ''
            lines.append(f"    {r['session']} {tag:20} {r['recorded']}/{r['expected']} of {r['bars_in_session']}; fully matched in "
                         f"{r['comparisons_fully_matched']}/{r['comparisons']} comparisons{cause}")
    lines += ['', 'MISSING EVIDENCE (per family; bars judged on their own readiness windows)']
    for kind, e in report['evidence'].items():
        causes = ', '.join(f'{k} {v}' for k, v in sorted(e['missing_bars_by_evidence'].items(), key=lambda kv: -kv[1])) or 'none'
        classes = ', '.join(f'{k} {v}' for k, v in e['bars_blamed_on_a_failed_check_by_class'].items())
        lines.append(f"  {report['families'][kind]['label']:22} {e['recorded_bars']}/{e['expected_bars']} recorded, {e['missing_bars']} missing in "
                     f"{e['readiness_windows_with_a_miss']} gap(s): {causes}" + (f"; failed-check bars by class: {classes}" if classes else ''))
    sil = report['silence']
    inside, after = sil['inside_recorded_process_span'], sil['after_recorded_stop']
    span = (f"since {stamp(inside['since'])}, no stop recorded" if not inside['stopped_at']
            else f"{stamp(inside['since'])} to {stamp(inside['stopped_at'])}")
    lines.append(f"  silence: {sil['periods']} stretch(es) with no attempt from any family, {sil['minutes']} min ({sil['share_of_window_pct']}% of the window); "
                 f"{inside['periods']} ({inside['minutes']} min) fall wholly inside the span recorded for the latest process (pid {inside['pid']}, {span}): no restart there"
                 + (f"; {after['periods']} ({after['minutes']} min) end after its recorded stop" if inside['stopped_at'] else '') + "; "
                 f"{sil['periods_ending_in_a_failed_first_check']} ended with a failed first check; {sil['interrupted_checks']} check(s) were recorded as interrupted")
    lines.append('  ' + sil['note'])
    for family, c in report['checks'].items():
        for r in c['reasons']:
            lines.append(f"  failed check x{r['count']:<3} {family:11} {r['message']}")
    by_class = Counter()
    for c in report['checks'].values():
        by_class.update(c['failed_checks_by_class'])
    lines.append('  recorded failed checks in the window by class (checks, not bars; bars are counted above): '
                 + (', '.join(f'{k} {v}' for k, v in sorted(by_class.items())) or 'none'))
    lines += ['', 'HELD / BLOCKED']
    held = report['held']
    lines.append(f"  {len(held['blocked_comparisons'])} comparison(s) have no matched bar"
                 + (' (quality file was written after the window end)' if held['quality_file_written_after_window_end'] else ''))
    for item in held['blocked_comparisons'][:12]:
        lines.append(f"    {item['comparison']}: {item['reason']}")
    if held['market_closed_or_stale_symbols']:
        lines.append(f"  {held['market_closed_or_stale_symbols']} further symbol(s) only show a market-closed-or-stale state in that file")
    lines += ['', 'FEES, EXPOSURE, LATENCY (whole-window, strategy accounts)']
    for kind, f in report['families'].items():
        pct, exp, ref = f['strategy_fees_pct_of_initial'], f['strategy_mean_exposure_pct'], f['reference_mean_exposure_pct']
        lat = f['latency_minutes']
        lines.append(f"  {f['label']:22} {f['strategy_fills']:4} fills, ${f['strategy_fees_paid']:,.2f} fees"
                     + (f" ({pct['min']}%..{pct['max']}% of initial capital)" if pct else '')
                     + (f"; mean exposure {exp['min']:.0f}%..{exp['max']:.0f}% vs reference {ref['min']:.0f}%..{ref['max']:.0f}%" if exp and ref else '')
                     + (f"; latency median {lat['median']} / max {lat['max']} min, {lat['late_count']} later than {lat['late_threshold']}" if lat else ''))
    lines += ['', 'REFERENCE FAIRNESS (comparisons carrying each flag)']
    lines.append('  ' + (', '.join(f'{k}: {v}' for k, v in sorted(report['fairness_flags'].items())) or 'none'))
    lines += ['', 'GUARDRAILS'] + [f'  - {g}' for g in report['guardrails']]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--since', type=aware, help='Window start (default: first collector attempt)')
    parser.add_argument('--until', type=aware, help='Window end; pin it to reproduce a report (default: now)')
    parser.add_argument('--baseline-until', type=aware, help='An earlier pin to count what changed since')
    parser.add_argument('--top', type=int, default=8)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if not args.runtime.is_dir():
        raise SystemExit(f'Runtime folder not found: {args.runtime}')
    report = build(args.runtime, args.since, args.until, args.baseline_until)
    print(json.dumps(report, indent=2, default=str) if args.json else render(report, args.top))


if __name__ == '__main__':
    main()
