"""The research desk: runs the five helpers beside the frozen strategy.

WHAT THIS IS
    A read-only companion layer. It observes the same completed session the
    frozen core strategy just observed, runs five deterministic helpers over it,
    stores what each helper saw with full provenance, and keeps two SHADOW paper
    books so the advisory rule can be measured forward instead of asserted.

WHAT IT CANNOT DO
    It cannot change a core target, a core fill, core accounting or the baseline
    books; it never writes to them and never reads them for a decision. It has
    no order route and no broker interface. It calls no language model, so it
    needs no agent, assistant or background process running anywhere.

HOW IT AVOIDS LOOKING AHEAD
    * It only ever reads completed sessions, on the core's own 16:15 New York
      convention, and today's forming bar is withheld.
    * It observes the session the CORE used. If the core is holding, or its
      session is absent from the research snapshot, the desk abstains for the
      whole cycle instead of choosing its own session.
    * A session older than one already recorded is refused, so history cannot be
      replayed into the forward record as if it had been decided live.
    * Advisories are written once per session and never rewritten later.
    * The shadow books use the frozen PaperBook accounting unchanged, including
      its observed-time fill guard, so a shadow decision fills no earlier than a
      core decision would have.

API BUDGET
    One batched daily OHLCV request per cache window for the whole universe, plus
    at most four public order-book snapshots per cycle. Polling the dashboard
    more often does not add requests: cached snapshots are reused and reported as
    cached. Every request is a read-only GET.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import pandas as pd

from paper_book import PaperBook, PaperHold, session_close, is_daily_label
from research_intake import (DailyIntake, BookIntake, completed_sessions, panel_sha256,
                             DEPTH_POLICY, utc)
from research_ledger import ResearchLedger
import research_roles as roles
from strategy_c import BROAD_UNIVERSE, RISK_OFF_TICKERS, COST_PER_SIDE

VERSION = 'research-v1'
SHADOW_BASELINE = 'shadow-baseline'
SHADOW_FILTERED = 'shadow-filtered'
# Bounded sample of the crypto references the hourly lab already tracks. Four
# keeps public order-book fan-out small; the other references are not claimed on.
DEFAULT_CRYPTO_PRODUCTS = ('BTC-USD', 'ETH-USD', 'SOL-USD', 'XRP-USD')

class WallClock:
    """Production time source: UTC, and never allowed to move backwards.

    A backwards system adjustment must not let a later reading claim an earlier
    instant than one already recorded, because those instants gate fills.
    """

    def __init__(self):
        self._last = None

    def __call__(self):
        now = utc(datetime.now(timezone.utc))
        if self._last is not None and now < self._last:
            now = self._last
        self._last = now
        return now


class StepClock:
    """Deterministic clock seeded at `start`, advancing on every read.

    Used whenever a caller injects an observation time. An injected clock still
    PROGRESSES the way the production clock does, so a test cannot accidentally
    assert that fetching and measuring took no time at all and so silently
    reproduce the bug where a decision carried its cycle's start time.
    """

    def __init__(self, start, step_seconds=1.0):
        self._next = utc(start)
        self._step = timedelta(seconds=step_seconds)

    def __call__(self):
        value = self._next
        self._next = value + self._step
        return value


ADVISORY_RULE = ('A new entry is deferred when a helper reports an unusually heavy or thin '
                 'session, an extended close, a suspect price series or thin traded volume. '
                 'The deferred weight stays in cash rather than being moved to another name, '
                 'because redistributing it would be a different strategy. An abstention never '
                 'defers: "we could not look" is not evidence against a name.')


class ResearchDesk:
    def __init__(self, root, *, universe=None, crypto_products=DEFAULT_CRYPTO_PRODUCTS,
                 intake=None, books=None, ledger=None, cost_rate=COST_PER_SIDE,
                 shadow_cash=10000.0, enable_books=True, clock=None,
                 injected_step_seconds=1.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock if clock is not None else WallClock()
        self.injected_step_seconds = injected_step_seconds
        self.universe = sorted(set(universe or (list(BROAD_UNIVERSE) + list(RISK_OFF_TICKERS))))
        self.crypto_products = tuple(crypto_products or ())
        self.enable_books = enable_books
        self.intake = intake if intake is not None else DailyIntake(self.root / 'intake')
        self.books = books if books is not None else (
            BookIntake(self.root / 'intake') if enable_books else None)
        self.ledger = ledger if ledger is not None else ResearchLedger(self.root / 'research.sqlite3')
        self.shadow = {
            name: PaperBook(self.root / f'{name}.sqlite3', initial_cash=shadow_cash,
                            cadence='monthly', cost_rate=cost_rate,
                            version=f'{VERSION}/{name}')
            for name in (SHADOW_BASELINE, SHADOW_FILTERED)}
        self.message = 'Waiting for the first completed session observed by the core strategy.'

    # ---------------------------------------------------------------- helpers
    def pause(self, paused):
        """Pause the shadow books with the rest of the application."""
        for book in self.shadow.values():
            book.pause(paused)

    def _paused(self):
        return any(book.status()['paused'] for book in self.shadow.values())

    @staticmethod
    def _core_target(core_snapshot):
        weights = core_snapshot.get('target_weights') or {}
        return {symbol: float(weight) for symbol, weight in weights.items() if weight > 0}

    def _abstain(self, reason, observed_at, *, session=None):
        """Record the refusal itself, and nothing else.

        A hold is appended to its own table rather than written as a cycle, so it
        can never be read as a reading, and a later retry once data arrives adds
        the real advisories without erasing this refusal.
        """
        self.message = reason
        self.ledger.record_hold(session, observed_at, reason)
        return reason

    # ----------------------------------------------------------------- cycle
    def observe(self, core_snapshot=None, now=None):
        """Run one research cycle against the session the core just observed.

        TIME. Two instants are read from the clock and they are not the same
        thing. `cycle_started_at` is when this cycle began; the data had not been
        fetched yet and no measurement existed. The DECISION time is read after
        every fetch and every calculation, immediately before the decision is
        made immutable, and that is the instant a reading and its advisory
        actually existed. It is the one stamped on every reading, used for
        freshness, and given to both shadow books, because a bar that completed
        while this cycle was still fetching must not be able to fill an advisory
        that did not yet exist.

        Injecting `now` seeds a clock that still advances, so an injected time
        cannot collapse the two instants into one and hide that distinction.
        """
        if self._paused():
            self.message = 'Paused; the research helpers made no request and recorded nothing.'
            return self.message
        if not core_snapshot or not core_snapshot.get('asof'):
            self.message = ('Held: the frozen strategy has no completed snapshot this cycle, so '
                            'the helpers made no data request and recorded no advisory.')
            return self.message
        core_session = str(core_snapshot['asof'])
        clock = StepClock(now, self.injected_step_seconds) if now is not None else self.clock
        cycle_started_at = utc(clock())
        started_iso = cycle_started_at.isoformat()
        # Until the decision is ready this only labels a REFUSAL, never a reading.
        observed_iso = started_iso

        if not is_daily_label(core_session):
            return self._abstain(
                f'Held: {core_session} is not a completed daily session label. This layer reads the '
                f'core daily strategy only.', observed_iso)
        newest = self.ledger.newest_session()
        if newest and core_session < newest:
            return self._abstain(
                f'Held: session {core_session} is older than the recorded session {newest}. '
                f'Backdated advisories are refused so history cannot enter the forward record.',
                observed_iso, session=core_session)
        if self.ledger.has_advisories(core_session):
            # Already advised. Nothing is re-read, re-fetched or rewritten: the
            # first observation of a session is the one that stands.
            self.message = (f'Session {core_session} was already recorded; the first observation '
                            f'stands, no data was requested and nothing was rewritten.')
            return self.message
        prepared = self.ledger.decision(core_session)
        if prepared:
            # A previous cycle committed a decision for this session and then died
            # before its advisories were stored, possibly with only one shadow book
            # updated. Finish that decision exactly as it was taken: the same
            # targets, prices and observation time, and no new data request.
            return self._commit(prepared, recovered=True)
        try:
            closed_at = session_close(core_session)
        except ValueError:
            return self._abstain(f'Held: {core_session} is not a usable session label.', observed_iso)
        if closed_at > cycle_started_at:
            return self._abstain(
                f'Held: session {core_session} cannot have completed before this cycle began at '
                f'{started_iso}.', started_iso, session=core_session)

        try:
            # Fetches are timed from the cycle start, which is never later than the
            # decision: the completed-session cut can only withhold a bar, never
            # admit one that was still forming.
            panel, meta = self.intake.load(self.universe, now=cycle_started_at,
                                           require_session=pd.Timestamp(core_session))
        except Exception as exc:
            return self._abstain(
                f'Held: the research data intake is unavailable ({type(exc).__name__}: {exc}). '
                f'Previous advisories are preserved and nothing was estimated.',
                utc(clock()).isoformat(), session=core_session)
        panel = completed_sessions(panel, cycle_started_at)
        index = panel['Close'].index
        if len(index) == 0:
            return self._abstain('Held: the research snapshot contains no completed sessions.',
                                 utc(clock()).isoformat(), session=core_session)
        session_stamp = pd.Timestamp(core_session)
        if session_stamp not in index:
            return self._abstain(
                f'Held: session {core_session} used by the strategy is not present in the research '
                f'snapshot (latest available {index[-1].date()}). No helper substituted another '
                f'session, and the cycle will be retried when the data arrives.',
                utc(clock()).isoformat(), session=core_session)
        session = core_session
        used_sha = panel_sha256({field: frame.loc[:session_stamp] for field, frame in panel.items()})
        source = meta.get('source')
        core_target = self._core_target(core_snapshot)
        core_prices = {k: v for k, v in (core_snapshot.get('prices') or {}).items()
                       if isinstance(v, (int, float)) and math.isfinite(v)}

        # The roles are pure and read no clock. Their readings are stamped once,
        # below, with the decision time captured after all of this work.
        findings = roles.unusual_activity(panel, self.universe, session, None, None, source=source)
        flagged = sorted({row['symbol'] for row in findings
                          if not row['abstained'] and row['verdict'] != 'ordinary'})
        scope = sorted(set(core_target) | set(flagged))
        findings += roles.entry_quality(panel, scope, session, None, None, source=source)
        findings += roles.signal_noise(panel, scope, session, None, None,
                                       core_prices=core_prices, source=source)
        book_snapshots = {}
        if self.books is not None and self.crypto_products:
            book_snapshots = self.books.load(self.crypto_products, now=cycle_started_at)
        findings += roles.liquidity_depth(panel, scope, session, None, None,
                                          books=book_snapshots, source=source,
                                          book_source=DEPTH_POLICY)
        deferrals = roles.deferral_symbols(findings)

        # Every fetch and every measurement is now done. THIS is when the reading
        # and its advisory exist, so this is the instant that gets recorded and
        # that gates the shadow books' fills.
        decision_ready_at = max(utc(clock()), cycle_started_at)
        observed_iso = decision_ready_at.isoformat()
        session_age_hours = (decision_ready_at - closed_at).total_seconds() / 3600
        roles.stamp_findings(findings, observed_iso, session_age_hours)
        notes = [f'Universe examined for unusual activity: {len(self.universe)} names.',
                 f'Entry, integrity and liquidity helpers examined {len(scope)} name(s): the '
                 f'strategy\'s own target plus anything the activity helper flagged.',
                 ADVISORY_RULE]
        decision = {
            'session': session, 'observed_at': observed_iso,
            'cycle_started_at': started_iso,
            'core_session': core_session, 'core_observed_at': core_snapshot.get('fetched_at'),
            'intake_sha256': meta.get('sha256'), 'used_sha256': used_sha,
            'intake_meta': meta, 'session_age_hours': session_age_hours,
            'bar_end': core_snapshot.get('bar_end') or session_close(session).isoformat(),
            'bar_end_basis': core_snapshot.get('bar_end_basis'),
            'prices': core_prices, 'core_target': core_target,
            'filtered_target': {symbol: weight for symbol, weight in core_target.items()
                                if symbol not in deferrals},
            'deferrals': deferrals, 'findings': findings, 'notes': notes, 'outcome_scope': len(scope),
        }
        # Commit the decision BEFORE touching a shadow book. Everything after this
        # point is replayable from the stored row alone.
        stored, created = self.ledger.prepare_decision(session, decision)
        return self._commit(stored, recovered=not created)

    def _commit(self, decision, *, recovered=False):
        """Apply a stored decision: shadow books, then digest and advisories.

        Safe to call again after a crash anywhere inside it. The shadow books
        refuse a session they have already processed, `prepare_decision` is
        immutable, and the advisory rows are unique per session, role and symbol.
        """
        session, observed_iso = decision['session'], decision['observed_at']
        findings = decision['findings']
        shadow = self._cycle_shadow(decision)
        digest = roles.supervisor(findings, session, observed_iso,
                                  decision.get('session_age_hours'),
                                  core_target=decision.get('core_target'),
                                  intake_meta=decision.get('intake_meta'), shadow=shadow,
                                  cycle_notes=(decision.get('notes') or []) +
                                  (['This session\'s decision was prepared by an earlier cycle that '
                                    'did not finish; it was completed from the stored decision, with '
                                    'the original targets, prices and observation time.']
                                   if recovered else []),
                                  core_observed_at=decision.get('core_observed_at'),
                                  cycle_started_at=decision.get('cycle_started_at'))
        outcome = (f'{len(findings)} readings recorded for session {session}; '
                   f'{sum(1 for f in findings if f["abstained"])} abstentions; '
                   f'{len(decision.get("deferrals") or {})} name(s) flagged.')
        if recovered:
            outcome = 'Recovered an unfinished cycle. ' + outcome
        self.ledger.record(session, observed_iso, findings, digest, outcome=outcome,
                           core_session=decision.get('core_session'),
                           core_observed_at=decision.get('core_observed_at'),
                           intake_meta=decision.get('intake_meta'),
                           used_sha256=decision.get('used_sha256'))
        self.message = outcome
        return outcome

    # ---------------------------------------------------------------- shadow
    def _cycle_shadow(self, decision):
        """Two virtual books: the strategy's own target, and the filtered target.

        OBSERVATION TIME. Both books record the RESEARCH decision time, not the
        core's earlier fetch time. The filtered target only exists once the
        helpers have read the session, so timing its signal from the core's fetch
        would let a bar that completed in between fill a decision that did not yet
        exist. The core's own fetch time is kept beside it as provenance.

        Both books are given the same observation time and the same prices, so the
        only difference between them is the advisory filter.

        Unexpected failures are NOT swallowed here. A half-applied cycle is
        recoverable from the stored decision, while a digest claiming both books
        moved would not be.
        """
        prices = decision['prices']
        targets = {SHADOW_BASELINE: decision['core_target'],
                   SHADOW_FILTERED: decision['filtered_target']}
        deferrals = sorted(decision.get('deferrals') or {})
        outcomes = {}
        for name in (SHADOW_BASELINE, SHADOW_FILTERED):
            snapshot = {'asof': decision['session'],
                        'fetched_at': decision['observed_at'],
                        'bar_end': decision['bar_end'],
                        'bar_end_basis': decision.get('bar_end_basis'),
                        'prices': prices, 'target_weights': targets[name],
                        'strategy': f'{VERSION}/{name}',
                        'observation_basis': 'research decision time, not the core fetch time',
                        'core_observed_at': decision.get('core_observed_at'),
                        'core_session': decision.get('core_session'),
                        'advisory_deferrals': deferrals if name == SHADOW_FILTERED else []}
            try:
                outcomes[name] = self.shadow[name].cycle(snapshot)
            except PaperHold as exc:
                # A documented data hold. It changes no state and can legitimately
                # differ between the two books, which the matched comparison then
                # excludes rather than papering over.
                outcomes[name] = f'Held: {exc}'
        return {'rule': ADVISORY_RULE, 'deferrals': deferrals, 'outcomes': outcomes,
                **self._matched_comparison()}

    def _matched_comparison(self):
        """Compare the two shadow books only on sessions BOTH actually observed.

        A count of observations does not establish matched dates: a cycle that
        updated one book and died leaves the other behind, and comparing each
        book's own latest mark would invent a difference that no matched session
        supports.
        """
        status = {name: book.status() for name, book in self.shadow.items()}
        equity = {}
        for name, book in self.shadow.items():
            equity[name] = {row['asof']: row['equity'] for row in book.record()['observations']}
        common = sorted(set(equity[SHADOW_BASELINE]) & set(equity[SHADOW_FILTERED]))
        only_one = sorted(set(equity[SHADOW_BASELINE]) ^ set(equity[SHADOW_FILTERED]))
        matched = {'sessions': len(common), 'first': common[0] if common else None,
                   'latest': common[-1] if common else None,
                   'unmatched_sessions': only_one}
        if common:
            latest = common[-1]
            baseline_value = equity[SHADOW_BASELINE][latest]
            filtered_value = equity[SHADOW_FILTERED][latest]
            matched.update({'baseline_equity': baseline_value, 'filtered_equity': filtered_value,
                            'difference': filtered_value - baseline_value})
            summary = (f'Shadow evaluation: {len(common)} session(s) have been observed by both '
                       f'virtual books. On the latest of them, {latest}, the advisory-filtered book '
                       f'marks ${filtered_value:,.2f} against ${baseline_value:,.2f} for the '
                       f'unfiltered copy of the same targets, a difference of '
                       f'${filtered_value - baseline_value:,.2f}. Both are virtual and both start at '
                       f'the first session this desk observed, so this is a record, not a result: '
                       f'it cannot show whether the filter helps.')
        else:
            matched.update({'baseline_equity': None, 'filtered_equity': None, 'difference': None})
            summary = ('Shadow evaluation: no session has yet been observed by both virtual books, '
                       'so no comparison is shown.')
        if only_one:
            summary += (f' {len(only_one)} session(s) were recorded by only one book and are '
                        f'excluded from the comparison: {", ".join(only_one)}.')
        return {'matched': matched, 'summary': summary,
                'books': {name: {'equity': values['equity'], 'cash': values['cash'],
                                 'pnl': values['pnl'], 'initial': values['initial'],
                                 'observations': values['observation_count'],
                                 'fills': values['fill_count'], 'since': values['since'],
                                 'latest_session': (sorted(equity[name])[-1]
                                                    if equity[name] else None),
                                 'outcome': values['outcome'],
                                 'holdings': [{'symbol': h['ticker'], 'shares': h['shares'],
                                               'value': h['value']} for h in values['holdings']],
                                 'pending': values['pending']}
                          for name, values in status.items()}}

    # ---------------------------------------------------------------- reading
    def status(self):
        """Read-only payload for the dashboard's research section."""
        latest = self.ledger.latest_session()
        digest = self.ledger.digest()
        advisories = self.ledger.advisories()
        return {'version': VERSION, 'message': self.message, 'paused': self._paused(),
                'cycle': latest, 'digest': digest, 'advisories': advisories,
                # Computed live, so a cycle that died part-way through shows its
                # real divergence instead of the last digest's tidy picture.
                'shadow': self._matched_comparison(),
                'freshness_basis': roles.FRESHNESS_BASIS,
                'counts': self.ledger.counts(), 'history': self.ledger.cycles(limit=20),
                'holds': self.ledger.holds(limit=20),
                'roles': [{'role': role, 'title': title} for role, title in roles.ROLE_TITLES.items()],
                'policy': {'advisory_rule': ADVISORY_RULE, 'depth_policy': DEPTH_POLICY,
                           'scoring': ('No confidence score is produced anywhere in this layer.'),
                           'authority': ('Advisory only. These helpers cannot change a core target, '
                                         'a fill, the accounting or the baseline books, and no live '
                                         'execution route exists.'),
                           'llm': ('Deterministic code. No language model is called, per session or '
                                   'otherwise, so nothing here needs an assistant running.'),
                           'unavailable': [roles.EQUITY_SPREAD_UNAVAILABLE,
                                           roles.EQUITY_DEPTH_UNAVAILABLE],
                           'crypto_products': list(self.crypto_products)}}
