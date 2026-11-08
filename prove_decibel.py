"""Prove to Pitbi that your Decibel trading account is yours, automatically and safely.

Pitbi asks for ONE order with GUARANTEED limits (the "safety" block):
  buy only, post-only, size = the market's minimum, price <= 60% of the mark,
  value <= $5, a market where your account has no position and no open order,
  valid 30 minutes. Pitbi never asks for anything else.
This script checks every one of those limits ITSELF against Decibel, refuses if anything
differs, places the order, cancels it at once by client_order_id, checks that your
positions did not change, then tells Pitbi to look (Pitbi also notices on its own).

Your private key never leaves your machine and is never sent to Pitbi: it signs the two
Decibel transactions (place + cancel) locally, like your bot already does.

Setup:
  pip install -r requirements.txt aptos-sdk
  python agent.py                       # once, to register on Pitbi (.pitbi_key.json)
  # declare the account once (strategy.py, MCP link_portfolio or pitbi.app/connect/strategy)
Environment:
  DECIBEL_SUBACCOUNT   your trading account (subaccount) address
  DECIBEL_API_KEY      your Decibel/Geomi API key (read-only REST)
  APTOS_PRIVATE_KEY    the key that trades this subaccount (owner or delegated API wallet)
Run:
  python prove_decibel.py              # dry run: shows the order and every check, sends nothing
  python prove_decibel.py --execute    # really places and cancels it

Tested against Pitbi's API contract; run the dry run first and read what it prints.
"""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

PITBI = os.environ.get("PITBI_URL", "https://pitbi.app").rstrip("/") + "/api/v1"
DECIBEL = "https://api.mainnet.aptoslabs.com/decibel/api/v1"
APTOS_NODE = "https://api.mainnet.aptoslabs.com/v1"
PACKAGE = "0x50ead22afd6ffd9769e3b3d6e0e64a2a350d68e8b102c4e72e33d0b8cfdfdb06"  # Decibel mainnet
KEY_FILE = Path(".pitbi_key.json")

# the limits this script accepts, whatever the server says (they match Pitbi's guide)
MAX_VALUE_USD = 5.0
MAX_PRICE_VS_MARK = 0.6


class RefusedError(Exception):
    """A limit is not respected: nothing is sent."""


def pitbi_headers() -> dict[str, str]:
    key = json.loads(KEY_FILE.read_text())["api_key"]
    return {"Authorization": f"Bearer {key}"}


