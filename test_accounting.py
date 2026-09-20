"""Accounting invariants, timing, forward/replay parity and hourly data gates."""
from datetime import datetime, timezone
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
import numpy as np
import pandas as pd
from execution_model import rebalance,simulate
from paper_book import PaperBook,PaperHold
from market_lab import completed_prices,continuous_recent,target_series
import market_lab


class AccountingTests(unittest.TestCase):
    def test_two_assets_drift_without_daily_rebalancing(self):
        dates=pd.date_range('2025-01-01',periods=4)
        prices=pd.DataFrame({'A':[100,100,200,100],'B':[100,100,100,200]},index=dates)
        eq,weights,fills=simulate(prices,{dates[0]:{'A':.5,'B':.5}},cost_rate=0)
        self.assertAlmostEqual(eq.iloc[-1],15000)
        self.assertAlmostEqual(weights.iloc[2]['A'],2/3)
        self.assertEqual(len(fills),2)

    def test_signal_cannot_capture_price_jump_before_execution(self):
        dates=pd.date_range('2025-01-01',periods=3)
        eq,_,_=simulate(pd.DataFrame({'A':[10,20,20]},index=dates),{dates[0]:{'A':1}},cost_rate=0)
        self.assertTrue((eq==10000).all())

    def test_both_sides_costed_without_borrowing(self):
        cash,shares,fills=rebalance(10000,{}, {'A':100},{'A':1},.01)
        self.assertAlmostEqual(cash,0,places=7)
        self.assertAlmostEqual(shares['A'],10000/101)
        cash,shares,exits=rebalance(cash,shares,{'A':100},{},.01)
        self.assertAlmostEqual(cash,10000*.99/1.01)
        self.assertEqual(shares,{})
        self.assertGreater(exits[0]['cost'],0)

    def test_turnover_uses_drifted_holdings(self):
        cash,shares,_=rebalance(0,{'A':50,'B':50},{'A':200,'B':100},{'A':.5,'B':.5},0)
        self.assertAlmostEqual(shares['A'],37.5)
        self.assertAlmostEqual(shares['B'],75)

    def test_missing_held_or_entry_price_fails(self):
        d=pd.date_range('2025-01-01',periods=3)
        for values in [[100,np.nan,100],[100,100,np.nan]]:
            with self.assertRaises(ValueError):simulate(pd.DataFrame({'A':values},index=d),{d[0]:{'A':1}})

    def test_paper_and_historical_share_identical_accounting(self):
        d=pd.date_range('2025-01-01',periods=4)
        prices=pd.DataFrame({'A':[100,110,130,120]},index=d)
        eq,_,_=simulate(prices,{d[0]:{'A':1}})
        with tempfile.TemporaryDirectory() as td:
            book=PaperBook(Path(td)/'p.db')
            for i,date in enumerate(d):
                # Each synthetic session is observed after its close; the next
                # session's bar therefore completes after the observation.
                book.cycle({'asof':str(date.date()),'fetched_at':f'{date.date()}T22:00:00+00:00',
                            'prices':{'A':float(prices.iloc[i,0])},'target_weights':{'A':1}})
                self.assertAlmostEqual(book.status()['equity'],eq.iloc[i])
            self.assertEqual(book.status()['observation_count'],4)

    def test_hourly_observation_time_and_restart_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'p.db';book=PaperBook(path,cadence='signal')
            def snap(hour,observed,target=1):
                return {'asof':f'2026-09-11T{hour:02d}:00:00+00:00',
                        'fetched_at':f'2026-09-11T{observed:02d}:05:00+00:00',
                        'prices':{'A':100.},'target_weights':{'A':1.} if target else {}}
            book.cycle(snap(10,11));book.cycle(snap(11,11))
            self.assertEqual(book.status()['fill_count'],0)
            book.cycle(snap(12,12));book=PaperBook(path,cadence='signal');book.cycle(snap(12,12))
            self.assertEqual(book.status()['fill_count'],1)
            book.cycle(snap(13,13,0));book.cycle(snap(14,14,0))
            self.assertEqual(book.status()['fill_count'],2)

    def test_changed_pending_signal_replans_after_fill(self):
        with tempfile.TemporaryDirectory() as td:
            book=PaperBook(Path(td)/'p.db',cadence='signal')
            for hour,target in [(10,1),(11,0),(12,0)]:
                book.cycle({'asof':f'2026-09-11T{hour}:00:00+00:00',
                    'fetched_at':'2026-09-11T11:05:00+00:00' if hour<12 else '2026-09-11T12:05:00+00:00',
                    'prices':{'A':100.},'target_weights':{'A':1} if target else {}})
            self.assertEqual(book.status()['pending']['weights'],{})

    def test_version_change_does_not_mix_forward_records(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'p.db';PaperBook(path,version='v1')
            with self.assertRaises(PaperHold):PaperBook(path,version='v2')

    def test_incomplete_hour_excluded_and_us_last_half_hour(self):
        dates=pd.DatetimeIndex(['2026-09-11T19:30:00Z','2026-09-11T20:30:00Z'])
        s=pd.Series([100,200],index=dates)
        result=completed_prices(s,False,datetime(2026,9,11,20,10,tzinfo=timezone.utc))
        self.assertEqual(len(result),1)
        self.assertEqual(result.index[-1].hour,20)
        self.assertEqual(len(completed_prices(s,True,datetime(2026,9,11,20,tzinfo=timezone.utc))),0)

    def test_early_close_ambiguous_bar_has_conservative_execution_bound(self):
        # 2026-11-27 (EST): start-labelled 9:30..12:30 New York bars. The 12:30
        # bar is labelled as ending 13:30 (18:30Z) but on this early-close day it
        # ended at 13:00 (18:00Z). Availability still waits for the label plus
        # buffer; the execution bound is 13:00, so a 13:10 signal cannot fill on it.
        starts=pd.DatetimeIndex([f'2026-11-27T{h}:30:00Z' for h in ['14','15','16','17']])
        s=pd.Series([100.,101.,102.,103.],index=starts)
        early=completed_prices(s,False,datetime(2026,11,27,18,10,tzinfo=timezone.utc))
        self.assertEqual(early.index[-1],pd.Timestamp('2026-11-27T17:30:00Z'))  # 12:30 bar not yet available
        done=completed_prices(s,False,datetime(2026,11,27,18,40,tzinfo=timezone.utc))
        self.assertEqual(done.index[-1],pd.Timestamp('2026-11-27T18:30:00Z'))  # label preserved
        self.assertEqual(market_lab.execution_bound(done.index[-1],False),pd.Timestamp('2026-11-27T18:00:00Z'))
        # Every other listed bar keeps its label, including the 16:00 session end.
        for label in ['2026-11-27T17:30:00Z','2026-11-30T19:30:00Z','2026-11-30T21:00:00Z']:
            self.assertEqual(market_lab.execution_bound(pd.Timestamp(label),False),pd.Timestamp(label))
        # Crypto candles are never truncated: the same clock time is unchanged.
        self.assertEqual(market_lab.execution_bound(pd.Timestamp('2026-11-27T18:30:00Z'),True),pd.Timestamp('2026-11-27T18:30:00Z'))
        observed=datetime.fromisoformat('2026-11-27T18:10:00+00:00')
        self.assertLess(market_lab.execution_bound(done.index[-1],False),observed)   # would be held
        self.assertGreater(done.index[-1],observed)                                  # label alone would have filled

    def test_lab_early_close_bar_holds_fill_then_regular_session_fills(self):
        # Listed fund with 200+ regular 7-bar sessions, the 4-bar early-close day
        # 2026-11-27, and Monday 2026-11-30. Buy-and-hold target queued at 13:10
        # New York on the early-close day must not fill on the truncated bar.
        sessions=[d for d in pd.bdate_range('2026-10-01','2026-11-25') if d!=pd.Timestamp('2026-11-26')]
        starts=[pd.Timestamp(f'{d.date()}T{h:02d}:30:00',tz='America/New_York') for d in sessions for h in range(9,16)]
        starts+=[pd.Timestamp(f'2026-11-27T{h:02d}:30:00',tz='America/New_York') for h in range(9,13)]
        starts+=[pd.Timestamp(f'2026-11-30T{h:02d}:30:00',tz='America/New_York') for h in range(9,16)]
        index=pd.DatetimeIndex(starts).tz_convert('UTC')
        raw=pd.DataFrame({'SPY':100.+np.arange(len(index))*.01},index=index)
        assets={'SPY':market_lab.ASSETS['SPY']}
        with tempfile.TemporaryDirectory() as td,patch.object(market_lab,'ASSETS',assets):
            lab=market_lab.MarketLab(td);book=lab.books['SPY__buy-hold']
            lab.cycle(raw,datetime(2026,11,27,18,10,tzinfo=timezone.utc))      # 13:10 New York: 11:30 bar is latest
            s=book.status();self.assertEqual(s['snapshot']['asof'],'2026-11-27T17:30:00+00:00')
            self.assertEqual((s['fill_count'],s['pending']['observed_at']),(0,'2026-11-27T18:10:00+00:00'))
            lab.cycle(raw,datetime(2026,11,27,18,40,tzinfo=timezone.utc))      # 12:30 bar available, bound 13:00
            s=book.status();self.assertEqual(s['snapshot']['asof'],'2026-11-27T18:30:00+00:00')
            self.assertEqual(s['snapshot']['bar_end'],'2026-11-27T18:00:00+00:00')
            self.assertEqual(s['fill_count'],0);self.assertIn('fill held',s['outcome'])
            lab.cycle(raw,datetime(2026,11,30,15,40,tzinfo=timezone.utc))      # Monday 9:30 bar, ends 10:30 New York
            s=book.status();self.assertEqual(s['fill_count'],1)
            self.assertEqual(s['trades'][0]['asof'],'2026-11-30T15:30:00+00:00')
            record=book.record(calendar='US regular session')
            self.assertEqual([o['bar_end'] for o in record['observations']],
                             ['2026-11-27T17:30:00+00:00','2026-11-27T18:00:00+00:00','2026-11-30T15:30:00+00:00'])
            self.assertTrue(all(o['bar_end_basis']=='snapshot' for o in record['observations']))
            self.assertEqual(record['trades'][0]['signal_bar_end'],'2026-11-27T17:30:00+00:00')

    def test_crypto_gap_blocks_readiness(self):
        s=pd.Series(100,index=pd.date_range('2026-08-01',periods=202,freq='h',tz='UTC'))
        self.assertTrue(continuous_recent(s,True))
        self.assertFalse(continuous_recent(s.drop(s.index[-4]),True))

    def test_future_data_does_not_change_past_signals(self):
        d=pd.date_range('2025-01-01',periods=240,freq='h',tz='UTC')
        s=pd.Series(100+np.sin(np.arange(240)/4)*10+np.arange(240)*.03,index=d)
        for name in ['trend','legacy-crypto','legacy-currency']:
            a=target_series(s,name);s2=s.copy();s2.iloc[-10:]*=100
            b=target_series(s2,name)
            pd.testing.assert_series_equal(a.iloc[:-10],b.iloc[:-10])

    def test_forward_lab_never_backfills_and_survives_restart(self):
        now=datetime(2026,9,11,20,10,tzinfo=timezone.utc)
        d=pd.date_range(end='2026-09-11T19:00:00Z',periods=240,freq='h')
        raw=pd.DataFrame({'BTC-USD':np.arange(240)+100.},index=d)
        assets={'BTC-USD':market_lab.ASSETS['BTC-USD']}
        with tempfile.TemporaryDirectory() as td,patch.object(market_lab,'ASSETS',assets):
            lab=market_lab.MarketLab(td);lab.cycle(raw,now)
            self.assertTrue(all(r['observations']==1 and r['fills']==0 for r in lab.status()['rows']))
            lab=market_lab.MarketLab(td);lab.cycle(raw,now)
            self.assertTrue(all(r['observations']==1 for r in lab.status()['rows']))
            raw.loc[pd.Timestamp('2026-09-11T20:00:00Z')]=340
            lab.cycle(raw,now.replace(hour=21))
            self.assertEqual(lab.books['BTC-USD__trend'].status()['fill_count'],1)
            lab.pause(True)
            raw.loc[pd.Timestamp('2026-09-11T21:00:00Z')]=341
            lab.cycle(raw,now.replace(hour=22),should_pause=lambda:True)
            self.assertEqual(lab.books['BTC-USD__trend'].status()['observation_count'],2)

    def test_intake_rejects_path_and_direct_derivative_symbols(self):
        with tempfile.TemporaryDirectory() as td,patch.object(market_lab,'ASSETS',{}):
            lab=market_lab.MarketLab(td)
            for symbol in ['../bad','CL=F','EURUSD=X','https://example.com']:
                with self.assertRaises(ValueError):lab.add_asset(symbol,'Individual stocks')
            lab.add_asset('VTI','US equity / sectors')
            self.assertEqual(len(lab.books),2)
            self.assertEqual(lab.books['VTI__trend'].status()['fill_count'],0)
            with self.assertRaises(ValueError):lab.add_asset('BTC','Crypto')

    def test_public_crypto_candles_parse_close_and_page_under_limit(self):
        response=MagicMock();response.json.return_value=[[1789171200,10,30,20,25,12]]
        session=MagicMock();session.get.return_value=response
        with patch.object(market_lab.requests,'Session') as factory:
            factory.return_value.__enter__.return_value=session
            s=market_lab.fetch_crypto('BTC-USD')
        self.assertEqual(s.iloc[-1],25)
        self.assertEqual(session.get.call_count,5)
        for call in session.get.call_args_list:
            params=call.kwargs['params']
            self.assertLessEqual(pd.Timestamp(params['end'])-pd.Timestamp(params['start']),pd.Timedelta(hours=288))

    def test_historical_hourly_gap_is_excluded_not_scored(self):
        import validation_report
        d=pd.date_range('2026-08-01',periods=250,freq='h',tz='UTC').delete(220)
        raw=pd.DataFrame({'BTC-USD':100.},index=d)
        with patch.object(validation_report,'ASSETS',{'BTC-USD':market_lab.ASSETS['BTC-USD']}):
            report=validation_report.hourly_comparison(raw,pd.Timestamp('2026-08-20T00:00:00Z'))
        self.assertEqual(report['results'],[])
        self.assertIn('BTC-USD',report['unavailable'])


if __name__=='__main__':unittest.main()
