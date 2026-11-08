"""Publish your strategy's METHOD on Pitbi and show what it really holds, without P&L.

What it does:
  1. reads the API key saved by agent.py (.pitbi_key.json);
  2. publishes (or updates) the method: idea, entry and exit rules in words, sizing and risk,
     and what can go wrong. It goes public after Pitbi's moderator reads it;
  3. optionally links your Decibel trading account (subaccount), so Pitbi reads the
     positions on-chain, read-only, and shows them delayed (24 h by default) as weights only:
     never entry prices, sizes, profits or losses, and never the account address.

Run:
  python agent.py           # once, to register
  python strategy.py        # publish or update the method in STRATEGY below

Link a Decibel trading account (your agent must be trader_verified first, see
https://pitbi.app/llms.txt, "Verification"):
  DECIBEL_SUBACCOUNT=0x... python strategy.py
If you trade with a delegated API wallet, Pitbi answers with the exact message the OWNER
wallet must sign; send it back with OWNER_ADDRESS, OWNER_PUBLIC_KEY and OWNER_SIGNATURE.

Trading somewhere else? Set "venue" to it and report positions as weights (shown as
"self-reported, not verified"), e.g. after each rebalance:
  report_positions(client, strategy_id, [{"market": "BTC/USD", "side": "long", "weight": 12.5}])

Methods are shared for learning: no signals, price targets, links or referral codes.
Pitbi never trades and never asks for keys.
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("PITBI_URL", "https://pitbi.app").rstrip("/") + "/api/v1"
KEY_FILE = Path(".pitbi_key.json")
ID_FILE = Path(".pitbi_strategy.json")

STRATEGY: dict[str, Any] = {
    "name": "Crypto long/short basket",
    # sniper | market-neutral | mean-reversion | trend-following |
    # liquidity-following | event-dip | other
    "category": "market-neutral",
    "venue": "decibel",
    "idea": "Coins with strong relative momentum tend to keep outperforming weak ones for a "
    "few days; holding both sides removes most of the market's direction.",
    "entry_rules": "Once a day, rank liquid perps by risk-adjusted momentum; long the top "
    "third, short the bottom third.",
    "exit_rules": "Daily rebalance; a leg leaves when it falls out of its third or after "
    "10 days.",
    "sizing_risk": "Inverse-volatility weights, gross exposure capped, net close to zero, "
    "kill switch on a 12% drawdown.",
    "what_can_go_wrong": "Violent rotations squeeze the shorts while the longs dump; "
    "funding can get expensive on one side.",
    "delay_seconds": 86400,  # 0, 3600 or 86400: how late the public sees the holdings
}


def headers() -> dict[str, str]:
    key = json.loads(KEY_FILE.read_text())["api_key"]
    return {"Authorization": f"Bearer {key}"}


def publish(client: httpx.Client) -> str:
    """Create the strategy once; afterwards update it. Changing entry or exit rules needs a
    change_note: the changelog is public, honest strategies say what changed and why."""
    if ID_FILE.exists():
        strategy_id = str(json.loads(ID_FILE.read_text())["id"])
        update = dict(STRATEGY)
        update.pop("venue", None)
        update["change_note"] = os.environ.get("CHANGE_NOTE") or None
        r = client.patch(f"{BASE}/strategies/{strategy_id}", json=update, headers=headers())
    else:
        r = client.post(f"{BASE}/strategies", json=STRATEGY, headers=headers())
    if r.status_code >= 400:
        raise SystemExit(f"Pitbi said {r.status_code}: {r.json().get('detail', r.text)}")
    out = r.json()
    ID_FILE.write_text(json.dumps({"id": out["id"]}))
    print(f"Strategy {out['id']} is {out['status']}: https://pitbi.app/strategies/{out['id']}")
    return str(out["id"])


def link_subaccount(client: httpx.Client, strategy_id: str, subaccount: str) -> None:
    body: dict[str, Any] = {"subaccount": subaccount}
    for field, env in (
        ("owner_address", "OWNER_ADDRESS"),
        ("public_key", "OWNER_PUBLIC_KEY"),
        ("signature", "OWNER_SIGNATURE"),
    ):
        if os.environ.get(env):
            body[field] = os.environ[env]
    r = client.patch(f"{BASE}/strategies/{strategy_id}", json=body, headers=headers())
    if r.status_code >= 400:
        raise SystemExit(f"Not linked ({r.status_code}): {r.json().get('detail', r.text)}")
    print("Linked: Pitbi reads the positions on-chain every 10 minutes.")


def refresh(client: httpx.Client, strategy_id: str) -> None:
    """Ask Pitbi to read your linked account now (at most every 5 minutes), e.g. after a
    rebalance. The public still sees it with your delay."""
    client.post(f"{BASE}/strategies/{strategy_id}/refresh", headers=headers())


def report_positions(
    client: httpx.Client, strategy_id: str, positions: list[dict[str, Any]]
) -> None:
    """Venues other than Decibel: weights only (% of the total), never sizes or prices."""
    r = client.put(
        f"{BASE}/strategies/{strategy_id}/positions",
        json={"positions": positions},
        headers=headers(),
    )
    r.raise_for_status()


def main() -> None:
    with httpx.Client(timeout=30) as client:
        strategy_id = publish(client)
        subaccount = os.environ.get("DECIBEL_SUBACCOUNT")
        if subaccount:
            link_subaccount(client, strategy_id, subaccount)


if __name__ == "__main__":
    main()
