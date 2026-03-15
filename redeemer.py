"""Auto-redeem resolved Polymarket positions via the gasless Relayer API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

import config
import db
from balance import get_usdc_balance
from utils import log

# ── Constants ────────────────────────────────────────────────────────────
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
RELAYER_URL = os.getenv("POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com")
CHAIN_ID = 137

REDEEM_INTERVAL = 5 * 60  # 5 minutes

# redeemPositions(address,bytes32,bytes32,uint256[]) selector
_REDEEM_SELECTOR = Web3.keccak(
    text="redeemPositions(address,bytes32,bytes32,uint256[])"
)[:4]

# EIP-712 type hashes
_DOMAIN_TYPEHASH = Web3.keccak(
    text="EIP712Domain(uint256 chainId,address verifyingContract)"
)
_SAFE_TX_TYPEHASH = Web3.keccak(
    text="SafeTx(address to,uint256 value,bytes data,uint8 operation,"
    "uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,"
    "address gasToken,address refundReceiver,uint256 nonce)"
)

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# ── Step 1: Build redeemPositions calldata ───────────────────────────────

def build_redeem_calldata(condition_id: str) -> str:
    """Encode redeemPositions(collateral, parentCollection, conditionId, indexSets)."""
    condition_bytes = bytes.fromhex(condition_id.replace("0x", "").zfill(64))
    parent_collection = bytes(32)
    usdc = Web3.to_checksum_address(USDC_ADDRESS)
    index_sets = [1, 2]

    encoded = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [usdc, parent_collection, condition_bytes, index_sets],
    )
    return "0x" + (_REDEEM_SELECTOR + encoded).hex()


# ── Step 2: Fetch nonce from relayer ─────────────────────────────────────

async def get_relayer_nonce(client, eoa_address: str) -> str:
    """GET /nonce?address=...&type=SAFE"""
    resp = await client.get(
        f"{RELAYER_URL}/nonce",
        params={"address": eoa_address, "type": "SAFE"},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["nonce"]


# ── Step 3: Build EIP-712 Safe transaction hash ─────────────────────────

def build_safe_tx_hash(
    safe_address: str,
    to: str,
    data: str,
    nonce: str,
    chain_id: int = CHAIN_ID,
) -> bytes:
    """Compute the EIP-712 hash that the Safe owner must sign."""
    # Domain separator
    domain_separator = Web3.keccak(
        encode(
            ["bytes32", "uint256", "address"],
            [_DOMAIN_TYPEHASH, chain_id, Web3.to_checksum_address(safe_address)],
        )
    )

    data_bytes = bytes.fromhex(data.replace("0x", ""))

    # Struct hash
    safe_tx_hash = Web3.keccak(
        encode(
            [
                "bytes32", "address", "uint256", "bytes32", "uint8",
                "uint256", "uint256", "uint256", "address", "address", "uint256",
            ],
            [
                _SAFE_TX_TYPEHASH,
                Web3.to_checksum_address(to),
                0,                              # value
                Web3.keccak(data_bytes),         # keccak256 of data
                0,                              # operation: CALL
                0,                              # safeTxGas
                0,                              # baseGas
                0,                              # gasPrice
                _ZERO_ADDRESS,                  # gasToken
                _ZERO_ADDRESS,                  # refundReceiver
                int(nonce),                     # nonce
            ],
        )
    )

    # Final EIP-712 hash: "\x19\x01" ‖ domainSeparator ‖ safeTxHash
    return Web3.keccak(b"\x19\x01" + domain_separator + safe_tx_hash)


# ── Step 4: Sign with EIP-191 prefix, adjust v for Safe ─────────────────

def sign_safe_tx(tx_hash: bytes, private_key: str) -> str:
    """eth_sign style: sign tx_hash with EIP-191 prefix, adjust v for Safe."""
    account = Account.from_key(private_key)
    # encode_defunct handles the "\x19Ethereum Signed Message:\n32" prefix internally
    msg = encode_defunct(primitive=tx_hash)
    signed = account.sign_message(msg)

    r = signed.r.to_bytes(32, "big")
    s = signed.s.to_bytes(32, "big")
    v = signed.v

    # Adjust v for Safe format: 27→31, 28→32
    if v in (27, 28):
        v = v + 4
    elif v in (0, 1):
        v = v + 31

    return "0x" + r.hex() + s.hex() + bytes([v]).hex()


# ── Builder API credentials (separate from CLOB API) ─────────────────────
BUILDER_API_KEY = os.getenv("POLYMARKET_BUILDER_API_KEY", "")
BUILDER_SECRET = os.getenv("POLYMARKET_BUILDER_SECRET", "")
BUILDER_PASSPHRASE = os.getenv("POLYMARKET_BUILDER_PASSPHRASE", "")


# ── Step 5: Build HMAC-SHA256 builder headers ────────────────────────────

def build_builder_headers(method: str, path: str, body: dict | None) -> dict[str, str]:
    """Polymarket Builder API authentication headers."""
    timestamp = str(int(time.time()))
    body_string = json.dumps(body, separators=(",", ":")) if body else ""
    message = f"{timestamp}{method}{path}{body_string}"

    # Decode secret with padding fix
    try:
        padded = BUILDER_SECRET + "=" * (4 - len(BUILDER_SECRET) % 4)
        secret_bytes = base64.urlsafe_b64decode(padded)
    except Exception:
        secret_bytes = BUILDER_SECRET.encode()

    sig = base64.urlsafe_b64encode(
        hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    ).decode()

    return {
        "POLY_BUILDER_API_KEY": BUILDER_API_KEY,
        "POLY_BUILDER_SIGNATURE": sig,
        "POLY_BUILDER_TIMESTAMP": timestamp,
        "POLY_BUILDER_PASSPHRASE": BUILDER_PASSPHRASE,
        "Content-Type": "application/json",
    }


# ── Step 6: Submit to relayer and poll for confirmation ──────────────────

async def submit_to_relayer(
    client,
    eoa_address: str,
    safe_address: str,
    calldata: str,
    signature: str,
    nonce: str,
) -> str:
    """POST /submit — returns the relayer transaction ID."""
    body = {
        "type": "SAFE",
        "from": Web3.to_checksum_address(eoa_address),
        "to": Web3.to_checksum_address(CTF_ADDRESS),
        "proxyWallet": Web3.to_checksum_address(safe_address),
        "data": calldata,
        "signature": signature,
        "value": "0",
        "nonce": str(nonce),
        "signatureParams": {
            "gasPrice": "0",
            "operation": "0",
            "safeTxnGas": "0",
            "baseGas": "0",
            "gasToken": _ZERO_ADDRESS,
            "refundReceiver": _ZERO_ADDRESS,
        },
        "metadata": "",
    }

    headers = build_builder_headers("POST", "/submit", body)

    resp = await client.post(
        f"{RELAYER_URL}/submit",
        json=body,
        headers=headers,
    )
    if resp.status_code != 200:
        raise Exception(f"Relayer submit failed: {resp.status_code} — {resp.text}")
    data = resp.json()
    return data["transactionID"]


async def poll_relayer(client, transaction_id: str, max_polls: int = 30) -> bool:
    """Poll GET /transaction until mined/confirmed or failed."""
    for i in range(max_polls):
        resp = await client.get(
            f"{RELAYER_URL}/transaction",
            params={"id": transaction_id},
        )
        data = resp.json()

        # Handle both list and dict responses
        if isinstance(data, list):
            if len(data) == 0:
                await asyncio.sleep(2)
                continue
            tx = data[0]
        else:
            tx = data

        state = tx.get("state", tx.get("status", ""))
        log.info("[REDEEM] Transaction state: %s", state)

        if state in ("STATE_MINED", "STATE_CONFIRMED", "CONFIRMED", "MINED", "SUCCESS"):
            return True
        if state in ("STATE_FAILED", "FAILED", "ERROR"):
            raise Exception(f"Relayer transaction failed: {tx}")

        await asyncio.sleep(2)
    raise Exception("Relayer polling timed out")


# ── Step 7: Main redemption function ────────────────────────────────────

async def redeem_condition(
    client,
    condition_id: str,
    eoa_address: str,
    eoa_private_key: str,
    safe_address: str,
) -> float:
    """Redeem a single resolved condition via the relayer. Returns amount redeemed."""
    short = condition_id[:10]
    log.info("[REDEEM] Starting redemption for %s...", short)

    # Build calldata
    calldata = build_redeem_calldata(condition_id)

    # Get nonce
    nonce = await get_relayer_nonce(client, eoa_address)
    log.info("[REDEEM] Got nonce: %s", nonce)

    # Build and sign Safe tx hash
    tx_hash = build_safe_tx_hash(safe_address, CTF_ADDRESS, calldata, nonce)
    signature = sign_safe_tx(tx_hash, eoa_private_key)

    # Balance before
    balance_before = await get_usdc_balance()

    # Submit to relayer
    txn_id = await submit_to_relayer(
        client, eoa_address, safe_address, calldata, signature, nonce,
    )
    log.info("[REDEEM] Submitted to relayer — txn_id: %s", txn_id)

    # Poll for confirmation
    await poll_relayer(client, txn_id)

    # Balance after
    balance_after = await get_usdc_balance()
    amount = max(0.0, balance_after - balance_before) if balance_before >= 0 and balance_after >= 0 else 0.0

    log.info("[REDEEM] Success — redeemed $%.2f for %s", amount, short)
    return amount


# ── Step 8: Redemption loop ─────────────────────────────────────────────

async def _redeem_cycle() -> None:
    """One pass: check all unredeemed winning trades, redeem via relayer."""
    fills = await db.get_unredeemed_fills()
    if not fills:
        return

    # De-duplicate by condition_id
    seen: dict[str, dict[str, Any]] = {}
    for f in fills:
        cid = f["condition_id"]
        if cid and cid not in seen:
            seen[cid] = f

    if not seen:
        return

    log.info("[REDEEM] %d conditions to check", len(seen))

    async with config.get_http_client() as client:
        for condition_id, fill in seen.items():
            try:
                amount = await redeem_condition(
                    client,
                    condition_id,
                    config.EOA_ADDRESS,
                    config.PRIVATE_KEY,
                    config.PROXY_WALLET,
                )
                await db.mark_redeemed(condition_id)
                if amount > 0:
                    await db.log_event(
                        "trade_win",
                        f"Redeemed winning position — ${amount:.2f} returned",
                        {
                            "market_id": fill.get("market_id"),
                            "amount_redeemed": round(amount, 2),
                            "condition_id": condition_id,
                        },
                    )
            except Exception as exc:
                log.error("[REDEEM] Failed %s: %s", condition_id[:10], exc)

            await asyncio.sleep(10)


async def redemption_loop() -> None:
    """Run redemption checks every 5 minutes, starting immediately."""
    log.info("Redemption loop started (every %d min)", REDEEM_INTERVAL // 60)
    while True:

        try:
            await _redeem_cycle()
        except Exception:
            log.exception("Unexpected error in redemption loop")
        await asyncio.sleep(REDEEM_INTERVAL)
