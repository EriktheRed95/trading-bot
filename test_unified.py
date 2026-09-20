"""Offline integration checks; synthetic market data and disposable paper books."""
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import numpy as np
import pandas as pd
import trading_engine as engine
from paper_book import PaperBook, PaperHold
import paper_book
from trading_app import Controller, make_server, research_payload
import build_dashboard

NOW=datetime(2026,9,11,22,tzinfo=timezone.utc)

def prices():
    dates=pd.bdate_range(end='2026-09-11',periods=300)
    x=np.arange(len(dates))
    return pd.DataFrame({t:100*np.exp(x*(.001+i*.00001)+.015*np.sin(x/(3+i%7)))
                         for i,t in enumerate(set(engine.BROAD_UNIVERSE+list(engine.RISK_OFF_TICKERS)+['SPY']))},index=dates)

def snapshot(date='2026-09-10', weights=None, values=None, observed=None):
    # Synthetic sessions are observed shortly after their 16:00 New York close,
    # so a later-session fill is physically possible. Omitting fetched_at would
    # record wall-clock time, and every synthetic past bar would be held.
    return {'asof':date,'fetched_at':observed or f'{date}T20:30:00+00:00','prices':values or {'AAA':100.,'BBB':80.},
            'target_weights':{'AAA':1.} if weights is None else weights}


class SignalTests(unittest.TestCase):
    def test_complete_snapshot_has_finite_weights_and_prices(self):
        s=engine.signal_snapshot(prices(),now=NOW)
        self.assertEqual(s['asof'],'2026-09-11')
        self.assertAlmostEqual(sum(s['target_weights'].values()),1)
        self.assertTrue(all(math.isfinite(h['price']) and math.isfinite(h['weight']) for h in s['holdings']))

    def test_missing_latest_symbol_is_excluded_not_forward_filled(self):
        p=prices();ticker=engine.BROAD_UNIVERSE[0];p.loc[p.index[-1],ticker]=np.nan
        s=engine.signal_snapshot(p,now=NOW)
        self.assertIn(ticker,s['excluded']);self.assertNotIn(ticker,s['target_weights'])

    def test_partial_day_is_not_used(self):
        s=engine.signal_snapshot(prices(),now=NOW.replace(hour=15))
        self.assertEqual(s['asof'],'2026-09-10')

    def test_snapshot_carries_bar_end_and_observation_time(self):
        s=engine.signal_snapshot(prices(),now=NOW)
        # Fail-closed session close: 13:00 New York (17:00Z in September).
        self.assertEqual(s['bar_end'],'2026-09-11T17:00:00+00:00')
        self.assertEqual(s['bar_end_basis'],paper_book.SESSION_CLOSE_BASIS)
        self.assertEqual(s['fetched_at'],NOW.isoformat())
        self.assertEqual(paper_book.bar_end(s),datetime(2026,9,11,17,tzinfo=timezone.utc))
        self.assertEqual(paper_book.bar_end({'asof':s['asof']}),paper_book.bar_end(s))

    def test_large_universe_outage_holds(self):
        p=prices();p.loc[p.index[-1],engine.BROAD_UNIVERSE[:25]]=np.nan
        with self.assertRaises(engine.DataUnavailable):engine.signal_snapshot(p,now=NOW)

    def test_stale_market_holds(self):
        with self.assertRaises(engine.DataUnavailable):engine.signal_snapshot(prices().iloc[:-10],now=NOW)

    def test_missing_defensive_source_holds(self):
        p=prices();p['SPY']=np.linspace(400,100,len(p));p=p.drop(columns=engine.RISK_OFF_TICKERS[0])
        with self.assertRaises(engine.DataUnavailable):engine.signal_snapshot(p,now=NOW)

    def test_defensive_cash_target(self):
        p=prices()
        for t in ['SPY',*engine.RISK_OFF_TICKERS]:p[t]=np.linspace(400,100,len(p))
        s=engine.signal_snapshot(p,now=NOW)
        self.assertFalse(s['risk_on']);self.assertEqual(s['target_weights'],{})

    def test_static_report_uses_same_snapshot(self):
        with patch.object(engine,'signal_snapshot',return_value={'asof':'2026-09-11','eligible':12,'holdings':[], 'risk_on':True}):
            self.assertEqual(build_dashboard.current_portfolio()['asof'],'2026-09-11')

    def test_missing_macro_does_not_crash_render(self):
        live={'series':{}}
        text=build_dashboard.render_macro({'_live':live,'_macro':build_dashboard.macro_reading(live)})
        self.assertEqual(text.count('Unavailable'),3)

    def test_existing_research_panels_remain_available(self):
        panels=research_payload()
        self.assertTrue(any('Strategy C' in p['title'] for p in panels))
        self.assertTrue(any(p['tier']==2 for p in panels))
        json.dumps(panels,allow_nan=False)


class BookTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'paper.db';self.book=PaperBook(self.path)
    def tearDown(self):self.temp.cleanup()

    def test_signal_does_not_fill_same_session(self):
        self.book.cycle(snapshot());self.book.cycle(snapshot())
        self.assertEqual(self.book.status()['trades'],[])
        self.assertIsNotNone(self.book.status()['pending'])

    def test_later_close_fill_charges_cost_and_preserves_cash(self):
        self.book.cycle(snapshot());self.book.cycle(snapshot('2026-09-11',values={'AAA':110.,'BBB':80.}))
        s=self.book.status();self.assertEqual(len(s['trades']),1)
        self.assertEqual(s['trades'][0]['price'],110);self.assertGreater(s['trades'][0]['cost'],0)
        self.assertGreaterEqual(s['cash'],0);self.assertLess(s['equity'],10000)

    def test_restart_same_session_does_not_duplicate(self):
        self.book.cycle(snapshot());self.book.cycle(snapshot('2026-09-11'))
        other=PaperBook(self.path);other.cycle(snapshot('2026-09-11'))
        self.assertEqual(len(other.status()['trades']),1)

    def test_expired_targets_are_replaced_without_filling(self):
        self.book.cycle(snapshot('2026-08-03'))
        self.book.cycle(snapshot('2026-09-11',weights={'BBB':1.},values={'BBB':80.}))
        s=self.book.status();self.assertEqual(s['trades'],[])
        self.assertEqual(s['pending']['signal_date'],'2026-09-11')
        self.assertEqual(s['pending']['weights'],{'BBB':1.})

    def test_holdings_are_fixed_shares_between_rebalances(self):
        self.book.cycle(snapshot());self.book.cycle(snapshot('2026-09-11'))
        before=self.book.status();self.book.cycle(snapshot('2026-09-14',values={'AAA':120.,'BBB':80.}))
        after=self.book.status();self.assertEqual(before['holdings'][0]['shares'],after['holdings'][0]['shares'])
        self.assertGreater(after['equity'],before['equity']);self.assertEqual(len(after['trades']),1)

    def test_missing_held_price_rolls_back(self):
        self.book.cycle(snapshot());self.book.cycle(snapshot('2026-09-11'))
        with self.assertRaises(PaperHold):self.book.cycle(snapshot('2026-09-14',values={'BBB':80.}))
        self.assertEqual(self.book.status()['snapshot']['asof'],'2026-09-11')

    def test_invalid_weights_refused(self):
        for weights in [{'AAA':float('nan')},{'AAA':-1},{'AAA':2}]:
            with self.assertRaises(PaperHold):self.book.cycle(snapshot(weights=weights))

    def test_pause_persists_and_blocks_cycle(self):
        self.book.pause(True);other=PaperBook(self.path)
        self.assertEqual(other.cycle(snapshot()),'Paused');self.assertIsNone(other.status()['snapshot'])

    def test_two_process_connections_only_fill_once(self):
        self.book.cycle(snapshot());errors=[]
        def run():
            try:PaperBook(self.path).cycle(snapshot('2026-09-11'))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[]);self.assertEqual(len(self.book.status()['trades']),1)

    def test_controller_does_not_start_without_request(self):
        calls=[];controller=Controller(self.book,lambda:calls.append(True))
        self.assertEqual(calls,[]);self.assertFalse(controller.status()['busy'])

    def test_controller_blocks_overlapping_requests_and_pause_during_fetch(self):
        started=threading.Event();release=threading.Event()
        def fetch():started.set();release.wait(3);return snapshot()
        controller=Controller(self.book,fetch);self.assertTrue(controller.request_cycle(True));started.wait(3)
        self.assertFalse(controller.request_cycle(True));self.book.pause(True);release.set()
        with controller.lock:pass
        self.assertIsNone(self.book.status()['snapshot'])

    def test_record_api_passes_hourly_calendar_for_gap_flags(self):
        import market_lab
        assets={k:market_lab.ASSETS[k] for k in ['BTC-USD','SPY']}
        with patch.object(market_lab,'ASSETS',assets):
            lab=market_lab.MarketLab(Path(self.temp.name)/'lab')
            # Friday 16:00 New York bar, then Monday 10:30 New York bar: a normal
            # weekend for a listed fund, but unobserved hours for 24/7 crypto.
            for asof,observed in [('2026-10-02T20:00:00+00:00','2026-10-02T20:05:00+00:00'),
                                  ('2026-10-05T14:30:00+00:00','2026-10-05T14:35:00+00:00')]:
                for symbol in assets:
                    lab.books[f'{symbol}__buy-hold'].cycle({'asof':asof,'fetched_at':observed,'prices':{symbol:100.},'target_weights':{}})
            server=make_server(Controller(self.book,lab=lab),0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                get=lambda account:json.load(urlopen(base+'/api/record?account='+account))
                btc=get('BTC-USD__buy-hold');spy=get('SPY__buy-hold')
                self.assertEqual(btc['provenance']['calendar'],'24/7')
                self.assertEqual(spy['provenance']['calendar'],'US regular session')
                self.assertEqual([o['unobserved_bars_possible'] for o in btc['observations']],[None,True])
                self.assertEqual([o['unobserved_bars_possible'] for o in spy['observations']],[None,False])
                self.assertEqual(btc['provenance']['gap_count'],1);self.assertEqual(spy['provenance']['gap_count'],0)
                self.assertEqual(get('core')['provenance']['calendar'],'US regular session')
                self.assertEqual(get('benchmark-SPY')['provenance']['calendar'],'US regular session')
                with self.assertRaises(HTTPError) as raised:urlopen(base+'/api/record?account=nope')
                self.assertEqual(raised.exception.code,404)
            finally:server.shutdown();server.server_close();thread.join()

    def test_http_rejects_unauthorized_write_and_bad_host(self):
        server=make_server(Controller(self.book),0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            html=urlopen(base).read().decode();self.assertIn('Trading control center',html)
            req=Request(base+'/api/pause',data=b'{"paused":true}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as raised:urlopen(req)
            self.assertEqual(raised.exception.code,403)
            token=html.split("const token='")[1].split("'")[0]
            req.add_header('X-Paper-Token',token)
            self.assertTrue(json.load(urlopen(req))['paused'])
            bad=Request(base+'/api/status',headers={'Host':'evil.example'})
            with self.assertRaises(HTTPError):urlopen(bad)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
