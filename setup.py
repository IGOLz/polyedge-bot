"""One-time setup: create or derive Polymarket API credentials from your private key."""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

CLOB_BASE_URL = "https://clob.polymarket.com"
CHAIN_ID = 137


def main() -> None:
    load_dotenv()
    pk = os.getenv("PRIVATE_KEY", "").strip()
    if not pk:
        print("ERROR: Set PRIVATE_KEY in your .env file first.")
        sys.exit(1)

    proxy = os.getenv("PROXY_WALLET", "").strip()
    if proxy:
        client = ClobClient(CLOB_BASE_URL, key=pk, chain_id=CHAIN_ID, signature_type=2, funder=proxy)
    else:
        client = ClobClient(CLOB_BASE_URL, key=pk, chain_id=CHAIN_ID)

    # Try creating new API key first (required for wallets that haven't registered yet)
    print("Creating API credentials …")
    try:
        creds = client.create_api_key()
        print("New API key created successfully.")
    except Exception as create_exc:
        print(f"create_api_key failed ({create_exc}), trying derive_api_key …")
        try:
            creds = client.derive_api_key()
            print("Existing API key derived successfully.")
        except Exception as derive_exc:
            print(f"\nBoth methods failed:")
            print(f"  create: {create_exc}")
            print(f"  derive: {derive_exc}")
            print("\nTroubleshooting:")
            print("  1. Make sure PRIVATE_KEY in .env is correct (starts with 0x)")
            print("  2. Your wallet may need to accept Polymarket ToS at https://polymarket.com first")
            print("  3. Try connecting your wallet on the Polymarket website before running this script")
            sys.exit(1)

    print("\n--- Add these to your .env file ---\n")
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    print()


if __name__ == "__main__":
    main()
