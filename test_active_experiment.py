import tempfile
import threading
import unittest
import json
import re
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
import numpy as np
import pandas as pd
from active_experiment import ActiveExperiment, snapshot
from paper_book import PaperBook, PaperHold
from trading_app import Controller, make_server


def data(now):
    # Candle at now.floor belongs to the still forming bar and must be ignored.
    index=pd.date_range(end=now.floor('15min'),periods=220,freq='15min')
    values=100+np.arange(220)*.2+np.sin(np.arange(220))
    return {t:pd.Series(values*(i+1),index=index) for i,t in enumerate(['BTC-USD','ETH-USD'])}


class ActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=pd.Timestamp('2026-09-13T12:03:00Z')
        self.root=Path(self.tmp.name)
        self.active=ActiveExperiment(self.root/'active')

    def tearDown(self):
        self.tmp.cleanup()

    def test_independent_capital_no_same_bar_fill_and_restart(self):
        core=PaperBook(self.root/'core.sqlite3')
        before=core.path.read_bytes()
        self.active.cycle(data(self.now),self.now)
        first=self.active.book.status()
        self.assertEqual(first['initial'],10000)
        self.assertEqual(first['fill_count'],0)
        self.active.cycle(data(self.now),self.now)
        self.assertEqual(self.active.book.status()['observation_count'],1)
        later=self.now+pd.Timedelta(minutes=15)
        self.active.cycle(data(later),later)
        state=self.active.book.status()
        self.assertGreater(state['fill_count'],0)
        self.assertLess(state['equity'],10000)  # entry costs
        self.assertEqual(core.path.read_bytes(),before)
        restored=ActiveExperiment(self.root/'active')
        restored.cycle(data(later),later)
        self.assertEqual(restored.book.status()['fill_count'],state['fill_count'])
        self.assertEqual(restored.book.status()['observation_count'],2)
        for fill in restored.book.record()['trades']:
            self.assertGreater(pd.Timestamp(fill['fill_bar_end']),pd.Timestamp(fill['signal_observed_at']))

    def test_partial_and_future_bars_not_used(self):
        raw=data(self.now)
        for s in raw.values():s.iloc[-1]=1000000
        snap=snapshot(raw,self.now)
        self.assertEqual(snap['asof'],'2026-09-13T12:00:00+00:00')
        self.assertLess(snap['prices']['BTC-USD'],200)
        self.assertLessEqual(sum(snap['target_weights'].values()),1)
        self.assertTrue(all(w<=.5 for w in snap['target_weights'].values()))

    def test_gap_or_staleness_holds_both_books(self):
        raw=data(self.now)
        raw['BTC-USD']=raw['BTC-USD'].drop(raw['BTC-USD'].index[-5])
        with self.assertRaises(PaperHold):self.active.cycle(raw,self.now)
        with self.assertRaises(PaperHold):self.active.cycle(data(self.now),self.now+pd.Timedelta(hours=1))
        self.assertEqual(self.active.book.status()['observation_count'],0)
        self.assertEqual(self.active.reference.status()['observation_count'],0)

    def test_pause_blocks_fetch_and_persists(self):
        self.active.fetch=lambda: self.fail('Paused account fetched data')
        self.active.pause(True)
        self.assertFalse(self.active.request_cycle())
        self.assertTrue(ActiveExperiment(self.root/'active').book.status()['paused'])

    def test_concurrent_and_repeat_checks_are_throttled(self):
        started=threading.Event();release=threading.Event()
        def fetch():
            started.set();release.wait(3)
            return data(self.now)
        self.active.fetch=fetch
        self.assertTrue(self.active.request_cycle())
        self.assertTrue(started.wait(2))
        self.assertFalse(self.active.request_cycle())
        release.set()
        with self.active.lock:pass
        self.assertFalse(self.active.request_cycle())

    def test_api_authorization_and_pause_controls(self):
        core=PaperBook(self.root/'core.sqlite3')
        server=make_server(Controller(core,active=self.active),port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        opener=build_opener(ProxyHandler({}))
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with opener.open(base) as response:
                token=re.search("const token='([^']+)'",response.read().decode())[1]
            def post(path,value,origin=base,key=token):
                req=Request(base+path,data=json.dumps(value).encode(),headers={
                    'Content-Type':'application/json','X-Paper-Token':key,'Origin':origin})
                with opener.open(req) as response:return json.load(response)
            for origin,key in [('https://untrusted.example',token),(base,'wrong')]:
                with self.assertRaises(HTTPError) as caught:
                    post('/api/active-pause',{'paused':True},origin,key)
                self.assertEqual(caught.exception.code,403)
                caught.exception.close()
            post('/api/active-pause',{'paused':True})
            self.assertTrue(self.active.book.status()['paused'])
            self.assertTrue(self.active.reference.status()['paused'])
            self.assertFalse(post('/api/active-cycle',{})['started'])
            post('/api/pause',{'paused':True})
            with self.assertRaises(HTTPError) as caught:
                post('/api/active-pause',{'paused':False})
            self.assertEqual(caught.exception.code,409);caught.exception.close()
            post('/api/pause',{'paused':False})
            self.assertFalse(self.active.book.status()['paused'])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
