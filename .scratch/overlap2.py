"""Match Kalshi *markets* to Polymarket markets. The event level was the wrong unit."""
import asyncio, json, re
from arbbot.venues.kalshi.rest import KalshiRestClient

STOP = {"will","the","be","a","an","in","on","of","to","by","before","after","at","for","is",
        "and","or","who","what","which","this","that","it","win","next","us","u.s"}

def toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}

async def kalshi_markets(pages: int = 5):
    out, cursor = [], None
    async with KalshiRestClient(requests_per_second=4, max_attempts=2) as c:
        for _ in range(pages):
            p = {"status": "open", "limit": 200, "with_nested_markets": "true"}
            if cursor: p["cursor"] = cursor
            b = (await c.fetch("/events", p)).payload
            evs = b.get("events") or []
            if not evs: break
            for e in evs:
                et = str(e.get("title", ""))
                for m in e.get("markets") or []:
                    if m.get("status") != "active": continue
                    sub = str(m.get("yes_sub_title") or m.get("subtitle") or "")
                    out.append((str(m.get("ticker","")), f"{sub} {et}".strip(), sub, et))
            cursor = b.get("cursor")
            if not cursor: break
    return out

async def main() -> None:
    pm = json.load(open(".scratch/pm500.json", encoding="utf-8"))
    ks = await kalshi_markets()
    print(f"kalshi markets: {len(ks)}   polymarket: {len(pm)}")

    pmt = [(m["question"], toks(m["question"]), m) for m in pm]
    hits = []
    for tick, combined, sub, et in ks:
        kt = toks(combined)
        if len(kt) < 3: continue
        for q, qt, m in pmt:
            if len(qt) < 3: continue
            j = len(kt & qt) / len(kt | qt)
            if j >= 0.55:
                hits.append((j, tick, sub, et, q, m))
    hits.sort(key=lambda h: -h[0])
    seen = set()
    print(f"\nmarket-level candidates at Jaccard >= 0.55: {len(hits)}\n")
    for j, tick, sub, et, q, m in hits:
        if tick in seen: continue
        seen.add(tick)
        print(f"  {j:.2f}  K {tick}")
        print(f"        \"{sub}\" / {et[:52]}")
        print(f"        P \"{q[:64]}\"  bid={m.get('bestBid')} ask={m.get('bestAsk')} liq=${float(m.get('liquidityNum') or 0):,.0f}")
        if len(seen) >= 18: break
asyncio.run(main())
