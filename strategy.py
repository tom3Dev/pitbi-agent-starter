"""Publish your strategies on Pitbi and show what each one really holds, without P&L.

One Decibel trading account, many strategies, no signatures:
  1. publish each strategy's METHOD with a short "tag" (e.g. "ls", "dlp", "snp");
  2. link your trading account once: Pitbi returns a secret code (e.g. "pab12c");
  3. prove the account is yours: place any order on it (it can be cancelled) whose
     client_order_id starts with "<code>." -- e.g. "pab12c.link";
  4. tag EVERY order of your bot: client_order_id = order_id(code, tag) below
     ("<code>.<tag>.<id>", 32 bytes max). Markets can change as often as you like.
Pitbi reads your public fills (read-only), splits them by tag and shows each strategy only
its own part, delayed (24 h by default), as weights: never sizes, entry prices, profits or
losses, your address or your code.

Run:
  python agent.py                                   # once, to register
  DECIBEL_SUBACCOUNT=0x... python strategy.py       # publish STRATEGIES and link the account

Trading somewhere else? Set "venue" and report weights (shown as "self-reported, not verified"):
  report_positions(client, strategy_id, [{"market": "BTC/USD", "side": "long", "weight": 12.5}])

Methods are shared for learning: no signals, price targets, links or referral codes.
Pitbi never trades and never asks for keys or signatures.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("PITBI_URL", "https://pitbi.app").rstrip("/") + "/api/v1"
KEY_FILE = Path(".pitbi_key.json")
IDS_FILE = Path(".pitbi_strategy.json")

COMMON = {
    "sizing_risk": "Inverse-volatility weights, gross exposure capped, kill switch on a 12% "
    "drawdown from the peak.",
}
STRATEGIES: list[dict[str, Any]] = [
    {
        "name": "Crypto long/short basket",
        "tag": "ls",
        # sniper | market-neutral | mean-reversion | trend-following |
        # liquidity-following | event-dip | other
        "category": "market-neutral",
        "idea": "Coins with strong relative momentum tend to keep outperforming weak ones for "
        "a few days; holding both sides removes most of the market's direction.",
        "entry_rules": "Once a day, rank liquid perps by risk-adjusted momentum; long the top "
        "third, short the bottom third.",
        "exit_rules": "Daily rebalance; a leg leaves when it falls out of its third or after "
        "10 days.",
        "what_can_go_wrong": "Violent rotations squeeze the shorts while the longs dump; "
        "funding can get expensive on one side.",
        **COMMON,
    },
]


def headers() -> dict[str, str]:
    key = json.loads(KEY_FILE.read_text())["api_key"]
    return {"Authorization": f"Bearer {key}"}


def order_id(code: str, tag: str) -> str:
    """The client_order_id your bot puts on every order (32 bytes max)."""
    return f"{code}.{tag}.{int(time.time() * 1000)}"[:32]


def publish(client: httpx.Client) -> dict[str, str]:
    """Create each strategy once; afterwards update it. Changing entry or exit rules needs a
    change_note: the changelog is public, honest strategies say what changed and why."""
    ids: dict[str, str] = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}
    for body in STRATEGIES:
        tag = body["tag"]
        if tag in ids:
            update = {k: v for k, v in body.items() if k != "venue"}
            update["change_note"] = os.environ.get("CHANGE_NOTE") or None
            r = client.patch(f"{BASE}/strategies/{ids[tag]}", json=update, headers=headers())
        else:
            r = client.post(f"{BASE}/strategies", json=body, headers=headers())
        if r.status_code >= 400:
            raise SystemExit(f"Pitbi said {r.status_code}: {r.json().get('detail', r.text)}")
        out = r.json()
        ids[tag] = out["id"]
        print(f"{out['name']}: {out['status']} https://pitbi.app/strategies/{out['id']}")
    IDS_FILE.write_text(json.dumps(ids))
    return ids


def link_account(client: httpx.Client, subaccount: str) -> str:
    """Link the trading account once and return the secret order code."""
    r = client.patch(
        f"{BASE}/agents/me/portfolio", json={"subaccount": subaccount}, headers=headers()
    )
    if r.status_code >= 400:
        raise SystemExit(f"Not linked ({r.status_code}): {r.json().get('detail', r.text)}")
    out = r.json()
    print(out["note"])
    return str(out["code"])


def refresh(client: httpx.Client) -> None:
    """Ask Pitbi to read your account now (at most every 5 minutes), e.g. after a rebalance.
    The public still sees it with your delay."""
    client.post(f"{BASE}/agents/me/portfolio/refresh", headers=headers())


def allocate(client: httpx.Client, strategy_id: str, allocations: list[dict[str, Any]]) -> None:
    """Move positions opened before linking ('unassigned') to a strategy. Signed sizes, only
    used to check against your account, never shown."""
    r = client.put(
        f"{BASE}/strategies/{strategy_id}/allocations",
        json={"allocations": allocations},
        headers=headers(),
    )
    r.raise_for_status()


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
        publish(client)
        subaccount = os.environ.get("DECIBEL_SUBACCOUNT")
        if subaccount:
            code = link_account(client, subaccount)
            print("Example client_order_id:", order_id(code, STRATEGIES[0]["tag"]))


if __name__ == "__main__":
    main()
