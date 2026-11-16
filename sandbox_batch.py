"""A bot loop on the sandbox with one call per decision: the batched step.

Besides moving the clock, one `step` call can cancel orders (cancel_ids), place new ones
(orders, each with its own result in `placed`), return the books you watch at the new clock
(watch, depth) and stop at the end of the first minute with a fill, cancel, TP/SL or
liquidation instead of running all its `minutes` (until_event). A quoting bot then needs one
call per decision instead of cancel + cancel + place + place + market + step.

    pip install -r requirements.txt
    python sandbox_batch.py

This one quotes BTC on both sides with post-only orders a few bps behind the best bid and
ask (rounded to the tick, away from the book), requotes after every event or at most every
30 minutes, and stops quoting the side that would grow the position past a cap. It shows
the calls; it is not a strategy. Every rule is listed at https://pitbi.app/sandbox
"""

import asyncio
import math
import os
from datetime import datetime, timezone
from decimal import Decimal

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("PITBI_MCP_URL", "https://pitbi.app/mcp")
MARKET = "BTC"
BEHIND_BPS = 10.0  # how far behind the best bid and ask the quotes rest
DECISIONS = 12  # steps in the loop
MAX_MINUTES = 30  # the most one step moves the clock: it stops earlier at the first event
START_HOURS_AGO = 8  # more than DECISIONS x MAX_MINUTES (6 h): the loop stays in recorded data

calls = 0


async def call(pitbi: ClientSession, **args: object) -> dict:
    """One sandbox action. A rejected action comes back as an error with Decibel's reason."""
    global calls
    calls += 1
    r = await pitbi.call_tool("sandbox", args)
    if r.structured_content is None:
        text = " ".join(getattr(c, "text", "") for c in r.content)
        return {"error": text}
    return r.structured_content


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def on_tick(price: float, tick: float, down: bool) -> float:
    """Decibel rejects a price off the tick: round down for a bid, up for an ask, and keep
    only the tick's decimals (64263.100000000006 is off the tick too)."""
    n = math.floor(price / tick + 1e-9) if down else math.ceil(price / tick - 1e-9)
    decimals = max(0, -int(Decimal(str(tick)).as_tuple().exponent))
    return round(n * tick, decimals)


def quotes(book: dict, rules: dict, position: float, size: float, cap: float, n: int) -> list:
    """A post-only bid and ask BEHIND_BPS behind the best prices; no side past the cap."""
    out = []
    tick, behind = rules["tick_size"], BEHIND_BPS / 10_000
    if book.get("best_bid") and position + size <= cap + size / 2:
        out.append({"market": MARKET, "side": "buy", "size": size,
                    "price": on_tick(book["best_bid"] * (1 - behind), tick, down=True),
                    "time_in_force": "post_only", "client_order_id": f"bid-{n}"})
    if book.get("best_ask") and position - size >= -cap - size / 2:
        out.append({"market": MARKET, "side": "sell", "size": size,
                    "price": on_tick(book["best_ask"] * (1 + behind), tick, down=False),
                    "time_in_force": "post_only", "client_order_id": f"ask-{n}"})
    return out


def describe(events: list[dict]) -> str:
    seen = []
    for e in events:
        if e["kind"] == "fill":
            role = "maker" if e.get("maker") else "taker"
            seen.append(f"{e['side']} {e['size']:g} at {e['price']} ({role}, {e['priced_by']})")
        elif e["kind"] == "cancelled":
            seen.append(f"order {e['order_id']} cancelled: {e['reason']}")
        else:  # funding, take_profit, stop_loss, margin_call, liquidation
            seen.append(e["kind"])
    return "; ".join(seen) or "nothing"


async def run(pitbi: ClientSession) -> None:
    s = await call(pitbi, action="start", start_hours_ago=START_HOURS_AGO)
    sid, clock = s["session_id"], s["clock_ms"]

    # the rules and the first book; after this, every step brings the book back by itself
    m = await call(pitbi, action="market", session_id=sid, market=MARKET)
    rules, book = m["rules"], m["book"]
    size = max(rules["min_size"], rules["lot_size"])  # the smallest order Decibel accepts
    cap = 3 * size
    print(f"clock {utc(clock)} UTC: quoting {size:g} {MARKET} a side, {BEHIND_BPS:g} bps "
          f"behind the best prices, tick {rules['tick_size']:g}, position cap {cap:g}")

    open_ids: list[int] = []
    position = 0.0
    last = s
    for n in range(1, DECISIONS + 1):
        # ONE call: cancel the old quotes, place the new ones, move the clock until
        # something happens (MAX_MINUTES at most) and read the book at the new clock
        r = await call(pitbi, action="step", session_id=sid, cancel_ids=open_ids,
                       orders=quotes(book, rules, position, size, cap, n),
                       watch=[MARKET], depth=1, minutes=MAX_MINUTES, until_event=True)
        if "error" in r:  # e.g. the end of the recorded data: nothing changed
            print("stopped:", r["error"])
            break
        # a quote that filled before its cancel arrived comes back in cancelled with ok
        # false: that is normal. A rejected order says why in placed[...]["error"].
        for p in r.get("placed", []):
            if not p["ok"]:
                print(f"    order {p['index']} rejected: {p['error']}")
        last = r
        moved = (r["clock_ms"] - clock) // 60_000
        clock, book = r["clock_ms"], r["books"][MARKET]
        open_ids = [o["id"] for o in r["open_orders"]]
        position = round(sum(p["size"] for p in r["positions"] if p["market"] == MARKET), 12)
        stop = "stopped at the event" if moved < MAX_MINUTES else "full step"
        print(f"{n:2}. {utc(clock)} +{moved:2} min ({stop}): {describe(r['events'])}"
              f" | position {position:g}")

    print(f"\nequity {last.get('equity')} | realized {last.get('realized_pnl')} | "
          f"fees {last.get('fees_paid')} | funding {last.get('funding_paid')}")
    print(f"{calls} calls in all; one action per call would have taken about "
          f"{2 + 6 * DECISIONS} (cancel, cancel, place, place, market, step).")
    print("read-only page of this session:", last.get("view_url"))


async def main() -> None:
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as pitbi:
            await pitbi.initialize()
            await run(pitbi)


if __name__ == "__main__":
    asyncio.run(main())
