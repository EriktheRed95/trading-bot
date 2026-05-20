"""Stream FNSPID's 23GB news CSV and distill it into a tiny monthly sentiment panel.

FNSPID has no precomputed sentiment and no parquet build, and the file is far too
big to store. So we STREAM it (curl | python), score each headline with VADER on
the fly, and keep only an aggregate: per (symbol, year-month) the mean sentiment
and article count. Output is a few MB and reusable across backtests.

Usage:
    curl -sL <FNSPID news csv url> | python fnspid_sentiment.py [--limit N] [--out FILE]

The Date column is timestamped, so the monthly panel is point-in-time: a backtest
can use month M's sentiment to inform the rebalance at the END of month M (or
later) with no look-ahead.
"""
import csv
import sys
from collections import defaultdict

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

csv.field_size_limit(10_000_000)  # Article field holds full article bodies


def main():
    limit = None
    out = "fnspid_sentiment_monthly.csv"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--limit":
            limit = int(args[i + 1])
        elif a == "--out":
            out = args[i + 1]

    an = SentimentIntensityAnalyzer()
    agg = defaultdict(lambda: [0.0, 0])
    reader = csv.reader(sys.stdin)
    next(reader, None)  # header

    n = 0
    for row in reader:
        if limit and n >= limit:
            break
        n += 1
        if len(row) < 4:
            continue
        date, title, sym = row[1], row[2], row[3]
        if not sym or len(date) < 7 or not title:
            continue
        score = an.polarity_scores(title)["compound"]
        k = (sym.strip().upper(), date[:7])   # year-month
        agg[k][0] += score
        agg[k][1] += 1
        if n % 500_000 == 0:
            print(f"  ...{n:,} rows, {len(agg):,} (symbol,month) buckets", file=sys.stderr)

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "month", "sent_mean", "n"])
        for (sym, ym), (s, c) in sorted(agg.items()):
            w.writerow([sym, ym, round(s / c, 4), c])
    print(f"Read {n:,} rows -> wrote {len(agg):,} (symbol,month) rows to {out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
