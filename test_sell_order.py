"""Test script for stop-loss GTC sell order placement.

Every run: BUY on active BTC 5m market, wait 5s, try stop-loss sell.
Standalone — does not import from bot modules.
"""

import asyncio
import os
import time

import asyncpg
import httpx
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    AssetType,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

# Initialize CLOB client — same as main bot
config_patch = os.getenv('PROXY_URL', '')
if config_patch:
    import py_clob_client.http_helpers.helpers as _helpers
    _helpers._http_client = httpx.Client(
        http2=True,
        transport=httpx.HTTPTransport(proxy=config_patch),
    )

clob = ClobClient(
    "https://clob.polymarket.com",
    key=os.getenv("PRIVATE_KEY"),
    chain_id=137,
    creds=ApiCreds(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
    ),
    signature_type=2,
    funder=os.getenv("PROXY_WALLET"),
)

print("=" * 60)
print("STOP-LOSS TEST — BTC 5m market")
print("=" * 60)

# Step 1 — Find current active BTC 5m market from database
print("\n[1] Finding active BTC 5m market from database...")

async def get_btc_5m_market():
    pool = await asyncpg.create_pool(
        host=os.getenv('POSTGRES_HOST', 'localhost'),
        port=int(os.getenv('POSTGRES_PORT', '5432')),
        user=os.getenv('POSTGRES_USER', 'polymarket'),
        password=os.getenv('POSTGRES_PASSWORD', ''),
        database=os.getenv('POSTGRES_DB', 'polymarket_tracker'),
        min_size=1,
        max_size=3,
    )
    row = await pool.fetchrow("""
        SELECT market_id, started_at, ended_at
        FROM market_outcomes
        WHERE market_type = 'btc_5m'
        AND resolved = FALSE
        AND ended_at > NOW()
        ORDER BY ended_at ASC
        LIMIT 1
    """)
    await pool.close()
    return row

market = asyncio.run(get_btc_5m_market())
if not market:
    print("No active BTC 5m market in database")
    exit()

condition_id = market['market_id']
print(f"Market from DB: {condition_id[:16]}... | ends: {market['ended_at']}")

# Step 2 — Get token IDs and prices
print("\n[2] Getting token IDs...")
market_detail = clob.get_market(condition_id)
tokens = market_detail.get('tokens', []) if isinstance(market_detail, dict) else []

token_info = []
for t in tokens:
    try:
        price_resp = clob.get_price(t['token_id'], 'BUY')
        price = float(price_resp.get('price', 0)) if isinstance(price_resp, dict) else float(price_resp)
        token_info.append({'token_id': t['token_id'], 'outcome': t.get('outcome', ''), 'price': price})
        print(f"  {t.get('outcome')}: {price:.2f} | token: {t['token_id'][:16]}")
    except Exception as e:
        print(f"  Error: {e}")

if not token_info:
    print("No tokens found")
    exit()

# Step 3 — Buy the winning side (highest price)
best = max(token_info, key=lambda x: x['price'])
token_id = best['token_id']
buy_price = best['price']

# Get best ask from orderbook
try:
    book = clob.get_order_book(token_id)
    asks = book.get('asks', []) if isinstance(book, dict) else (book.asks if hasattr(book, 'asks') else [])
    if asks:
        buy_price = float(asks[0]['price'] if isinstance(asks[0], dict) else asks[0].price)
except Exception:
    buy_price = round(buy_price + 0.01, 2)

shares = max(2, int(1.50 / buy_price))
while shares * buy_price < 1.0:
    shares += 1

print(f"\n[3] Buying '{best['outcome']}' @ {buy_price} | {shares} shares (${shares * buy_price:.2f})")

buy_args = OrderArgs(token_id=token_id, price=buy_price, size=float(shares), side=BUY)
signed = clob.create_order(buy_args)
try:
    resp = clob.post_order(signed, OrderType.FOK)
    print(f"BUY result: status={resp.get('status')} success={resp.get('success')}")
    if not resp.get('success') and resp.get('status') != 'matched':
        print("BUY did not fill — cannot test stop-loss")
        exit()
except Exception as e:
    print(f"BUY failed: {e}")
    exit()

# Step 4 — Wait for token settlement
print("\n[4] Waiting 5 seconds for token settlement...")
time.sleep(5)

# Step 5 — Check balance
balance_resp = clob.get_balance_allowance(BalanceAllowanceParams(
    asset_type=AssetType.CONDITIONAL,
    token_id=token_id,
))
balance = int(balance_resp.get('balance', '0') if isinstance(balance_resp, dict) else '0')
print(f"Token balance: {balance} ({'OK' if balance > 0 else 'ZERO — tokens not settled yet'})")

if balance == 0:
    print("Balance is 0 — cannot test stop-loss. Try again in a few seconds.")
    exit()

# Step 6 — Place stop-loss GTC sell @ 0.10
stop_price = 0.10
print(f"\n[5] Placing GTC SELL (stop-loss) @ {stop_price} | {shares} shares...")

sell_args = OrderArgs(token_id=token_id, price=stop_price, size=float(shares), side=SELL)
signed_sell = clob.create_order(sell_args)
try:
    sell_resp = clob.post_order(signed_sell, OrderType.GTC)
    print(f"STOP-LOSS result: {sell_resp}")
    order_id = sell_resp.get('orderID') or sell_resp.get('id') if isinstance(sell_resp, dict) else None
    if order_id:
        print(f"\nSUCCESS! Stop-loss placed: {order_id[:16]}")
        print("Cancelling test order...")
        time.sleep(2)
        clob.cancel(order_id)
        print("Cancelled.")
    else:
        print("No order ID returned")
except Exception as e:
    print(f"STOP-LOSS FAILED: {e}")

print("\nDone.")
