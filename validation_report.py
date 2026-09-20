"""Reproducible corrected accounting and matched-cost benchmark comparisons.

Historical comparisons are retrospective diagnostics, not untouched holdouts.
No optimization and no winner is automatically promoted to a paper allocation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
from execution_model import simulate
from strategy_c import run_allocator, BROAD_UNIVERSE, RISK_OFF_TICKERS
from metrics import compute_metrics, periods_per_year
from market_lab import ASSETS, strategies, target_series, completed_prices, fetch_hourly, continuous_recent


def measured(eq,fills):
    m=compute_metrics(eq)
    return {**{k:float(v) for k,v in m.items()},'fills':len(fills),
            'costs':sum(f['cost'] for f in fills)}


def benchmark(close, target, monthly=False, cost=.0006):
    dates=close.groupby(close.index.to_period('M')).head(1).index if monthly else close.index[:1]
    return simulate(close,{d:target for d in dates},cost_rate=cost)


def daily_comparison(close):
    # A common start leaves 253 observations for stock signals and starts all
    # accounts from the same cash. Terminal positions marked, not liquidated.
    close=close.reindex(close['SPY'].dropna().index)
    start=close.index[252:][0]
    start=close.loc[start:].groupby(close.loc[start:].index.to_period('M')).head(1).index[0]
    eval_close=close.loc[start:]
    rows={};curves={}
    eq,stats,_=run_allocator(close,BROAD_UNIVERSE,risk_off='dynamic',evaluation_start=start)
    rows['Strategy C corrected']={**{k:float(v) for k,v in compute_metrics(eq).items()},**stats}
    curves['Strategy C corrected']=eq
    eq,stats,_=run_allocator(close,BROAD_UNIVERSE,risk_off='dynamic',evaluation_start=start,rebalance_schedule='last')
    label='Strategy C corrected, month-end schedule'
    rows[label]={**{k:float(v) for k,v in compute_metrics(eq).items()},**stats}
    curves[label]=eq
    pool=[t for t in BROAD_UNIVERSE if t in close and close[t].loc[:start].tail(253).notna().all()]
    specs={'SPY buy and hold':({'SPY':1},False),'QQQ buy and hold':({'QQQ':1},False),
           '60/40 SPY AGG monthly':({'SPY':.6,'AGG':.4},True),
           'Same-pool buy and hold (survivor biased)':({t:1/len(pool) for t in pool},False),
           'BIL Treasury bill fund buy and hold':({'BIL':1},False),'Cash, zero interest':({},False)}
    for label,(target,monthly) in specs.items():
        eq,_,fills=benchmark(eval_close,target,monthly)
        rows[label]=measured(eq,fills);curves[label]=eq
    bill_returns=eval_close['BIL'].pct_change(fill_method=None)
    for name,eq in curves.items():
        excess=(eq.pct_change(fill_method=None)-bill_returns).dropna()
        sd=excess.std()
        rows[name]['sharpe_vs_bills']=float(excess.mean()/sd*np.sqrt(periods_per_year(eq.index))) if sd>1e-12 else 0.
    # Additional reporting periods use the same existing curves, never labelled
    # out-of-sample: the strategy and universe have already been researched.
    periods={}
    for label,cut in [('Recent five years',eval_close.index[-1]-pd.DateOffset(years=5)),
                      ('Recent two years',eval_close.index[-1]-pd.DateOffset(years=2))]:
        periods[label]={name:{k:float(v) for k,v in compute_metrics(eq.loc[cut:]).items()} for name,eq in curves.items()}
    return {'start':str(start.date()),'end':str(close.index[-1].date()),'results':rows,'periods':periods,
            'limitations':['Predefined surviving stock pool; no point-in-time/delisting correction claimed.',
              'Adjusted total-return units, fractional shares; taxes and broker-specific constraints excluded.',
              'Signal at first monthly observed close, fill next observed close; no same-bar execution.',
              '6 bps per side; equal cash start, identical calendar and accounting; cash earns zero; BIL is separate.',
              'Recent windows are retrospective diagnostics, NOT untouched holdouts.',
              'First-session monthly planning matches the current paper core; a corrected month-end variant is reported separately. Old headlines used different windows/timing and are not directly comparable.',
              'Invalid held prices fail the run rather than silently forward-fill or liquidate.']},pd.DataFrame(curves)


def hourly_comparison(raw, now):
    rows=[];failures={}
    for symbol,asset in ASSETS.items():
        if symbol not in raw:failures[symbol]='No data';continue
        s=completed_prices(raw[symbol].dropna(),asset['group']=='Crypto',now)
        if len(s)<230:failures[symbol]='Insufficient history';continue
        if not continuous_recent(s,asset['group']=='Crypto',lookback=len(s)):
            failures[symbol]='Missing hourly candles: excluded from historical comparison';continue
        s=s.iloc[:];frame=s.to_frame(symbol);start=s.index[199]
        for strategy in strategies(symbol):
            signals=target_series(s,strategy).loc[start:]
            if signals.isna().any():failures[f'{symbol}/{strategy}']='Missing signal observations';continue
            changes=signals.ne(signals.shift())
            events={d:({symbol:float(w)} if w else {}) for d,w in signals[changes].items()}
            for multiplier in [1,3]:
                cost=asset['cost_bps']/10000*multiplier
                eq,_,fills=simulate(frame.loc[start:],events,cost_rate=cost,initial=1000)
                bh,_,bf=simulate(frame.loc[start:],{start:{symbol:1.}},cost_rate=cost,initial=1000)
                m=measured(eq,fills);b=measured(bh,bf)
                rows.append({'symbol':symbol,'group':asset['group'],'strategy':strategy,
                             'cost_bps':asset['cost_bps']*multiplier,'start':str(start),'end':str(s.index[-1]),
                             'return_pct':m['total_return'],'buy_hold_pct':b['total_return'],
                             'excess_pct':m['total_return']-b['total_return'],
                             'max_drawdown_pct':m['max_drawdown'],'fills':m['fills'],'costs':m['costs'],
                             'status':'Experimental; short retrospective sample; no promotion'})
    return {'results':rows,'unavailable':failures,
            'limitations':['Only 60 days of requested hourly history, with 200-bar warm-up.',
              'Crypto uses Coinbase Exchange public candles; listed stocks/funds use Yahoo adjusted bars. No cross-venue candle blending.',
              'No annualized returns displayed for this short sample.',
              'Long/cash, fractional units, next observed close, no spread/volume order-book simulator.',
              '6 bps ETF / 30 bps crypto per side and 3x stress are assumptions, not verified venue quotes.',
              'Buy-and-hold uses identical start, bars, cash, entry costs and terminal marking.',
              'No parameter search; every candidate reported, including losers.']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=Path('runtime/validation'))
    parser.add_argument('--cached',action='store_true');args=parser.parse_args()
    out=args.out;out.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc)
    if args.cached:
        close=pd.read_csv(out/'daily_prices.csv',index_col=0,parse_dates=True)
        hourly=pd.read_csv(out/'hourly_prices.csv',index_col=0,parse_dates=True)
    else:
        raw=yf.download(sorted(set(BROAD_UNIVERSE+RISK_OFF_TICKERS+['SPY','QQQ','AGG','BIL'])),
                        start='2008-01-01',auto_adjust=True,progress=False,threads=4,timeout=20)
        close=raw['Close'];close.index=pd.to_datetime(close.index).tz_localize(None)
        # Exclude today's daily bar, even if this run happens after the close.
        close=close.loc[close.index<pd.Timestamp(now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date())]
        close.to_csv(out/'daily_prices.csv')
        hourly=fetch_hourly();hourly.to_csv(out/'hourly_prices.csv')
    daily,curves=daily_comparison(close);curves.to_csv(out/'daily_equity.csv')
    print(json.dumps(daily['results'],default=str),flush=True)
    hourly_results=hourly_comparison(hourly,now)
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [out/'daily_prices.csv',out/'hourly_prices.csv']}
    report={'generated_at':now.isoformat(),'version':'accounting-v2/hourly-v1','data_sha256':hashes,
            'code_sha256':{name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                           for name in ['execution_model.py','strategy_c.py','market_lab.py','indicators.py','strategy_config.py','validation_report.py']},
            'daily':daily,'hourly':hourly_results}
    (out/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    pd.DataFrame(hourly_results['results']).to_csv(out/'hourly_comparison.csv',index=False)
    lines=['# Trading validation','',f"Generated {now.isoformat()}",'',
           '## Corrected daily simulation',f"{daily['start']} to {daily['end']}",'',
           '| Account | CAGR | Maximum drawdown | Sharpe above Treasury bills |',
           '|---|---:|---:|---:|']
    for name,m in daily['results'].items():lines.append(f"| {name} | {m['cagr']:.2f}% | {m['max_drawdown']:.2f}% | {m['sharpe_vs_bills']:.2f} |")
    lines+=['']+['- '+x for x in daily['limitations']]+['','## Hourly research','',
        f"{len(hourly_results['results'])} candidate/cost comparisons. Full results in hourly_comparison.csv.",
        'No candidate is promoted on these short historical results. All enabled forward accounts are paper experiments.','']
    lines+=['- '+x for x in hourly_results['limitations']]
    (out/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print(f'Saved {out}/report.json',flush=True)


if __name__=='__main__':main()
