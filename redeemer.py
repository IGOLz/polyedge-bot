"""Auto-redeem resolved Polymarket positions directly on-chain via execTransaction."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from eth_abi import encode as abi_encode
from web3 import Web3

import config
import db
from balance import get_usdc_balance
from utils import log

# ── Constants (Polygon mainnet) ──────────────────────────────────────────
REDEEM_INTERVAL = 5 * 60  # 5 minutes

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
REDEEM_SELECTOR = bytes.fromhex("baa51c0f")  # keccak256("redeemPositions(address,bytes32,bytes32,uint256[])")
HASH_ZERO = b"\x00" * 32

# Minimal Gnosis Safe ABI — only the functions we use
SAFE_ABI = [
    {
        "name": "execTransaction",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable",
    },
    {
        "name": "nonce",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "getOwners",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
    },
]

# ── Lazy-initialised Web3 ────────────────────────────────────────────────
_w3: Web3 | None = None


def get_w3() -> Web3:
    global _w3
    if _w3 is None:
        rpc_url = os.environ["POLYGON_RPC_URL"]
        _w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if not _w3.is_connected():
            raise ConnectionError(f"Cannot connect to RPC: {rpc_url}")
        log.info("[REDEEM] Web3 connected: %s", rpc_url)
    return _w3


# ── Calldata encoding ────────────────────────────────────────────────────

def encode_redeem_calldata(condition_id: str, neg_risk: bool) -> tuple[str, bytes]:
    """Return (contract_address, calldata_bytes) for redeemPositions."""
    contract_address = NEG_RISK_ADDRESS if neg_risk else CTF_ADDRESS
    condition_id_bytes = bytes.fromhex(condition_id.removeprefix("0x"))
    encoded_args = abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_ADDRESS, HASH_ZERO, condition_id_bytes, [1, 2]],
    )
    return contract_address, REDEEM_SELECTOR + encoded_args


# ── Caller-approved signature ────────────────────────────────────────────

def build_caller_approved_signature(eoa_address: str) -> bytes:
    """
    Build a Safe 'caller-approved' signature (v=1).

    When the EOA calling execTransaction IS the Safe owner, the Safe contract
    allows a special signature format where:
      r = padded EOA address (32 bytes)
      s = 0x00...00 (32 bytes)
      v = 0x01 (1 byte)

    The Safe verifies: msg.sender == address(r), which passes since we ARE
    the caller. This completely bypasses EIP-712 hashing.

    Reference: GnosisSafe.sol checkNSignatures(), v==1 branch.
    """
    addr_bytes = bytes.fromhex(eoa_address.lower().removeprefix("0x"))
    r = b"\x00" * 12 + addr_bytes   # 32 bytes: 12 zero padding + 20 byte address
    s = b"\x00" * 32                 # 32 bytes of zeros
    v = b"\x01"                      # v = 1 = caller-approved
    return r + s + v                 # 65 bytes total


# ── Neg-risk detection via Gamma API ─────────────────────────────────────

async def is_neg_risk_market(condition_id: str) -> bool:
    """Query the Gamma API to check if a market uses the NegRisk adapter."""
    url = f"https://gamma-api.polymarket.com/markets?conditionId={condition_id}"
    async with config.get_http_client() as client:
        try:
            resp = await client.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data:
                return bool(data[0].get("negRisk", False))
        except Exception as exc:
            log.warning("[REDEEM] Gamma API lookup failed for %s: %s", condition_id[:10], exc)
    return False


# ── Core redemption via Safe execTransaction ─────────────────────────────

async def redeem_condition(condition_id: str, neg_risk: bool) -> str:
    """
    Execute redeemPositions through the Safe via execTransaction.
    The EOA calls execTransaction directly on-chain, paying gas in POL.
    Returns the transaction hash hex string.
    """
    private_key = config.PRIVATE_KEY
    proxy_wallet = config.PROXY_WALLET
    eoa_address = config.EOA_ADDRESS

    contract_address, calldata = encode_redeem_calldata(condition_id, neg_risk)

    loop = asyncio.get_event_loop()

    def _send() -> str:
        w3 = get_w3()
        safe = w3.eth.contract(
            address=Web3.to_checksum_address(proxy_wallet),
            abi=SAFE_ABI,
        )

        safe_nonce = safe.functions.nonce().call()
        log.info("[REDEEM] condition=%s safe_nonce=%d", condition_id[:10], safe_nonce)

        signature = build_caller_approved_signature(eoa_address)

        tx = safe.functions.execTransaction(
            Web3.to_checksum_address(contract_address),  # to
            0,                                           # value
            calldata,                                    # data
            0,                                           # operation: CALL
            0,                                           # safeTxGas
            0,                                           # baseGas
            0,                                           # gasPrice
            Web3.to_checksum_address(ZERO_ADDRESS),      # gasToken
            Web3.to_checksum_address(ZERO_ADDRESS),      # refundReceiver
            signature,                                   # signatures
        ).build_transaction({
            "from": Web3.to_checksum_address(eoa_address),
            "nonce": w3.eth.get_transaction_count(
                Web3.to_checksum_address(eoa_address)
            ),
            "gas": 300_000,
            "maxFeePerGas": w3.to_wei("100", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("30", "gwei"),
        })

        signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()
        log.info("[REDEEM] TX submitted: %s", tx_hash_hex)

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] == 0:
            raise RuntimeError(f"TX reverted on-chain: {tx_hash_hex}")

        log.info("[REDEEM] Confirmed block=%d tx=%s", receipt["blockNumber"], tx_hash_hex)
        return tx_hash_hex

    return await loop.run_in_executor(None, _send)


# ── Main redemption cycle ────────────────────────────────────────────────

async def _redeem_cycle() -> None:
    """One pass: fetch unredeemed winning trades, redeem via Safe, mark in DB."""
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

    log.info("[REDEEM] %d condition(s) to redeem", len(seen))

    for condition_id, fill in seen.items():
        short = condition_id[:10]
        try:
            neg_risk = await is_neg_risk_market(condition_id)
            log.info("[REDEEM] Redeeming %s... (neg_risk=%s)", short, neg_risk)

            balance_before = await get_usdc_balance()

            tx_hash = await redeem_condition(condition_id, neg_risk)
            log.info("[REDEEM] Confirmed: %s tx=%s", short, tx_hash)

            balance_after = await get_usdc_balance()
            amount = max(0.0, balance_after - balance_before) if balance_before >= 0 and balance_after >= 0 else 0.0

            await db.mark_redeemed(condition_id)
            log.info("[REDEEM] Marked redeemed: %s ($%.2f returned)", short, amount)

            if amount > 0:
                await db.log_event(
                    "trade_win",
                    f"Redeemed winning position — ${amount:.2f} returned",
                    {
                        "market_id": fill.get("market_id"),
                        "amount_redeemed": round(amount, 2),
                        "condition_id": condition_id,
                        "tx_hash": tx_hash,
                    },
                )
        except Exception as exc:
            log.error("[REDEEM] Failed %s: %s", short, exc, exc_info=True)
            continue

        await asyncio.sleep(10)


# ── Redemption loop (imported by main.py) ────────────────────────────────

async def redemption_loop() -> None:
    """Run redemption checks every 5 minutes, starting immediately."""
    log.info("Redemption loop started (every %d min)", REDEEM_INTERVAL // 60)
    while True:
        try:
            await _redeem_cycle()
        except Exception:
            log.exception("Unexpected error in redemption loop")
        await asyncio.sleep(REDEEM_INTERVAL)
