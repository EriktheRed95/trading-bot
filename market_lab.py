"""Versioned hourly research candidates and explicit, extensible market coverage.

Every account is an independent experiment with virtual capital, not an allocated
piece of the core portfolio. No broker calls, derivatives multipliers or leverage.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from algo_crypto import evaluate_crypto_strategy
from algo_forex import evaluate_forex_strategy
from indicators import calculate_rsi, calculate_macd, calculate_bbands
from paper_book import PaperBook

GROUPS = {
    'Individual stocks':['AAPL','MSFT','NVDA','AMD','AMZN','GOOGL','META','TSLA','JPM','XOM'],
    'US equity / sectors':['SPY','QQQ','IWM','DIA','XLK','XLF','XLE','XLV','XLI','XLP','XLY','XLU','XLB','XLC'],
    'International equity':['EFA','EEM','EWJ','EWZ','FXI','INDA'],
    'Bonds':['AGG','TLT','IEF','SHY','LQD','HYG','TIP'],
    'Commodity funds':['GLD','SLV','DBC','USO','UNG','DBA','CPER'],
    'Real estate':['VNQ','VNQI'],
    'Currency funds':['UUP','FXE','FXB','FXY','FXA','FXC'],
    'Crypto':['BTC-USD','ETH-USD','SOL-USD','XRP-USD','ADA-USD','DOGE-USD','LTC-USD','LINK-USD','AVAX-USD','BCH-USD'],
}
ASSETS = {t:{'symbol':t,'group':g,'cost_bps':30 if g=='Crypto' else 6,
             'instrument':'spot reference' if g=='Crypto' else 'exchange-traded fund',
             'calendar':'24/7' if g=='Crypto' else 'US regular session'} for g,ts in GROUPS.items() for t in ts}
VERSION = 'hourly-v1'
UNSUPPORTED = ['Direct futures (contract rolls, margin and settlement required)',
               'Options (expiry, strike, multiplier and bid/ask chain required)',
               'Direct forex (venue spreads, rollover and funding required)',
               'Illiquid / unlisted securities and other markets without validated data']


def completed_prices(series, crypto, now=None):
    """Yahoo hourly bars are start-labelled. Keep only fully completed bars.

    The last US session bar is 30 minutes. Provider holidays/early closes can
    cause a conservative delay; missing bars are never manufactured or filled.
    """
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError('UTC-aware observation time required')
    s=series.copy().sort_index()
    s.index=pd.to_datetime(s.index,utc=True)
    s=s[~s.index.duplicated(keep='last')]
    ends=s.index+pd.Timedelta(hours=1)
    if not crypto:
        local=s.index.tz_convert('America/New_York')
        valid=(local.dayofweek<5)&((local.hour*60+local.minute)>=570)&((local.hour*60+local.minute)<960)
        s=s.loc[valid];local=local[valid];ends=ends[valid]
        session_end=(local.normalize()+pd.Timedelta(hours=16)).tz_convert('UTC')
        ends=pd.DatetimeIndex([min(a,b) for a,b in zip(ends,session_end)])
    s.index=ends
    s=s.loc[s.index<=now-pd.Timedelta(minutes=5)]
    return s.where(np.isfinite(s)&(s>0))


def execution_bound(end, crypto):
    """Lower bound on when a completed bar actually ended, for the fill guard.

    Availability gating above keeps the inferred label (a bar is used only once
    its labelled end plus buffer has passed). Execution timing is separate: on
    a 13:00 New York early-close day the bar starting 12:30 ends at 13:00 but is
    labelled 13:30, so a signal observed at 13:10 must not fill on it. Without
    a calendar that single ambiguous bar is bounded at 13:00. Bars starting at
    or after 13:00 can only exist on a regular session, and every earlier bar
    ends one hour after its start on either day type, so their labels stand.
    Crypto bars are never truncated. The bound is never later than the label.
    """
    end=pd.Timestamp(end)
    if crypto:
        return end
    local=end.tz_convert('America/New_York')
    if (local.hour,local.minute)==(13,30):
        return (local.normalize()+pd.Timedelta(hours=13)).tz_convert('UTC')
    return end


EXECUTION_BOUND_BASIS=('label, except the 12:30 New York start bar which is bounded at 13:00 '
                       '(possible early close; no exchange calendar available)')


def target_series(series, strategy):
    """Rules frozen at v1. Long or cash; no retrospective parameter search.

    trend: 20/80-hour average crossover. legacy crypto: original SMA200 +
    RSI/MACD score enters >=4, exits <=0. currency mean reversion enters >=4,
    exits <=-4. Legacy candidates have a 24-observed-bar maximum signal hold
    (roughly one crypto day or several exchange sessions), then a cash signal.
    """
    s=series.astype(float)
    result=pd.Series(np.nan,index=s.index)
    valid=s.rolling(200).count().eq(200)
    if strategy=='trend':
        return (s.rolling(20).mean()>s.rolling(80).mean()).astype(float).where(valid)
    # Same RSI/MACD/Bollinger formulas and score thresholds as the legacy
    # modules, calculated once for the whole history rather than per bar.
    rsi=calculate_rsi(s)
    if strategy=='legacy-crypto':
        macd,signal=calculate_macd(s)
        scores=pd.Series(np.where(s<s.rolling(200).mean(),-10,
            2+np.where(rsi<35,3,np.where(rsi>75,-2,0))+np.where(macd>signal,2,np.where(macd<signal,-1,0))),index=s.index)
    elif strategy=='legacy-currency':
        upper,lower=calculate_bbands(s)
        scores=(s<=lower).astype(int)*3-(s>=upper).astype(int)*3+(rsi<30).astype(int)*2-(rsi>70).astype(int)*2
    else:
        raise ValueError('Unknown versioned strategy')
    state=0.;age=0
    for i in range(199,len(s)):
        if not valid.iloc[i]:
            continue
        score=scores.iloc[i]
        age=age+1 if state else 0
        if state and (score <= (0 if strategy=='legacy-crypto' else -4) or age>=24):
            state=0.;age=0
        elif not state and score>=4:
            state=1.;age=0
        result.iloc[i]=state
    return result


def strategies(symbol):
    group=ASSETS[symbol]['group']
    return ['trend']+(['legacy-crypto'] if group=='Crypto' else ['legacy-currency'] if group=='Currency funds' else [])


def continuous_recent(s, crypto, lookback=200):
    recent=s.tail(lookback)
    if recent.isna().any() or len(recent)<200:return False
    gaps=recent.index.to_series().diff().dropna()
    if crypto:return bool((gaps==pd.Timedelta(hours=1)).all())
    local=recent.index.tz_convert('America/New_York')
    # Within a provider session, a missing candle invalidates the recent sample.
    for a,b,la,lb in zip(recent.index[:-1],recent.index[1:],local[:-1],local[1:]):
        if la.date()==lb.date() and b-a>pd.Timedelta(hours=1):return False
    return True


def fetch_hourly(crypto_days=60):
    listed=[t for t,a in ASSETS.items() if a['group']!='Crypto']
    raw=yf.download(listed,period='60d',interval='1h',auto_adjust=True,
                    prepost=False,progress=False,threads=4,timeout=20)
    close=raw['Close'];close.index=pd.to_datetime(close.index,utc=True)
    parts=[close]
    for symbol,asset in ASSETS.items():
        if asset['group']!='Crypto':continue
        try:
            parts.append(fetch_crypto(symbol,days=crypto_days).to_frame(symbol))
        except (requests.RequestException,ValueError,KeyError,TypeError):
            # No cross-venue fallback or invented candle. The lab reports a hold.
            continue
    return pd.concat(parts,axis=1).sort_index()


def fetch_crypto(symbol,days=60):
    """Public Coinbase Exchange candles; no authentication or order endpoints.

    https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles
    Requests stay below 300 buckets. Keep one source per instrument throughout.
    """
    end=pd.Timestamp.now(tz='UTC').floor('h')
    start=end-pd.Timedelta(days=days)
    rows=[]
    with requests.Session() as session:
        while start<end:
            stop=min(start+pd.Timedelta(hours=288),end)
            response=session.get(f'https://api.exchange.coinbase.com/products/{symbol}/candles',
                params={'granularity':3600,'start':start.isoformat(),'end':stop.isoformat()},timeout=20)
            response.raise_for_status();batch=response.json()
            if not isinstance(batch,list):raise ValueError('Invalid public candle response')
            rows.extend(batch);start=stop
    values={pd.Timestamp(row[0],unit='s',tz='UTC'):float(row[4]) for row in rows
            if len(row)>=5 and isinstance(row[0],(int,float))}
    return pd.Series(values,dtype=float).sort_index()


class MarketLab:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.message='Waiting for first hourly observation.'
        intake=self.root/'intake.json'
        if intake.exists():
            for asset in json.loads(intake.read_text(encoding='utf-8')):
                ASSETS[asset['symbol']]=asset
        self.books={}
        self.benchmarks={name:PaperBook(self.root/f'core-benchmark-{name}.sqlite3',
            version='core-benchmark-v2') for name in ['SPY','QQQ','60-40','BIL']}
        for symbol,asset in ASSETS.items():
            for strategy in strategies(symbol)+['buy-hold']:
                key=f'{symbol}__{strategy}'
                self.books[key]=PaperBook(self.root/f'{key}.sqlite3',initial_cash=1000,
                    cadence='signal',cost_rate=asset['cost_bps']/10000,version=VERSION,
                    max_pending_hours=2 if asset['group']=='Crypto' else 96)

    def add_asset(self,symbol,group):
        symbol=symbol.strip().upper()
        if group not in GROUPS or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,19}',symbol):
            raise ValueError('Use a stock, fund or USD crypto symbol and a supported market group.')
        if group=='Crypto' and not symbol.endswith('-USD'):
            raise ValueError('Crypto symbols must use the USD reference, for example BTC-USD.')
        if symbol in ASSETS:
            return 'Already in market intake.'
        asset={'symbol':symbol,'group':group,'cost_bps':30 if group=='Crypto' else 6,
               'instrument':'spot reference' if group=='Crypto' else 'listed stock / fund; user supplied',
               'calendar':'24/7' if group=='Crypto' else 'US regular session'}
        # A new symbol gets fresh independent accounts. It must pass history,
        # freshness and price checks before any forward observation or fill.
        ASSETS[symbol]=asset
        for strategy in strategies(symbol)+['buy-hold']:
            self.books[f'{symbol}__{strategy}']=PaperBook(self.root/f'{symbol}__{strategy}.sqlite3',
                initial_cash=1000,cadence='signal',cost_rate=asset['cost_bps']/10000,
                version=VERSION,max_pending_hours=2 if group=='Crypto' else 96)
        path=self.root/'intake.json'
        rows=json.loads(path.read_text()) if path.exists() else []
        rows.append(asset)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(rows,indent=2),encoding='utf-8');temp.replace(path)
        return f'{symbol} added; awaiting valid market data. Historical validation is not yet included for this symbol.'

    def pause(self, value):
        for book in self.books.values():book.pause(value)
        for book in self.benchmarks.values():book.pause(value)

    def cycle_core(self,snapshot):
        for name,target in {'SPY':{'SPY':1.},'QQQ':{'QQQ':1.},'60-40':{'SPY':.6,'AGG':.4},'BIL':{'BIL':1.}}.items():
            if set(target)<=snapshot['prices'].keys():
                self.benchmarks[name].cycle({**snapshot,'target_weights':target,'strategy':f'benchmark/{name}'})

    def cycle(self, raw=None, now=None, should_pause=lambda:False):
        if raw is None:
            have_history=all((self.root/'observed'/f'{t}.csv').exists() for t,a in ASSETS.items() if a['group']=='Crypto')
            raw=fetch_hourly(crypto_days=12 if have_history else 60)
        now=now or datetime.now(timezone.utc)
        updated=0;held={}
        for symbol,asset in ASSETS.items():
            if should_pause():
                return 'Paused; hourly scan stopped'
            if symbol not in raw:
                held[symbol]='No source data';continue
            # Drop union-calendar rows absent for this instrument before applying
            # indicators. Explicit missing source observations remain unavailable.
            s=completed_prices(raw[symbol].dropna(),asset['group']=='Crypto',now)
            if not continuous_recent(s,asset['group']=='Crypto'):
                held[symbol]='Needs 200 valid completed hourly bars';continue
            if pd.Timestamp(now)-s.index[-1]>pd.Timedelta(minutes=100):
                held[symbol]='Market closed or data stale; no new fills';continue
            # Persist the actual observed source slice; never replay old bars into
            # forward accounts. New signals are registered only at observation time.
            archive=self.root/'observed';archive.mkdir(exist_ok=True)
            history_path=archive/f'{symbol}.csv'
            if history_path.exists():
                old=pd.read_csv(history_path,index_col=0,parse_dates=True).iloc[:,0]
                old.index=pd.to_datetime(old.index,utc=True)
                s=pd.concat([old,s.loc[s.index>old.index[-1]]]).sort_index()
            s.to_csv(history_path)
            for strategy in strategies(symbol)+['buy-hold']:
                if should_pause():
                    return 'Paused; hourly scan stopped'
                target=1. if strategy=='buy-hold' else float(target_series(s,strategy).iloc[-1])
                if not np.isfinite(target):continue
                crypto=asset['group']=='Crypto'
                snapshot={'asof':s.index[-1].isoformat(),'fetched_at':now.isoformat(),
                          'bar_end':execution_bound(s.index[-1],crypto).isoformat(),
                          'bar_end_basis':'crypto candle end' if crypto else EXECUTION_BOUND_BASIS,
                          'strategy':f'{VERSION}/{strategy}','prices':{symbol:float(s.iloc[-1])},
                          'target_weights':{symbol:target} if target else {},
                          'cost_bps':asset['cost_bps'],
                          'source':'Coinbase Exchange public candles' if asset['group']=='Crypto' else 'Yahoo adjusted hourly reference'}
                self.books[f'{symbol}__{strategy}'].cycle(snapshot)
                updated+=1
        self.message=f'{updated} experiment observations processed; {len(held)} assets held.'
        (self.root/'quality.json').write_text(json.dumps({'observed_at':now.isoformat(),'holds':held}),encoding='utf-8')
        return self.message

    def status(self):
        rows=[]
        for key,book in list(self.books.items()):
            symbol,strategy=key.split('__');s=book.status()
            rows.append({'symbol':symbol,'strategy':strategy,'group':ASSETS[symbol]['group'],
                         'equity':s['equity'],'pnl':s['pnl'],'asof':s['snapshot']['asof'] if s['snapshot'] else None,
                         'fills':s['fill_count'],'observations':s['observation_count'],
                         'since':s['since'],'cost_bps':ASSETS[symbol]['cost_bps'],
                         'position':'Long' if s['holdings'] else 'Cash','pending':bool(s['pending'])})
        path=self.root/'quality.json'
        return {'version':VERSION,'message':self.message,'assets':list(ASSETS.values()),'rows':rows,
                'core_benchmarks':[{ 'name':name,**{k:s[k] for k in ['equity','pnl','since','observation_count','fill_count']}}
                                   for name,book in self.benchmarks.items() for s in [book.status()]],
                'unsupported':UNSUPPORTED,'quality':json.loads(path.read_text()) if path.exists() else {}}
