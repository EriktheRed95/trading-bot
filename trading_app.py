"""One local dashboard and paper bot. An in-process background collector drives
automatic cycles; the browser only displays state and sends manual requests."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import webbrowser
from urllib.parse import urlsplit, parse_qs
from paper_book import PaperBook, PaperHold
from trading_engine import signal_snapshot, DataUnavailable

ROOT = Path(__file__).resolve().parent


def research_payload():
    """Keep the existing research panels inside the control center, without orders."""
    from build_dashboard import build_panels, macro_reading
    def read(name):
        path=ROOT/name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    live={'series':{}}
    panels=build_panels(live,macro_reading(live),read('overnight_results.json'),
                        tips=read('tip_intake.json'),theses=read('thesis_intake.json'))
    keys={'tier','title','subtitle','body','rules','metrics','metric_note','robust','caveat',
          'readings','source','killer','backtest_path','prior'}
    return [{k:v for k,v in p.items() if k in keys} for p in panels]


class Controller:
    def __init__(self, book, fetch=signal_snapshot, lab=None, desk=None, active=None, stocks=None):
        self.book, self.fetch = book, fetch
        self.lab = lab
        # Optional research companion. With desk=None every code path below is
        # byte-for-byte the previous behaviour, which the tests assert.
        self.desk = desk
        self.active = active
        self.stocks = stocks
        # lock guards the core family (daily book, core benchmarks, research
        # helpers); hourly_lock guards the hourly lab and its market intake. The
        # background collector acquires these same locks, so a dashboard request
        # and a scheduled check can never run the same family at once.
        self.lock = threading.Lock()
        self.hourly_lock = threading.Lock()
        self.collector = None
        self.last_started = None
        self.last_check = None
        self.message = 'Waiting for the first core check.'

    def paused(self):
        return self.book.is_paused()

    def request_cycle(self, force=False):
        if self.paused():
            self.message = 'Paused; no market fetch or paper execution.'
            return False
        if self.collector:
            # Same due rules and locks as the scheduler: an open page's request
            # never duplicates background work. force is an explicit Check now.
            source = 'manual' if force else 'browser'
            families = ['core','hourly'] + (['active','stocks_5m','stocks_15m'] if force else [])
            started = [self.collector.request(name, source, force=force)[0]
                       for name in families if name in self.collector.families]
            return any(started)
        if not self.lock.acquire(blocking=False):
            return False
        if not force and self.last_started is not None and time.monotonic()-self.last_started < 300:
            self.lock.release()
            return False
        self.last_started = time.monotonic()
        def work():
            try:
                self.run_core()
            finally:
                self.lock.release()
            if self.hourly_lock.acquire(blocking=False):
                try:
                    self.run_hourly()
                finally:
                    self.hourly_lock.release()
        threading.Thread(target=work,daemon=True).start()
        return True

    def run_core(self):
        """One core pass for a caller holding self.lock. Returns (kind, message).

        kind is ok, partial (core accepted but a benchmark/helper step failed),
        held (data rejected; nothing recorded) or error.
        """
        snapshot=None
        # A snapshot the core REJECTED is not an observation. The desk sees it
        # only once book.cycle has returned without raising, which includes the
        # normal 'Already processed this session' de-duplication.
        core_accepted=False
        kind='ok'
        try:
            self.message = 'Checking completed market sessions…'
            snapshot=self.fetch()
            self.message = self.book.cycle(snapshot)
            core_accepted=True
            if self.lab and not self.paused():
                self.lab.cycle_core(snapshot)
        except (DataUnavailable,PaperHold) as exc:
            kind='partial' if core_accepted else 'held'
            self.message = str(exc)
            self.book.record_error(self.message)
        except Exception as exc:
            kind='partial' if core_accepted else 'error'
            self.message = f'Market refresh failed ({type(exc).__name__}); existing paper state preserved.'
            self.book.record_error(self.message)
        # The research companion observes last and is fully advisory: it
        # runs after every core and benchmark decision is already
        # committed, and a failure here cannot reach that state.
        if self.desk and not self.paused():
            try:
                if core_accepted:
                    self.desk.observe(snapshot)
                else:
                    self.desk.message=('Held: the frozen strategy did not accept a snapshot '
                                       'this cycle, so the helpers recorded nothing.')
            except Exception as exc:
                kind='partial' if kind=='ok' else kind
                self.desk.message=f'Research helpers held ({type(exc).__name__}); previous advisories preserved.'
        self.last_check = datetime.now(timezone.utc).isoformat()
        return kind, self.message

    def run_hourly(self, should_pause=None):
        """One hourly-lab pass for a caller holding self.hourly_lock."""
        should_pause = should_pause or self.paused
        if not self.lab:
            return 'skipped', 'Hourly lab not enabled.'
        if should_pause():
            return 'paused', 'Paused; hourly scan not started'
        try:
            self.lab.message = 'Checking hourly markets…'
            message = self.lab.cycle(should_pause=should_pause)
            return ('paused' if message.startswith('Paused') else 'ok'), message
        except Exception as exc:
            self.lab.message=f'Hourly refresh held ({type(exc).__name__}); previous records preserved.'
            return 'error', self.lab.message

    def status(self):
        return {**self.book.status(),'busy':self.lock.locked(), 'message':self.message,
                'last_check':self.last_check,
                'collector':self.collector is not None}


def make_server(controller, port=8791):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def reply(self, code, payload, content_type='application/json'):
            data = payload.encode() if isinstance(payload,str) else json.dumps(payload,allow_nan=False).encode()
            self.send_response(code)
            self.send_header('Content-Type',content_type+'; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors http://127.0.0.1:8790 http://localhost:8790; base-uri 'none'")
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403,{'error':'Local host required'})
            if self.path == '/':
                return self.reply(200,(ROOT/'trading_ui.html').read_text(encoding='utf-8').replace('__TOKEN__',token),'text/html')
            if self.path == '/api/stock-experiments':
                return self.reply(200,controller.stocks.status() if controller.stocks else {'enabled':False})
            if self.path == '/api/active':
                return self.reply(200,controller.active.status() if controller.active else {'enabled':False})
            if self.path == '/api/status':
                return self.reply(200,controller.status())
            if self.path == '/api/collector':
                # Read-only collection health; never starts a check.
                return self.reply(200,controller.collector.status() if controller.collector else
                                  {'enabled':False,'mode':'PAPER ONLY','pid':os.getpid(),
                                   'message':'Background collector is disabled in this server process; '
                                             'only Check now requests run.'})
            if self.path == '/api/research':
                return self.reply(200,research_payload())
            if self.path == '/api/lab':
                return self.reply(200,controller.lab.status() if controller.lab else {'rows':[],'assets':[]})
            if self.path == '/api/research-desk':
                # Read-only advisory record. It carries no positions or orders.
                return self.reply(200,controller.desk.status() if controller.desk
                                  else {'enabled':False,'message':'Research helpers are not enabled.'})
            if self.path.startswith('/api/record?'):
                key=parse_qs(urlsplit(self.path).query).get('account',['core'])[0]
                if key.startswith('stocks:') and controller.stocks:
                    try:
                        return self.reply(200,controller.stocks.record(key.removeprefix('stocks:')))
                    except (KeyError,ValueError):
                        return self.reply(404,{'error':'Unknown stock experiment'})
                books={'core':controller.book}
                if controller.active:
                    books.update({'active':controller.active.book,'active-reference':controller.active.reference})
                if controller.lab:
                    books.update(controller.lab.books)
                    books.update({'benchmark-'+k:b for k,b in controller.lab.benchmarks.items()})
                if key not in books:
                    return self.reply(404,{'error':'Unknown account'})
                # Hourly experiments declare their calendar in the market intake
                # metadata; it drives the record's unobserved-bar flags. Core and
                # benchmark books are daily and use the book's default.
                calendar=None
                if controller.lab and key in controller.lab.books:
                    from market_lab import ASSETS
                    calendar=ASSETS.get(key.split('__')[0],{}).get('calendar')
                return self.reply(200,books[key].record(calendar=calendar))
            if self.path == '/api/validation':
                path=ROOT/'runtime'/'validation'/'report.json'
                return self.reply(200,json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'pending':True})
            return self.reply(404,{'error':'Not found'})

        def do_POST(self):
            origin = self.headers.get('Origin')
            allowed = {f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}
            if not self.host_ok() or (origin and origin not in allowed) or self.headers.get('X-Paper-Token') != token:
                return self.reply(403,{'error':'Local dashboard authorization required'})
            if self.headers.get('Content-Type') != 'application/json':
                return self.reply(415,{'error':'JSON required'})
            try:
                n = int(self.headers.get('Content-Length','0'))
                if not 0 < n <= 1024:
                    raise ValueError()
                payload = json.loads(self.rfile.read(n))
                if not isinstance(payload,dict):
                    raise ValueError()
            except (ValueError,TypeError):
                return self.reply(400,{'error':'Invalid request'})
            if self.path == '/api/stock-cycle' and controller.stocks:
                if controller.book.status()['paused']:
                    return self.reply(202,{'started':False})
                if controller.collector:
                    results={m:controller.collector.request(m,'manual') for m in ('stocks_5m','stocks_15m')}
                    return self.reply(202,{'started':any(r[0] for r in results.values()),
                                           'reasons':{m:r[1] for m,r in results.items()}})
                return self.reply(202,{'started':controller.stocks.request_cycle(should_pause=lambda:controller.book.status()['paused'])})
            if self.path == '/api/stock-pause' and controller.stocks:
                if type(payload.get('paused')) is not bool:
                    return self.reply(400,{'error':'paused must be boolean'})
                if not payload['paused'] and controller.book.status()['paused']:
                    return self.reply(409,{'error':'Resume the global paper bot first.'})
                controller.stocks.pause(payload['paused'])
                return self.reply(200,controller.stocks.status())
            if self.path == '/api/active-cycle' and controller.active:
                if controller.collector:
                    started,reason=controller.collector.request('active','manual')
                    return self.reply(202,{'started':started,'reason':reason})
                return self.reply(202,{'started':controller.active.request_cycle()})
            if self.path == '/api/active-pause' and controller.active:
                if type(payload.get('paused')) is not bool:
                    return self.reply(400,{'error':'paused must be boolean'})
                if not payload['paused'] and controller.book.status()['paused']:
                    return self.reply(409,{'error':'Resume the global paper bot first.'})
                controller.active.pause(payload['paused'])
                return self.reply(200,controller.active.status())
            if self.path == '/api/cycle':
                return self.reply(202,{'started':bool(controller.request_cycle(payload.get('force') is True))})
            if self.path == '/api/intake' and controller.lab:
                if not isinstance(payload.get('symbol'),str) or not isinstance(payload.get('group'),str):
                    return self.reply(400,{'error':'Symbol and market group required'})
                if not controller.hourly_lock.acquire(blocking=False):
                    return self.reply(409,{'error':'Wait for the current market check to finish.'})
                try:
                    result=controller.lab.add_asset(payload['symbol'],payload['group'])
                    controller.lab.pause(controller.book.status()['paused'])
                    return self.reply(200,{'message':result})
                except ValueError as exc:
                    return self.reply(400,{'error':str(exc)})
                finally:
                    controller.hourly_lock.release()
            if self.path == '/api/pause':
                if type(payload.get('paused')) is not bool:
                    return self.reply(400,{'error':'paused must be boolean'})
                controller.book.pause(payload['paused'])
                if controller.active:
                    controller.active.pause(payload['paused'])
                if controller.stocks:
                    controller.stocks.pause(payload['paused'])
                if controller.lab:
                    controller.lab.pause(payload['paused'])
                if controller.desk:
                    controller.desk.pause(payload['paused'])
                return self.reply(200,controller.status())
            return self.reply(404,{'error':'Not found'})
    return PaperServer(('127.0.0.1',port),Handler)


class PaperServer(ThreadingHTTPServer):
    """Loopback server that refuses to share its port.

    http.server enables SO_REUSEADDR, which on Windows lets a second process bind
    the same port. Exclusive binding makes a duplicate server fail instead.
    """
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _log_to_files(runtime):
    """pythonw (the hidden scheduled launcher) has no console streams.

    Separate files from Start-Trading.ps1's redirected server.log, which another
    process may hold open.
    """
    if sys.stdout is None or sys.stderr is None:
        runtime.mkdir(parents=True, exist_ok=True)
        for name, file in (('stdout', 'service.log'), ('stderr', 'service-errors.log')):
            try:
                stream = open(runtime/file, 'a', encoding='utf-8', buffering=1)
            except OSError:
                stream = open(os.devnull, 'w', encoding='utf-8')
            setattr(sys, name, stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8791)
    parser.add_argument('--state',type=Path,default=ROOT/'runtime'/'paper.sqlite3')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--once',action='store_true',help='Run one paper cycle without opening the dashboard')
    parser.add_argument('--no-research-helpers',action='store_true',
                        help='Run without the advisory research companion layer')
    parser.add_argument('--no-scheduler',action='store_true',
                        help='Serve the dashboard without background collection (manual checks only)')
    parser.add_argument('--restart-hung-after',type=float,default=0,metavar='MINUTES',
                        help='Exit (code 75) when a collection request has not returned for this long, '
                             'so the scheduled task can start a fresh process. 0 disables.')
    args = parser.parse_args()
    runtime = args.state.parent
    _log_to_files(runtime)
    if args.once:
        print(PaperBook(args.state).cycle(signal_snapshot()))
        return
    from collector import Collector, InstanceLock
    instance = InstanceLock(runtime/'collector.lock')
    if not instance.acquire():
        # Exit 0 before opening any database: another server already owns this
        # runtime. Nothing is killed.
        print(f'{datetime.now(timezone.utc).isoformat()} another paper server already owns {runtime}; exiting',
              file=sys.stderr, flush=True)
        return
    book = PaperBook(args.state)
    from market_lab import MarketLab
    lab=MarketLab(args.state.parent/'hourly-v1')
    lab.pause(book.status()['paused'])
    desk=None
    if not args.no_research_helpers:
        from research_desk import ResearchDesk
        desk=ResearchDesk(args.state.parent/'research-v1')
        desk.pause(book.status()['paused'])
    from active_experiment import ActiveExperiment
    active=ActiveExperiment(args.state.parent/'active-15m-v1')
    if book.status()['paused']:
        active.pause(True)
    from stock_experiments import StockExperiments
    stocks=StockExperiments(args.state.parent/'stock-experiments-v1')
    if book.status()['paused']:
        stocks.pause(True)
    controller = Controller(book,lab=lab,desk=desk,active=active,stocks=stocks)
    try:
        server = make_server(controller,args.port)
    except OSError as exc:
        instance.release()
        print(f'{datetime.now(timezone.utc).isoformat()} port {args.port} unavailable ({exc}); '
              'not starting a second server', file=sys.stderr, flush=True)
        sys.exit(4)
    hung_exit = []
    if not args.no_scheduler:
        def on_hung(reason):
            hung_exit.append(reason)
            threading.Thread(target=server.shutdown, daemon=True).start()
        controller.collector = Collector(controller, runtime/'collector.sqlite3',
                                         hung_exit_minutes=args.restart_hung_after, on_hung_exit=on_hung)
        controller.collector.start()
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'PAPER ONLY — dashboard and bot: {url} (pid {os.getpid()}, background collector '
          f'{"off" if args.no_scheduler else "on"})',flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if controller.collector:
            controller.collector.stop(timeout=10, reason=hung_exit[0] if hung_exit else 'server stopped')
        server.server_close()
        instance.release()
    if hung_exit:
        print(f'{datetime.now(timezone.utc).isoformat()} exiting for restart: {hung_exit[0]}',
              file=sys.stderr, flush=True)
        # A stuck worker thread may still be blocked in a network call.
        os._exit(75)


if __name__ == '__main__':
    main()
