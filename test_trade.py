"""Test script: place a tiny order to verify the signing + order flow works."""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

import config

load_dotenv()


def build_clob_client() -> ClobClient:
    creds = ApiCreds(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        api_passphrase=config.API_PASSPHRASE,
    )
    return ClobClient(
        config.CLOB_BASE_URL,
        key=config.PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        creds=creds,
        signature_type=2,
        funder=config.PROXY_WALLET,
    )


def find_active_market(clob: ClobClient) -> dict | None:
    """Find a currently active market with a live orderbook."""
    # Use Gamma API — it returns events with active/open markets
    print("Fetching active markets from Gamma API …")
    try:
        with config.get_sync_http_client(timeout=15.0) as http:
            resp = http.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": "20", "active": "true", "closed": "false"},
            )
        resp.raise_for_status()
        markets = resp.json()

        for m in markets:
            token_id = m.get("clobTokenIds")
            question = m.get("question", "unknown")
            outcome = m.get("groupItemTitle") or m.get("outcomes", "Yes")

            if not token_id:
                continue

            # clobTokenIds can be a JSON string like '["abc","def"]' or a plain string
            if isinstance(token_id, str) and token_id.startswith("["):
                import json
                token_ids = json.loads(token_id)
                token_id = token_ids[0] if token_ids else None

            if not token_id:
                continue

            # Verify the orderbook actually exists on the CLOB
            print(f"  Checking orderbook for: {question[:60]}…")
            try:
                book = clob.get_order_book(token_id)
                # If we get here without error, the orderbook exists
                if book:
                    return {
                        "question": question,
                        "token_id": token_id,
                        "outcome": outcome,
                    }
            except Exception:
                continue  # orderbook doesn't exist, try next

    except Exception as e:
        print(f"  Gamma API failed: {e}")

    # Fallback: use the target wallet's recent trades to find a live market
    print("Falling back to target wallet's recent markets …")
    try:
        with config.get_sync_http_client(timeout=15.0) as http:
            resp = http.get(
                "https://data-api.polymarket.com/trades",
                params={"user": config.TARGET_ADDRESS, "limit": "10", "takerOnly": "true"},
            )
        resp.raise_for_status()
        trades = resp.json()
        if isinstance(trades, list) and trades:
            t = trades[0]
            token_id = str(t.get("asset") or t.get("asset_id") or t.get("token_id", ""))
            if token_id:
                return {
                    "question": t.get("title") or t.get("market") or "recent trade market",
                    "token_id": token_id,
                    "outcome": t.get("outcome", "Yes"),
                }
    except Exception as e:
        print(f"  Fallback also failed: {e}")

    return None


def main() -> None:
    from eth_account import Account
    pk = config.PRIVATE_KEY
    if not pk.startswith("0x"):
        pk = "0x" + pk
    acct = Account.from_key(pk)
    print(f"Your EOA address: {acct.address}")
    print()

    # Build authenticated client
    clob = build_clob_client()

    # Step 1: Call get_address() — find out what address CLOB thinks we are
    print("=== Step 1: get_address() ===")
    try:
        addr = clob.get_address()
        print(f"  CLOB address: {addr}")
    except Exception as e:
        print(f"  get_address() failed: {e}")

    # Step 2: Check collateral/exchange/conditional addresses
    print("\n=== Step 2: Contract addresses ===")
    for method in ["get_collateral_address", "get_exchange_address", "get_conditional_address"]:
        try:
            result = getattr(clob, method)()
            print(f"  {method}(): {result}")
        except Exception as e:
            print(f"  {method}(): {e}")

    # Step 3: Current balance
    print("\n=== Step 3: Current balance ===")
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    bal = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  Raw: {bal}")

    # Step 4: Try update_balance_allowance — this approves USDC for the exchange
    print("\n=== Step 4: Trying update_balance_allowance() ===")
    print("  (This sets USDC approval for Polymarket exchange contracts)")
    try:
        result = clob.update_balance_allowance()
        print(f"  Result: {result}")
    except Exception as e:
        print(f"  Failed: {e}")

    # Step 5: Re-check balance after approval
    print("\n=== Step 5: Balance after approval ===")
    bal2 = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    balance = float(bal2.get("balance", 0)) if isinstance(bal2, dict) else 0
    print(f"  Raw: {bal2}")
    print(f"  Parsed: ${balance:.2f}")

    if balance > 0:
        print(f"\n  *** Balance found: ${balance:.2f}! Attempting test order… ***")
        # Find market and try order
        market = find_active_market(clob)
        if market:
            try:
                order_args = OrderArgs(
                    token_id=market["token_id"],
                    price=0.01,
                    size=1.0,
                    side="BUY",
                )
                signed = clob.create_order(order_args)
                resp = clob.post_order(signed, OrderType.GTC)
                print(f"  ORDER PLACED! {resp}")
                order_id = resp.get("orderID") or resp.get("order_id") if isinstance(resp, dict) else None
                if order_id:
                    clob.cancel(order_id)
                    print(f"  Cancelled. Everything works!")
            except Exception as e:
                print(f"  Order failed: {e}")
    else:
        print("\n  Balance still $0. Your funds may be in a custodial wallet")
        print("  that the CLOB API can't access. You may need to deposit")
        print("  fresh USDC to use the bot.")


if __name__ == "__main__":
    main()
