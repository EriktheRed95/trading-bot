"""Fetch a SEC filing (e.g. the 10-K URL printed by fundamentals.py) and print
targeted plain-text excerpts for narrative reading.

WebFetch can't set a User-Agent, so SEC's WAF 403s it. This uses the same clean
"Name email" UA that works for EDGAR, strips the HTML to text, and prints the
Business overview, customer-concentration sentences, and the risk-factor summary
— the parts that matter for a Buffett memo — capped so it stays readable.

Usage:  python read_filing.py <filing_url>
"""
import re
import sys

import requests

UA = {"User-Agent": "EriktheRed95 erik9@gmail.com", "Accept-Encoding": "gzip, deflate"}


def to_text(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&#160;", " ").replace("&nbsp;", " ")
            .replace("&amp;", "&").replace("&#8217;", "'").replace("&#8220;", '"')
            .replace("&#8221;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def section(text, start_pat, end_pats, cap=2800):
    m = re.search(start_pat, text, re.I)
    if not m:
        return None
    start = m.start()
    end = len(text)
    for ep in end_pats:
        em = re.search(ep, text[start + 200:], re.I)
        if em:
            end = min(end, start + 200 + em.start())
    return text[start:end][:cap].strip()


def main(url):
    html = requests.get(url, headers=UA, timeout=60).text
    text = to_text(html)
    print(f"(filing length: {len(text):,} chars)\n")

    biz = section(text, r"Item\s*1\.?\s*Business", [r"Item\s*1A", r"Risk Factors"])
    print("===== BUSINESS (Item 1) =====")
    print(biz or "[not located]")

    print("\n===== CUSTOMER CONCENTRATION =====")
    hits = []
    for m in re.finditer(r"[^.]*?customer[^.]*?\d{1,3}(?:\.\d+)?\s*%[^.]*?\.", text, re.I):
        s = m.group(0).strip()
        if 30 < len(s) < 350:
            hits.append(s)
    for s in hits[:6]:
        print(" -", s)
    if not hits:
        print("[no explicit % customer-concentration sentence found]")

    print("\n===== RISK FACTOR SUMMARY =====")
    rf = section(text, r"Summary of Risk Factors", [r"Item\s*1B", r"Unresolved Staff"], cap=2600) \
        or section(text, r"Item\s*1A\.?\s*Risk Factors", [r"Item\s*1B", r"Item\s*2"], cap=2600)
    print(rf or "[not located]")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python read_filing.py <filing_url>")
        sys.exit(1)
    main(sys.argv[1])
