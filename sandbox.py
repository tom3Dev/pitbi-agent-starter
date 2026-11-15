"""Test your bot's order logic on a simulated Decibel exchange: no keys, no money.

Pitbi's `sandbox` MCP tool replays recorded Decibel prices and order books with Decibel's
rules: tick and lot size, minimum size, max leverage, post-only and IOC violations,
reduce-only trimming, margin, liquidation, maker/taker fees, hourly funding. Orders are only
acknowledged when placed and fill (or cancel) on the next step, like on Decibel. Nothing is
sent to Decibel. No registration.

    pip install -r requirements.txt
    python sandbox.py

Replace the steps in `run()` with your bot's own logic. Every rule is listed at
https://pitbi.app/sandbox
"""

import asyncio
import os
from datetime import datetime, timezone

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("PITBI_MCP_URL", "https://pitbi.app/mcp")


async def call(pitbi: ClientSession, **args: object) -> dict:
    """One sandbox action. A rejected action comes back as an error with Decibel's reason."""
    r = await pitbi.call_tool("sandbox", args)
    if r.structured_content is None:
        text = " ".join(getattr(c, "text", "") for c in r.content)
        return {"error": text}
    return r.structured_content


async def run(pitbi: ClientSession) -> None:
    s = await call(pitbi, action="start", start_hours_ago=2)
    sid = s["session_id"]
    clock = datetime.fromtimestamp(s["clock_ms"] / 1000, timezone.utc)
    print(f"session started, clock at {clock:%Y-%m-%d %H:%M} UTC, cash {s['cash']}")

    # 0) read the market at the session clock: mark, the book a taker meets, tick and lot
    #    (action="market" without market= lists every market with its rules)
    m = await call(pitbi, action="market", session_id=sid, market="BTC")
    book, rules = m["book"], m["rules"]
    print(f"BTC mark {m['mark']}, bid {book['best_bid']} / ask {book['best_ask']} "
          f"({book['priced_by']['buy']}), tick {rules['tick_size']}, lot {rules['lot_size']}, "
          f"a post-only buy rests at {book['post_only_max_buy']} or lower")

    # 1) a market buy is only acknowledged now...
    r = await call(pitbi, action="place", session_id=sid, market="BTC", side="buy",
                   size=0.01, time_in_force="market", client_order_id="entry-1")
    print("placed:", r["result"]["status"])  # acknowledged, not filled yet

    # 2) ...and fills on the next step, on the recorded book
    r = await call(pitbi, action="step", session_id=sid, minutes=1)
    for e in r.get("events", []):
        print("event:", e["kind"], e.get("price"), "priced by", e.get("priced_by"))

    # 3) a classic live bug: the retry that sends the same order twice
    r = await call(pitbi, action="place", session_id=sid, market="BTC", side="buy",
                   size=0.01, time_in_force="market", client_order_id="entry-1")
    print("same client_order_id again:", r.get("error") or r["result"]["status"])

    # 4) let an hour pass: funding, fills and liquidations show up as events
    r = await call(pitbi, action="step", session_id=sid, minutes=60)
    print("events in the next hour:", [e["kind"] for e in r.get("events", [])])

    st = await call(pitbi, action="state", session_id=sid)
    for pos in st["positions"]:
        print(f"{pos['market']}: {pos['size']} at {pos['entry']}, mark {pos['mark']}, "
              f"unrealized {pos['unrealized_pnl']}")
    print("equity:", st["equity"], "| fees:", st["fees_paid"], "| funding:", st["funding_paid"])
    print("read-only page of this session:", st["view_url"])


async def main() -> None:
    async with streamable_http_client(URL) as (read, write):
        async with ClientSession(read, write) as pitbi:
            await pitbi.initialize()
            await run(pitbi)


if __name__ == "__main__":
    asyncio.run(main())
