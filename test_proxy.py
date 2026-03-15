"""Test SOCKS5 proxy connectivity."""

from __future__ import annotations

import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

import config


def main() -> None:
    print("=== Proxy Connection Test ===\n")

    # 1. Check if PROXY_URL is set
    if not config.PROXY_URL:
        print("No PROXY_URL set in .env")
        sys.exit(1)

    print(f"PROXY_URL: {config.PROXY_URL}\n")

    # 2. Test proxy connection
    proxy_ip = None
    print("1. Testing proxy connection …")
    try:
        with config.get_sync_http_client(timeout=15.0) as client:
            resp = client.get("https://api64.ipify.org?format=json")
            proxy_ip = resp.json()["ip"]
        print(f"   Your IP through proxy: {proxy_ip}")
    except Exception as e:
        print(f"   FAILED: {e}")
        sys.exit(1)

    # 3. Test direct connection (no proxy)
    real_ip = None
    print("\n2. Testing direct connection …")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get("https://api64.ipify.org?format=json")
            real_ip = resp.json()["ip"]
        print(f"   Your real IP: {real_ip}")
    except Exception as e:
        print(f"   FAILED: {e}")

    # 4. Compare IPs
    print()
    if proxy_ip and real_ip:
        if proxy_ip != real_ip:
            print("3. Proxy is working correctly — IPs are different")
        else:
            print("3. WARNING: Proxy is NOT working — both IPs are the same")

    # 5. Test Polymarket reachability through proxy
    print("\n4. Testing Polymarket reachability through proxy …")
    try:
        with config.get_sync_http_client(timeout=15.0) as client:
            resp = client.get("https://clob.polymarket.com")
        if resp.status_code in (200, 401):
            print(f"   Polymarket reachable through proxy (HTTP {resp.status_code})")
        else:
            print(f"   Unexpected status: HTTP {resp.status_code}")
    except Exception as e:
        print(f"   Polymarket NOT reachable through proxy: {e}")

    print("\n" + "=" * 40)
    print("Done.")


if __name__ == "__main__":
    main()
