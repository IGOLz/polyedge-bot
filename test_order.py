"""Place a real FOK test order on an active Polymarket market."""

from __future__ import annotations

import json
import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

import config

load_dotenv()

MIN_DOLLAR_SIZE = 1.0  # Polymarket minimum order value in dollars


def build_client() -> ClobClient:
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


def find_market(client: ClobClient) -> tuple[str, str, float] | None:
    """Find an active market via Gamma API, verify orderbook has asks."""
    print("Fetching active markets from Gamma API …")
    try:
        with config.get_sync_http_client(timeout=15.0) as http:
            resp = http.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "accepting_orders": "true",
                    "limit": "100",
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        print(f"  Gamma API failed: {e}")
        return None

    print(f"  Found {len(markets)} active markets")

    for market in markets:
        question = market.get("question", "unknown")

        raw_ids = market.get("clobTokenIds", "[]")
        raw_prices = market.get("outcomePrices", "[]")
        try:
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        except (json.JSONDecodeError, TypeError):
            continue

        if not token_ids or not prices:
            continue

        for i, token_id in enumerate(token_ids):
            if not token_id:
                continue
            gamma_price = float(prices[i]) if i < len(prices) else 0
            if not (0.05 < gamma_price < 0.95):
                continue

            # Verify orderbook has live asks
            try:
                book = client.get_order_book(token_id)
                asks = book.asks if hasattr(book, "asks") else []
                if not asks:
                    continue
                best_ask = float(min(asks, key=lambda x: float(x.price)).price)
                print(f"\n  Selected: {question}")
                print(f"  Token ID: {token_id}")
                print(f"  Best ask: ${best_ask:.4f}")
                print(f"  Top 3 asks: {[(float(a.price), float(a.size)) for a in asks[:3]]}")
                return question, token_id, best_ask
            except Exception:
                continue

    print("\nNo suitable market found.")
    return None


def main() -> None:
    print("=== Polymarket FOK Test Order ===\n")

    try:
        client = build_client()
    except Exception as e:
        print(f"Failed to build client: {e}")
        sys.exit(1)

    result = find_market(client)
    if result is None:
        print("\nNo suitable market found")
        sys.exit(1)

    question, token_id, best_ask = result
    dollars = max(config.FIXED_SIZE, MIN_DOLLAR_SIZE)
    # Floor to whole shares to avoid decimal precision errors
    import math
    shares = math.floor(dollars / best_ask)

    print(f"\nPlacing FOK BUY: ${dollars:.2f} ({shares} shares) @ ${best_ask:.4f} …")

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=round(best_ask, 3),
            size=shares,
            side="BUY",
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        print(f"\nOrder result: {resp}")

        order_status = (resp.get("status") or "").upper() if isinstance(resp, dict) else ""
        if order_status in ("CANCELLED", "EXPIRED"):
            print("\nFOK order found no match — cancelled (no money spent)")
        else:
            print("\nOrder filled!")
    except Exception as e:
        print(f"\nOrder FAILED: {e}")
        sys.exit(1)

    print("\nView your orders at:")
    print(f"https://polymarket.com/profile/{client.get_address()}")


if __name__ == "__main__":
    main()