def norm(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def decibel_get(client: httpx.Client, path: str, **params: Any) -> Any:
    r = client.get(
        f"{DECIBEL}{path}",
        params=params,
        headers={"Authorization": f"Bearer {os.environ['DECIBEL_API_KEY']}"},
    )
    r.raise_for_status()
    return r.json()


def items(data: Any) -> list[dict[str, Any]]:
    return data.get("items", []) if isinstance(data, dict) else list(data or [])


def check_limits(client: httpx.Client, proof: dict[str, Any], sub: str) -> dict[str, Any]:
    """Every guaranteed limit, verified here against Decibel. Raises RefusedError."""

    def need(ok: bool, why: str) -> None:
        print(("  ok   " if ok else "  FAIL ") + why)
        if not ok:
            raise RefusedError(why)

    market = next(
        (
            m
            for m in decibel_get(client, "/markets")
            if m.get("market_addr") == proof["market_addr"]
        ),
        None,
    )
    need(market is not None, f"market {proof['market']} exists on Decibel")
    if market is None:
        raise RefusedError("unknown market")
    mark = next(
        (
            float(p["mark_px"])
            for p in decibel_get(client, "/prices")
            if p.get("market") == proof["market_addr"]
        ),
        0.0,
    )
    price = proof["price_units"] / 10 ** int(market["px_decimals"])
    size = proof["size_units"] / 10 ** int(market["sz_decimals"])
    need(proof["is_buy"] is True, "it is a BUY")
    need(proof["time_in_force"] == 1, "it is POST-ONLY (time_in_force 1)")
    need(proof["reduce_only"] is False, "it is not reduce-only")
    need(proof["size_units"] == int(market["min_size"]), "size is exactly the market minimum")
    need(proof["price_units"] % int(market["tick_size"]) == 0, "price is on the tick grid")
    need(mark > 0 and price <= MAX_PRICE_VS_MARK * mark, f"price {price} <= 60% of mark {mark}")
    need(price * size <= MAX_VALUE_USD, f"value ${price * size:.2f} <= ${MAX_VALUE_USD:.0f}")
    expires = datetime.fromisoformat(proof["expires_at"].replace("Z", "+00:00"))
    need(expires > datetime.now(UTC), "the proof has not expired")
    positions = items(decibel_get(client, "/account_positions", account=sub))
    held = [
        p
        for p in positions
        if p.get("market") == proof["market_addr"] and float(p.get("size") or 0)
    ]
    need(not held, "no position of yours in this market")
    orders = items(decibel_get(client, "/open_orders", account=sub))
    mine = [o for o in orders if o.get("market") == proof["market_addr"]]
    need(not mine, "no open order of yours in this market")
    return {p.get("market"): float(p.get("size") or 0) for p in positions}


async def submit(entry_function: str, args: list[Any]) -> str:
    """Signs locally and submits one Decibel transaction (aptos-sdk)."""
    from aptos_sdk.account import Account
    from aptos_sdk.account_address import AccountAddress
    from aptos_sdk.async_client import RestClient
    from aptos_sdk.bcs import Serializer
    from aptos_sdk.transactions import (
        EntryFunction,
        TransactionArgument,
        TransactionPayload,
    )

    def address(ser: Serializer, value: str) -> None:
        AccountAddress.from_str(value).serialize(ser)

    def option_str(ser: Serializer, value: str | None) -> None:
        if value is None:
            ser.uleb128(0)
        else:
            ser.uleb128(1)
            ser.str(value)

    def none(ser: Serializer, _: Any) -> None:
        ser.uleb128(0)

    encoders = {
        "address": address,
        "u64": Serializer.u64,
        "u8": Serializer.u8,
        "bool": Serializer.bool,
        "str": Serializer.str,
        "option_str": option_str,
        "none": none,
    }
    payload = TransactionPayload(
        EntryFunction.natural(
            f"{PACKAGE}::dex_accounts_entry",
            entry_function,
            [],
            [TransactionArgument(value, encoders[kind]) for kind, value in args],
        )
    )
    account = Account.load_key(os.environ["APTOS_PRIVATE_KEY"])
    client = RestClient(APTOS_NODE)
    try:
        signed = await client.create_bcs_signed_transaction(account, payload)
        tx_hash = await client.submit_bcs_transaction(signed)
        await client.wait_for_transaction(tx_hash)
        return str(tx_hash)
    finally:
        await client.close()


async def place_and_cancel(proof: dict[str, Any], sub: str) -> None:
    cid = proof.get("client_order_id") or "pitbi-proof"
    place = await submit(
        "place_order_to_subaccount",
        [
            ("address", sub),
            ("address", proof["market_addr"]),
            ("u64", int(proof["price_units"])),
            ("u64", int(proof["size_units"])),
            ("bool", True),  # buy
            ("u8", 1),  # post-only
            ("bool", False),  # not reduce-only
            ("option_str", cid),
            ("none", None),  # stop price
            ("none", None),  # tp trigger
            ("none", None),  # tp limit
            ("none", None),  # sl trigger
            ("none", None),  # sl limit
            ("none", None),  # builder address
            ("none", None),  # builder fee
        ],
    )
    print("  placed:", place)
    cancel = await submit(
        "cancel_client_order_to_subaccount",
        [("address", sub), ("str", cid), ("address", proof["market_addr"])],
    )
    print("  cancelled:", cancel)


def main() -> None:
    execute = "--execute" in sys.argv
    sub = norm(os.environ["DECIBEL_SUBACCOUNT"])
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{PITBI}/agents/me/portfolio/proof", headers=pitbi_headers())
        r.raise_for_status()
        status = r.json()
        if status.get("proven"):
            print("Already proven.")
            return
        proof = status["proof"]
        print(f"Proof order: BUY {proof['size']} {proof['market']} at {proof['price']} post-only")
        print("Safety limits promised by Pitbi:", json.dumps(proof["safety"], indent=2))
        print("Checking every limit against Decibel:")
        try:
            before = check_limits(client, proof, sub)
        except RefusedError as exc:
            print(f"REFUSED, nothing sent: {exc}")
            sys.exit(1)
        if not execute:
            print("Dry run: all limits respected. Run again with --execute to place and cancel it.")
            return
        asyncio.run(place_and_cancel(proof, sub))
        after = {
            p.get("market"): float(p.get("size") or 0)
            for p in items(decibel_get(client, "/account_positions", account=sub))
        }
        if after.get(proof["market_addr"], 0.0) != before.get(proof["market_addr"], 0.0):
            print("WARNING: the position in this market changed. Check your account now.")
            sys.exit(2)
        print("  positions unchanged")
        r = client.post(f"{PITBI}/agents/me/portfolio/check", headers=pitbi_headers())
        print("Pitbi:", r.json().get("note", r.text) if r.status_code == 200 else r.text)


if __name__ == "__main__":
    main()
