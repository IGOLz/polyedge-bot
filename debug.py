"""Standalone diagnostic script — run this before main.py to verify everything works."""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

import config

load_dotenv()


def main() -> None:
    print("=== Polymarket Bot Diagnostics ===\n")
    errors = 0

    # 1. Build ClobClient (signature_type=2 / proxy mode)
    print("1. Building ClobClient (signature_type=2, proxy mode) …")
    try:
        creds = ApiCreds(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            api_passphrase=config.API_PASSPHRASE,
        )
        clob = ClobClient(
            config.CLOB_BASE_URL,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            creds=creds,
            signature_type=2,
            funder=config.PROXY_WALLET,
        )
        print("   OK")
    except Exception as e:
        print(f"   FAIL: {e}")
        sys.exit(1)

    # 2. EOA address
    print("\n2. EOA address:")
    try:
        addr = clob.get_address()
        print(f"   {addr}")
    except Exception as e:
        print(f"   FAIL: {e}")
        errors += 1

    # 3. Proxy wallet
    print(f"\n3. Proxy wallet (from .env):")
    print(f"   {config.PROXY_WALLET}")

    # 4. Proxy wallet balance via ClobClient
    print("\n4. Proxy wallet balance (via get_balance_allowance):")
    balance = 0.0
    try:
        bal = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        balance = int(bal.get("balance", "0")) / 1_000_000
        allowance = int(bal.get("allowance", "0")) / 1_000_000
        print(f"   Proxy wallet balance: ${balance:.2f}")
        print(f"   Allowance:            ${allowance:.2f}")
    except Exception as e:
        print(f"   FAIL: {e}")
        errors += 1

    # 5. Recent trades from target wallet
    print(f"\n5. Last 5 trades from target ({config.TARGET_ADDRESS[:10]}…):")
    try:
        with config.get_sync_http_client(timeout=15.0) as client:
            resp = client.get(
                "https://data-api.polymarket.com/trades",
                params={"user": config.TARGET_ADDRESS, "limit": "5", "takerOnly": "true"},
            )
        resp.raise_for_status()
        trades = resp.json()
        if isinstance(trades, list) and trades:
            for i, t in enumerate(trades[:5], 1):
                side = t.get("side", "?")
                price = t.get("price", "?")
                title = (t.get("title") or t.get("market") or "unknown")[:50]
                print(f"   {i}. {side} @ {price} — {title}")
        else:
            print("   No trades found")
    except Exception as e:
        print(f"   FAIL: {e}")
        errors += 1

    # Summary
    print("\n" + "=" * 40)
    if errors > 0:
        print(f"RESULT: {errors} error(s) — review above before running main.py")
        sys.exit(1)
    elif balance == 0:
        print("RESULT: All checks passed but proxy wallet balance is $0")
        print("  Deposit USDC.e to your Polymarket account via polymarket.com")
        sys.exit(1)
    else:
        print(f"RESULT: All checks passed — proxy wallet balance ${balance:.2f} USDC")
        print("  Safe to run: python main.py")


if __name__ == "__main__":
    main()
