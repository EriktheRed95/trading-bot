"""Actual-observation-time fill guard for every cadence, and record provenance.

Synthetic snapshots and disposable SQLite books only. No market data, no
runtime databases, no services.
"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import paper_book
from paper_book import PaperBook, HOLD_MESSAGE, UNKNOWN_MESSAGE, possible_unobserved_bars
import market_lab


def daily(asof, observed, price=100., weights=None):
    """A completed daily session (16:00 New York close) observed at `observed` UTC."""
    return {'asof':asof,'fetched_at':observed,'prices':{'AAA':price},
            'target_weights':{'AAA':1.} if weights is None else weights}


def hourly(asof, observed, price=100., target=1.):
    return {'asof':asof,'fetched_at':observed,'prices':{'X':price},'target_weights':{'X':target} if target else {}}


class DailyObservationGuardTests(unittest.TestCase):
    """Scenario: the dashboard is opened on 2026-10-02 after the close, but the
    provider has not yet published the 2026-10-02 bar. The snapshot's latest
    completed bar is 2026-10-01 and it starts the October plan. Later the same
    evening the provider publishes 2026-10-02, whose 16:00 close preceded the
    observation of the signal. That bar must not fill."""

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'core.db'
    def tearDown(self):self.temp.cleanup()

    def stale_signal_then_delayed_bar(self, book):
        out=book.cycle(daily('2026-10-01','2026-10-02T20:20:00+00:00',price=100.))
        self.assertIn('queued targets',out)
        self.assertEqual(book.status()['pending']['observed_at'],'2026-10-02T20:20:00+00:00')
        return book.cycle(daily('2026-10-02','2026-10-02T21:30:00+00:00',price=90.))

    def test_delayed_provider_bar_cannot_fill_before_signal_observed(self):
        book=PaperBook(self.path)
        out=self.stale_signal_then_delayed_bar(book)
        self.assertIn(HOLD_MESSAGE,out)
        s=book.status()
        self.assertEqual(s['fill_count'],0);self.assertEqual(s['observation_count'],2)
        self.assertEqual(s['pending']['signal_date'],'2026-10-01')
        self.assertEqual(s['cash'],10000.)
        # The next session completes after the observation and fills normally.
        out=book.cycle(daily('2026-10-05','2026-10-05T20:20:00+00:00',price=95.))
        self.assertEqual(out,'Filled prior targets at this observed session close')
        s=book.status();self.assertEqual(s['fill_count'],1)
        trade=s['trades'][0]
        self.assertEqual((trade['asof'],trade['signal_date'],trade['price']),('2026-10-05','2026-10-01',95.))
        record=book.record();fill=record['trades'][0]
        self.assertEqual(fill['signal_observed_at'],'2026-10-02T20:20:00+00:00')
        self.assertEqual(fill['observed_at'],'2026-10-05T20:20:00+00:00')
        self.assertEqual(fill['fill_bar_end'],'2026-10-05T17:00:00+00:00')  # 13:00 New York, fail-closed
        self.assertEqual([o['outcome'] for o in record['observations']][1],'Marked existing holdings; '+HOLD_MESSAGE)

    def test_hold_persists_across_restart(self):
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T20:20:00+00:00'))
        reopened=PaperBook(self.path)
        self.assertIn(HOLD_MESSAGE,reopened.cycle(daily('2026-10-02','2026-10-02T21:30:00+00:00',price=90.)))
        self.assertEqual(reopened.status()['fill_count'],0)
        again=PaperBook(self.path)
        self.assertEqual(again.cycle(daily('2026-10-05','2026-10-05T20:20:00+00:00')),'Filled prior targets at this observed session close')
        self.assertEqual(PaperBook(self.path).status()['fill_count'],1)

    def test_ordinary_next_session_fill_is_unchanged(self):
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-01T20:20:00+00:00'))
        self.assertEqual(book.cycle(daily('2026-10-02','2026-10-02T20:20:00+00:00',price=101.)),
                         'Filled prior targets at this observed session close')
        self.assertEqual(book.status()['trades'][0]['price'],101.)

    def test_observation_before_earliest_close_fills_at_that_days_close(self):
        # Signal seen at 11:00 New York on 10-02 (the 10-02 bar is withheld until
        # 16:15, so the snapshot is 10-01). Every possible 10-02 close, early or
        # regular, is later than the observation, so the same-day fill is valid.
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T15:00:00+00:00'))
        self.assertEqual(book.cycle(daily('2026-10-02','2026-10-02T20:20:00+00:00')),
                         'Filled prior targets at this observed session close')

    def test_observation_after_earliest_close_waits_for_next_session(self):
        # Signal seen at 14:00 New York. Without session-close metadata the book
        # cannot know whether 10-02 closed at 13:00 or 16:00, so it fails closed:
        # the fill waits for the next completed session instead of assuming 16:00.
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T18:00:00+00:00'))
        self.assertIn(HOLD_MESSAGE,book.cycle(daily('2026-10-02','2026-10-02T20:20:00+00:00')))
        self.assertEqual(book.cycle(daily('2026-10-05','2026-10-05T20:20:00+00:00')),
                         'Filled prior targets at this observed session close')

    def test_explicit_bar_end_can_only_tighten_the_check(self):
        book=PaperBook(self.path)
        book.cycle({**daily('2026-10-01','2026-10-02T20:20:00+00:00'),'bar_end':'2026-10-01T17:00:00+00:00'})
        held={**daily('2026-10-02','2026-10-02T21:30:00+00:00'),'bar_end':'2026-10-02T17:00:00+00:00'}
        self.assertIn(HOLD_MESSAGE,book.cycle(held))
        # An earlier explicit completion time is accepted (stricter than derived).
        early={**daily('2026-10-05','2026-10-05T20:20:00+00:00'),'bar_end':'2026-10-05T16:30:00+00:00'}
        self.assertEqual(book.cycle(early),'Filled prior targets at this observed session close')

    def test_untrusted_bar_end_cannot_authorize_a_fill(self):
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T20:20:00+00:00'))
        before=book.status()
        # Later than the bar can have completed (claims a 16:00 or 17:30 close).
        for stated in ['2026-10-02T20:00:00+00:00','2026-10-02T21:30:00+00:00','2026-10-03T00:00:00+00:00']:
            with self.assertRaises(paper_book.PaperHold):
                book.cycle({**daily('2026-10-02','2026-10-02T21:30:00+00:00'),'bar_end':stated})
        # Malformed values.
        for stated in ['yesterday','','2026-13-45T00:00:00',123,{'t':1}]:
            with self.assertRaises(paper_book.PaperHold):
                book.cycle({**daily('2026-10-02','2026-10-02T21:30:00+00:00'),'bar_end':stated})
        after=book.status()
        self.assertEqual((after['fill_count'],after['observation_count'],after['snapshot']['asof']),
                         (0,before['observation_count'],'2026-10-01'))
        # Hourly: a bar_end later than the bar-end label is rejected too.
        hourly_book=PaperBook(Path(self.temp.name)/'h.db',cadence='signal',initial_cash=1000)
        hourly_book.cycle(hourly('2026-09-11T14:00:00+00:00','2026-09-11T15:10:00+00:00'))
        with self.assertRaises(paper_book.PaperHold):
            hourly_book.cycle({**hourly('2026-09-11T15:00:00+00:00','2026-09-11T15:20:00+00:00'),'bar_end':'2026-09-11T16:00:00+00:00'})
        self.assertEqual(hourly_book.status()['fill_count'],0)

    def test_bar_claimed_complete_after_its_own_observation_is_held(self):
        # A snapshot whose bar cannot have completed by the time it was fetched
        # is inconsistent; nothing is recorded.
        book=PaperBook(self.path)
        with self.assertRaises(paper_book.PaperHold):
            book.cycle(daily('2026-10-05','2026-10-05T15:00:00+00:00'))  # 11:00 New York, before 13:00
        self.assertEqual(book.status()['observation_count'],0)
        with self.assertRaises(paper_book.PaperHold):
            book.cycle({**daily('2026-10-05','2026-10-05T20:00:00+00:00'),'fetched_at':'not a time'})

    def test_guard_holds_until_expiry_then_requeues_without_filling(self):
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T20:20:00+00:00'))
        self.assertIn(HOLD_MESSAGE,book.cycle(daily('2026-10-02','2026-10-02T21:30:00+00:00')))
        out=book.cycle(daily('2026-10-12','2026-10-12T20:20:00+00:00',weights={'AAA':.5}))
        self.assertTrue(out.startswith('Expired old targets without filling'))
        s=book.status();self.assertEqual(s['fill_count'],0)
        self.assertEqual(s['pending']['signal_date'],'2026-10-12');self.assertEqual(s['pending']['weights'],{'AAA':.5})

    def test_snapshot_without_fetched_at_records_wall_clock_and_guards(self):
        # Legacy callers that omit fetched_at get the actual cycle time recorded.
        # A past bar observed now cannot be a bar that closed before now.
        book=PaperBook(self.path)
        book.cycle({'asof':'2020-01-02','prices':{'AAA':100.},'target_weights':{'AAA':1.}})
        observed=book.status()['pending']['observed_at']
        self.assertGreater(datetime.fromisoformat(observed),datetime(2026,1,1,tzinfo=timezone.utc))
        self.assertIn(HOLD_MESSAGE,book.cycle({'asof':'2020-01-03','prices':{'AAA':100.},'target_weights':{'AAA':1.}}))
        self.assertEqual(book.status()['fill_count'],0)

    def test_legacy_pending_without_any_observation_time_fails_closed_then_recovers(self):
        # Backwards compatibility: a book written before observation times existed
        # has a pending row but no observation and no fetched_at. The account
        # still opens, but the unknowable signal cannot fill. It is held until it
        # expires, then re-planned at an observed bar with provenance, which fills
        # on the following session.
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T20:20:00+00:00'))
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('DELETE FROM observations')
            snap=json.loads(c.execute('SELECT snapshot FROM cycles').fetchone()[0]);snap.pop('fetched_at')
            c.execute('UPDATE cycles SET snapshot=?',(json.dumps(snap),))
        reopened=PaperBook(self.path)
        self.assertIsNone(reopened.status()['pending']['observed_at'])
        out=reopened.cycle(daily('2026-10-02','2026-10-02T21:30:00+00:00'))
        self.assertIn(UNKNOWN_MESSAGE,out)
        self.assertEqual(reopened.status()['fill_count'],0)
        self.assertIn(UNKNOWN_MESSAGE,reopened.cycle(daily('2026-10-05','2026-10-05T20:20:00+00:00')))
        # Past the seven-day expiry the stale signal is dropped and re-planned.
        out=reopened.cycle(daily('2026-10-09','2026-10-09T20:20:00+00:00'))
        self.assertTrue(out.startswith('Expired old targets without filling'));self.assertIn('queued targets',out)
        s=reopened.status();self.assertEqual(s['fill_count'],0)
        self.assertEqual((s['pending']['signal_date'],s['pending']['observed_at']),('2026-10-09','2026-10-09T20:20:00+00:00'))
        self.assertEqual(reopened.cycle(daily('2026-10-12','2026-10-12T20:20:00+00:00',price=97.)),
                         'Filled prior targets at this observed session close')
        self.assertEqual(reopened.status()['trades'][0]['signal_date'],'2026-10-09')

    def test_cycles_snapshot_fetched_at_is_fallback_when_observation_row_missing(self):
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T20:20:00+00:00'))
        with closing(sqlite3.connect(self.path)) as c,c:c.execute('DELETE FROM observations')
        self.assertIn(HOLD_MESSAGE,PaperBook(self.path).cycle(daily('2026-10-02','2026-10-02T21:30:00+00:00')))

    def test_naive_observation_timestamps_are_treated_as_utc(self):
        book=PaperBook(self.path)
        book.cycle(daily('2026-10-01','2026-10-02T20:20:00'))
        self.assertIn(HOLD_MESSAGE,book.cycle(daily('2026-10-02','2026-10-02T21:30:00')))
        self.assertEqual(book.cycle(daily('2026-10-05','2026-10-05T20:20:00')),'Filled prior targets at this observed session close')

    def test_existing_configuration_hash_still_opens(self):
        # The settings JSON compared on open is unchanged, so books created by
        # the previous version keep opening without a new version label.
        PaperBook(self.path)
        with closing(sqlite3.connect(self.path)) as c:
            settings=json.loads(c.execute('SELECT settings FROM configuration').fetchone()[0])
        self.assertEqual(sorted(settings),['cadence','cost_rate','max_pending_hours','version'])
        PaperBook(self.path)


class EarlyCloseTests(unittest.TestCase):
    """2026-11-27, the Friday after Thanksgiving, is a 13:00 New York early
    close (18:00Z in November). The book has no calendar, so every daily bar is
    treated as complete at 13:00 New York; that is exactly right on this day and
    conservative on ordinary days."""

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'core.db'
    def tearDown(self):self.temp.cleanup()

    def test_early_close_delayed_provider_bar_cannot_fill(self):
        book=PaperBook(self.path)
        # 11-26 is a holiday. The 11-25 bar is seen on Friday at 14:00 New York
        # (19:00Z), after the 13:00 early close, and queues the plan.
        book.cycle(daily('2026-11-25','2026-11-27T19:00:00+00:00',price=100.))
        # The provider publishes the 11-27 bar at 16:20 New York. A 16:00
        # assumption would have filled it: 21:00Z is later than 19:00Z.
        assumed_16=datetime(2026,11,27,16,tzinfo=paper_book.NEW_YORK)
        self.assertGreater(assumed_16,datetime.fromisoformat('2026-11-27T19:00:00+00:00'))
        out=book.cycle(daily('2026-11-27','2026-11-27T21:20:00+00:00',price=90.))
        self.assertIn(HOLD_MESSAGE,out);self.assertEqual(book.status()['fill_count'],0)
        self.assertEqual(paper_book.bar_end({'asof':'2026-11-27'}).isoformat(),'2026-11-27T18:00:00+00:00')
        # The next session (Monday) completed after the observation and fills.
        self.assertEqual(book.cycle(daily('2026-11-30','2026-11-30T21:20:00+00:00',price=95.)),
                         'Filled prior targets at this observed session close')
        trade=book.status()['trades'][0]
        self.assertEqual((trade['asof'],trade['signal_date'],trade['price']),('2026-11-30','2026-11-25',95.))

    def test_signal_observed_before_early_close_fills_that_day(self):
        # Seen at 12:00 New York (17:00Z) on the early-close day: even the 13:00
        # close is later than the observation, so filling at 11-27 is valid.
        book=PaperBook(self.path)
        book.cycle(daily('2026-11-25','2026-11-27T17:00:00+00:00'))
        self.assertEqual(book.cycle(daily('2026-11-27','2026-11-27T21:20:00+00:00',price=90.)),
                         'Filled prior targets at this observed session close')
        self.assertEqual(book.status()['trades'][0]['price'],90.)

    def test_valid_later_session_fill_after_early_close_day(self):
        # Signal from the early-close bar itself, observed after 16:15 New York,
        # fills at the next session as usual.
        book=PaperBook(self.path)
        book.cycle(daily('2026-11-27','2026-11-27T21:20:00+00:00'))
        self.assertEqual(book.cycle(daily('2026-11-30','2026-11-30T21:20:00+00:00',price=101.)),
                         'Filled prior targets at this observed session close')
        self.assertEqual(book.status()['trades'][0]['asof'],'2026-11-30')


class BenchmarkAndHourlyTests(unittest.TestCase):
    def test_core_benchmarks_follow_the_same_guard(self):
        with tempfile.TemporaryDirectory() as td,patch.object(market_lab,'ASSETS',{}):
            lab=market_lab.MarketLab(td)
            core=lambda asof,observed,spy:{'asof':asof,'fetched_at':observed,'bar_end':f'{asof}T17:00:00+00:00',
                                           'prices':{'SPY':spy,'QQQ':300.,'AGG':100.,'BIL':91.},'target_weights':{}}
            lab.cycle_core(core('2026-10-01','2026-10-02T20:20:00+00:00',500.))
            lab.cycle_core(core('2026-10-02','2026-10-02T21:30:00+00:00',450.))
            self.assertTrue(all(b.status()['fill_count']==0 for b in lab.benchmarks.values()))
            lab.cycle_core(core('2026-10-05','2026-10-05T20:20:00+00:00',480.))
            self.assertTrue(all(b.status()['fill_count']>=1 for b in lab.benchmarks.values()))
            self.assertEqual(lab.benchmarks['SPY'].status()['trades'][0]['price'],480.)

    def test_hourly_guard_behaviour_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            book=PaperBook(Path(td)/'h.db',cadence='signal',initial_cash=1000,max_pending_hours=96)
            book.cycle(hourly('2026-09-11T14:00:00+00:00','2026-09-11T15:10:00+00:00'))
            # Delayed provider publishes the 15:00 bar at 15:20; it closed before 15:10.
            self.assertIn(HOLD_MESSAGE,book.cycle(hourly('2026-09-11T15:00:00+00:00','2026-09-11T15:20:00+00:00')))
            self.assertEqual(book.cycle(hourly('2026-09-11T16:00:00+00:00','2026-09-11T16:05:00+00:00',price=101.)),
                             'Filled prior targets at this observed session close')
            self.assertEqual(book.status()['trades'][0]['price'],101.)
            # Cash signal fills on the following completed bar as before.
            book.cycle(hourly('2026-09-11T17:00:00+00:00','2026-09-11T17:05:00+00:00',target=0))
            book.cycle(hourly('2026-09-11T18:00:00+00:00','2026-09-11T18:05:00+00:00',target=0))
            self.assertEqual(book.status()['fill_count'],2)


class RecordProvenanceTests(unittest.TestCase):
    def test_daily_record_flags_gaps_and_keeps_net_move_across_them(self):
        with tempfile.TemporaryDirectory() as td:
            book=PaperBook(Path(td)/'core.db')
            book.cycle(daily('2026-10-01','2026-10-01T20:20:00+00:00',price=100.))
            book.cycle(daily('2026-10-02','2026-10-02T20:20:00+00:00',price=100.))  # fill
            book.cycle(daily('2026-10-05','2026-10-05T20:20:00+00:00',price=110.))  # Monday, no missed session
            book.cycle(daily('2026-10-13','2026-10-13T20:20:00+00:00',price=121.))  # six sessions unobserved
            record=book.record()
            obs=record['observations']
            self.assertEqual([o['unobserved_bars_possible'] for o in obs],[None,False,False,True])
            self.assertEqual([o['bar_gap_hours'] for o in obs],[None,24.,72.,192.])
            self.assertEqual([o['observation_gap_hours'] for o in obs],[None,24.,72.,192.])
            self.assertEqual(len(record['gaps']),1)
            gap=record['gaps'][0]
            self.assertEqual((gap['from_asof'],gap['to_asof']),('2026-10-05','2026-10-13'))
            shares=book.status()['holdings'][0]['shares']
            self.assertAlmostEqual(gap['equity_change'],shares*(121.-110.))
            self.assertAlmostEqual(obs[-1]['equity'],book.status()['cash']+shares*121.)
            self.assertEqual(record['provenance']['gap_count'],1)
            self.assertEqual(record['provenance']['calendar'],'US regular session')
            self.assertIn('Filled prior targets',obs[1]['outcome'])
            self.assertEqual(record['trades'][0]['signal_observed_at'],'2026-10-01T20:20:00+00:00')

    def test_hourly_gap_flags_depend_on_declared_calendar(self):
        with tempfile.TemporaryDirectory() as td:
            book=PaperBook(Path(td)/'h.db',cadence='signal',initial_cash=1000)
            # Friday 15:00 and 16:00 New York (EDT = UTC-4), then Monday 10:30 and 12:30.
            for asof in ['2026-10-02T19:00:00+00:00','2026-10-02T20:00:00+00:00',
                         '2026-10-05T14:30:00+00:00','2026-10-05T16:30:00+00:00']:
                book.cycle(hourly(asof,asof.replace('00:00+','05:00+'),target=0))
            flags=lambda calendar:[o['unobserved_bars_possible'] for o in book.record(calendar=calendar)['observations']]
            self.assertEqual(flags('US regular session'),[None,False,False,True])
            self.assertEqual(flags('24/7'),[None,False,True,True])
            self.assertEqual(flags(None),[None,None,None,None])
            self.assertEqual(book.record(calendar='24/7')['provenance']['gap_count'],2)

    def test_gap_heuristic_direct_cases(self):
        d=lambda asof:{'asof':asof}
        self.assertFalse(possible_unobserved_bars(d('2026-10-02'),d('2026-10-05'),'US regular session'))
        self.assertTrue(possible_unobserved_bars(d('2026-10-02'),d('2026-10-06'),'US regular session'))
        self.assertFalse(possible_unobserved_bars(d('2026-10-05T14:00:00+00:00'),d('2026-10-05T15:00:00+00:00'),'24/7'))
        self.assertTrue(possible_unobserved_bars(d('2026-10-05T14:00:00+00:00'),d('2026-10-05T16:00:00+00:00'),'24/7'))
        self.assertIsNone(possible_unobserved_bars(d('2026-10-05T14:00:00+00:00'),d('2026-10-05T16:00:00+00:00'),None))

    def test_record_preserves_the_execution_bound_actually_used(self):
        with tempfile.TemporaryDirectory() as td:
            book=PaperBook(Path(td)/'core.db')
            book.cycle({**daily('2026-10-01','2026-10-01T20:20:00+00:00'),'bar_end':'2026-10-01T16:45:00+00:00'})
            book.cycle(daily('2026-10-02','2026-10-02T20:20:00+00:00'))                       # fill, label-derived bound
            book.cycle({**daily('2026-10-05','2026-10-05T20:20:00+00:00'),'bar_end':'2026-10-05T16:15:00+00:00'})
            record=book.record()
            self.assertEqual([(o['bar_end'],o['bar_end_basis']) for o in record['observations']],
                             [('2026-10-01T16:45:00+00:00','snapshot'),('2026-10-02T17:00:00+00:00','label'),
                              ('2026-10-05T16:15:00+00:00','snapshot')])
            self.assertEqual((record['trades'][0]['fill_bar_end'],record['trades'][0]['signal_bar_end']),
                             ('2026-10-02T17:00:00+00:00','2026-10-01T16:45:00+00:00'))
            # Rows stored before the bound was persisted are reconstructed and say so.
            with closing(sqlite3.connect(Path(td)/'core.db')) as c,c:
                snap=json.loads(c.execute("SELECT snapshot FROM cycles WHERE asof='2026-10-05'").fetchone()[0])
                snap.pop('execution_bar_end');snap.pop('execution_bar_end_basis')
                c.execute("UPDATE cycles SET snapshot=? WHERE asof='2026-10-05'",(json.dumps(snap),))
            last=book.record()['observations'][-1]
            self.assertEqual((last['bar_end'],last['bar_end_basis']),('2026-10-05T16:15:00+00:00','reconstructed'))
            self.assertIn('does not prove bars existed',book.record()['provenance']['note'])

    def test_record_keeps_existing_fields_for_the_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            book=PaperBook(Path(td)/'core.db')
            book.cycle(daily('2026-10-01','2026-10-01T20:20:00+00:00'))
            book.cycle(daily('2026-10-02','2026-10-02T20:20:00+00:00'))
            record=book.record()
            self.assertTrue({'asof','observed_at','equity','cash'}<=set(record['observations'][0]))
            self.assertTrue({'asof','signal_date','ticker','shares','price','cost'}<=set(record['trades'][0]))
            json.dumps(record,allow_nan=False)


if __name__=='__main__':unittest.main()
