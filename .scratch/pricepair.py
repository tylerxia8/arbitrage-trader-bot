"""Price genuinely-corresponding pairs: '2028 US Presidential Election winner'."""
import asyncio, json, re
from decimal import Decimal as D
from arbbot.venues.kalshi.rest import KalshiRestClient

PM = json.load(open(".scratch/pm500.json", encoding="utf-8"))
WINNER = {m["question"]: m for m in PM if "win the 2028 US Presidential Election" in m["question"]}

def name(q): return q.replace("Will ","").split(" win the")[0].strip().lower()
PMBY = {name(q): m for q, m in WINNER.items()}

async def main():
    async with KalshiRestClient(requests_per_second=4, max_attempts=2) as c:
        b = (await c.fetch("/events", {"series_ticker":"KXPRESPERSON","status":"open",
                                       "with_nested_markets":"true","limit":5})).payload
        rows=[]
        for e in b.get("events") or []:
            for m in e.get("markets") or []:
                if m.get("status")!="active": continue
                sub=str(m.get("yes_sub_title") or "").strip()
                pm=PMBY.get(sub.lower())
                if not pm: continue
                p=(await c.fetch_orderbook(str(m["ticker"]))).payload; ob=p.get("orderbook_fp") or p.get("orderbook") or {}
                no=ob.get("no_dollars") or ob.get("no") or []
                yes=ob.get("yes_dollars") or ob.get("yes") or []
                k_ask = (D(1) - max((D(str(l[0])) for l in no), default=D(0))) if no else None
                k_bid = max((D(str(l[0])) for l in yes), default=None) if yes else None
                rows.append((sub, k_bid, k_ask, D(str(pm["bestBid"])), D(str(pm["bestAsk"])),
                             float(pm.get("liquidityNum") or 0)))
        print(f"{'candidate':<24}{'K bid':>8}{'K ask':>8}{'P bid':>8}{'P ask':>8}   {'K_yes+P_no':>11}  liq")
        print("-"*88)
        for sub,kb,ka,pb,pa,liq in rows:
            # buy YES on Kalshi at its ask, buy NO on Polymarket at (1 - P bid)
            cost = (ka + (D(1)-pb)) if ka is not None else None
            c_s = f"{cost:.4f}" if cost is not None else "   n/a"
            print(f"{sub[:23]:<24}{str(kb or '-'):>8}{str(ka or '-'):>8}{pb:>8}{pa:>8}   {c_s:>11}  ${liq:,.0f}")
asyncio.run(main())
