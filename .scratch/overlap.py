"""Do Kalshi and Polymarket list the same events? Cheap feasibility probe."""
import asyncio, json, re
from difflib import SequenceMatcher
from arbbot.venues.kalshi.rest import KalshiRestClient

STOP = {"will","the","be","a","an","in","on","of","to","by","before","after","at","for","is",
        "and","or","who","what","which","this","that","it","2025","2026","2027","2028"}

def toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}

async def kalshi_titles(pages: int = 4) -> list[tuple[str, str]]:
    out, cursor = [], None
    async with KalshiRestClient(requests_per_second=4, max_attempts=2) as c:
        for _ in range(pages):
            p = {"status": "open", "limit": 200}
            if cursor: p["cursor"] = cursor
            b = (await c.fetch("/events", p)).payload
            evs = b.get("events") or []
            if not evs: break
            out += [(str(e.get("event_ticker","")), str(e.get("title",""))) for e in evs]
            cursor = b.get("cursor")
            if not cursor: break
    return out

async def main() -> None:
    pm = json.load(open(".scratch/pm500.json", encoding="utf-8"))
    ks = await kalshi_titles()
    print(f"kalshi events: {len(ks)}   polymarket markets: {len(pm)}")

    pmt = [(m["question"], toks(m["question"]), m) for m in pm]
    hits = []
    for tick, title in ks:
        kt = toks(title)
        if not kt: continue
        for q, qt, m in pmt:
            if not qt: continue
            j = len(kt & qt) / len(kt | qt)
            if j >= 0.34:
                hits.append((j, title, q, tick, m))
    hits.sort(key=lambda h: -h[0])
    print(f"\ncandidate pairs at Jaccard >= 0.34: {len(hits)}\n")
    for j, title, q, tick, m in hits[:20]:
        print(f"  {j:.2f}  K: {title[:60]}")
        print(f"        P: {q[:60]}   bid={m.get('bestBid')} ask={m.get('bestAsk')}")
asyncio.run(main())
