"""
Standalone test script for the redemption pipeline.
Hits live Polygon mainnet RPC and executes directly on-chain via Safe execTransaction.
Safe to run once per condition — second run will show 0 USDC delta.

Usage:
    python test_redemption.py
"""

import os
import sys
import traceback

# ── Ensure project root is on sys.path so we can import redeemer ─────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from web3 import Web3

# ── Hardcoded test values (real resolved winning bet) ────────────────────
CONDITION_ID = "0xd44257576c81086bd9081122a34a906288687eb33ea3793ef7546325671d2567"
NEG_RISK = False
TOKEN_ID = "72792907195268398845443386525224185661725156722691300716831141152265445297332"
PROXY_WALLET = "0x5e5c250a0ea6416da2b56325619f8ccd4734c668"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

ERC1155_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

PAYOUT_DENOMINATOR_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

SAFE_INFO_ABI = [
    {"name": "nonce", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "getOwners", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "address[]"}], "stateMutability": "view"},
    {"name": "getThreshold", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

EXPECTED_SELECTOR = "0xbaa51c0f"


def mask(value: str) -> str:
    """Show first 6 chars + ***."""
    if len(value) <= 6:
        return value + "***"
    return value[:6] + "***"


def short_cid(cid: str) -> str:
    """Shorten a condition ID for display."""
    if len(cid) > 16:
        return cid[:10] + "..." + cid[-5:]
    return cid


def main() -> None:
    # ================================================================
    # STEP 1 — Load and validate env vars
    # ================================================================
    print("[STEP 1] Loading environment variables...")
    load_dotenv()

    required_vars = {
        "PRIVATE_KEY": "PRIVATE_KEY",
        "EOA_ADDRESS": "EOA_ADDRESS",
        "PROXY_WALLET": "PROXY_WALLET",
        "POLYGON_RPC_URL": "POLYGON_RPC_URL",
    }

    env_values: dict[str, str] = {}
    missing = []
    for env_name, display_name in required_vars.items():
        val = os.environ.get(env_name, "").strip()
        if not val:
            missing.append(env_name)
            print(f"  {display_name:<20} = MISSING")
        else:
            env_values[env_name] = val
            print(f"  {display_name:<20} = {mask(val)}")

    if missing:
        print(f"[STEP 1] ABORT - Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    print("[STEP 1] OK - All env vars present.\n")

    # ── Connect to Polygon RPC ───────────────────────────────────────
    POLYGON_RPC_URL = env_values["POLYGON_RPC_URL"]
    PRIVATE_KEY = env_values["PRIVATE_KEY"]
    EOA_ADDRESS = env_values["EOA_ADDRESS"]

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
    if not w3.is_connected():
        print(f"[ERROR] Cannot connect to Polygon RPC: {POLYGON_RPC_URL}")
        sys.exit(1)

    proxy_checksum = Web3.to_checksum_address(PROXY_WALLET)
    eoa_checksum = Web3.to_checksum_address(EOA_ADDRESS)

    # ── Pre-flight: check EOA has POL for gas ────────────────────────
    pol_balance = w3.eth.get_balance(eoa_checksum)
    pol_human = w3.from_wei(pol_balance, "ether")
    print(f"  EOA POL balance:  {pol_human} POL")
    if pol_balance < w3.to_wei(0.01, "ether"):
        print("[ERROR] EOA has less than 0.01 POL. Fund it before redemption.")
        print(f"  Send POL to: {EOA_ADDRESS}")
        sys.exit(1)
    print()

    # ================================================================
    # STEP 2 — Check USDC balance before redemption
    # ================================================================
    print("[STEP 2] Checking USDC balance of PROXY_WALLET before redemption...")
    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_BALANCE_ABI
    )
    balance_before_raw = usdc_contract.functions.balanceOf(proxy_checksum).call()
    balance_before = balance_before_raw / 1_000_000
    print(f"  Raw balance (wei):    {balance_before_raw}")
    print(f"  Human balance (USDC): {balance_before}")
    print("[STEP 2] OK - Balance recorded.\n")

    # ================================================================
    # STEP 3 — Check CTF token balance for the winning token
    # ================================================================
    print("[STEP 3] Checking CTF token balance for TOKEN_ID...")
    ctf_contract = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_BALANCE_ABI
    )
    token_id_int = int(TOKEN_ID)
    token_short = TOKEN_ID[:5] + "..." + TOKEN_ID[-4:]
    ctf_balance_raw = ctf_contract.functions.balanceOf(proxy_checksum, token_id_int).call()
    ctf_balance = ctf_balance_raw / 1_000_000
    print(f"  Token ID (short):     {token_short}")
    print(f"  Raw balance:          {ctf_balance_raw}")
    print(f"  Human balance:        {ctf_balance} tokens")
    if ctf_balance_raw == 0:
        print("  [WARNING] CTF token balance is 0. Market may already be redeemed "
              "or position not held by PROXY_WALLET.")
    print("[STEP 3] OK - Token balance recorded.\n")

    # ================================================================
    # STEP 4 — Verify condition is resolved on-chain
    # ================================================================
    print("[STEP 4] Checking on-chain resolution status for condition...")
    ctf_payout = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS), abi=PAYOUT_DENOMINATOR_ABI
    )
    condition_bytes = bytes.fromhex(CONDITION_ID.removeprefix("0x"))
    payout_denominator = ctf_payout.functions.payoutDenominator(condition_bytes).call()
    resolved = payout_denominator > 0
    print(f"  conditionId (hex):    {short_cid(CONDITION_ID)}")
    print(f"  payoutDenominator:    {payout_denominator}")
    print(f"  Resolved on-chain:    {'YES' if resolved else 'NO'}")
    if not resolved:
        print("[STEP 4] ABORT - Condition is NOT resolved on-chain. Cannot redeem yet.")
        sys.exit(1)
    print("[STEP 4] OK - Condition is resolved. Safe to redeem.\n")

    # ================================================================
    # STEP 5 — Encode redeemPositions calldata
    # ================================================================
    print("[STEP 5] Encoding redeemPositions calldata...")
    from redeemer import encode_redeem_calldata

    contract_address, calldata = encode_redeem_calldata(CONDITION_ID, NEG_RISK)
    calldata_hex = "0x" + calldata.hex()
    selector = calldata_hex[:10]
    calldata_bytes_len = len(calldata)

    contract_label = "NegRisk Adapter" if NEG_RISK else "CTF"
    print(f"  Target contract:      {contract_address} ({contract_label})")
    print(f"  Function selector:    {selector} ", end="")
    if selector == EXPECTED_SELECTOR:
        print("✓ (matches redeemPositions)")
    else:
        print(f"✗ (expected {EXPECTED_SELECTOR})")
    print(f"  Full calldata (hex):  {calldata_hex[:40]}...")
    print(f"  Calldata length:      {calldata_bytes_len} bytes")
    if selector != EXPECTED_SELECTOR:
        print("[STEP 5] WARNING - Selector mismatch! Calldata may be incorrect.")
    else:
        print("[STEP 5] OK - Calldata encoded correctly.\n")

    # ================================================================
    # STEP 6 — Verify Safe on-chain
    # ================================================================
    print("[STEP 6] Loading Safe info from chain...")
    try:
        safe_contract = w3.eth.contract(
            address=proxy_checksum, abi=SAFE_INFO_ABI
        )
        safe_nonce = safe_contract.functions.nonce().call()
        owners = safe_contract.functions.getOwners().call()
        threshold = safe_contract.functions.getThreshold().call()
        print(f"  Safe address:         {PROXY_WALLET}")
        print(f"  Owners:               {owners}")
        print(f"  Threshold:            {threshold}")
        print(f"  Nonce:                {safe_nonce}")
        assert threshold == 1, f"Expected 1-of-1 Safe, got threshold={threshold}"
        assert EOA_ADDRESS.lower() in [o.lower() for o in owners], \
            "EOA is not an owner of this Safe!"
        print("[STEP 6] OK - Safe verified. EOA is owner, threshold=1.\n")
    except Exception:
        print("[STEP 6] FAILED - Could not load Safe info:")
        traceback.print_exc()
        sys.exit(1)

    # ================================================================
    # STEP 7 — Build signature and submit execTransaction
    # ================================================================
    print("[STEP 7] Submitting execTransaction on-chain...")
    tx_hash_hex = ""
    final_state = ""
    try:
        from redeemer import build_caller_approved_signature, SAFE_ABI, ZERO_ADDRESS

        signature = build_caller_approved_signature(EOA_ADDRESS)

        print(f"  to (CTF):             {contract_address}")
        print(f"  calldata:             {calldata_hex[:40]}...")
        print(f"  safe_nonce:           {safe_nonce}")
        print(f"  signature:            0x{signature.hex()} ({len(signature)} bytes)")
        print(f"  sig type:             caller-approved (v=1, no EIP-712 needed)")

        safe_full = w3.eth.contract(
            address=proxy_checksum, abi=SAFE_ABI
        )
        eoa_nonce = w3.eth.get_transaction_count(eoa_checksum)

        tx = safe_full.functions.execTransaction(
            Web3.to_checksum_address(contract_address),
            0, calldata, 0, 0, 0, 0,
            Web3.to_checksum_address(ZERO_ADDRESS),
            Web3.to_checksum_address(ZERO_ADDRESS),
            signature,
        ).build_transaction({
            "from": eoa_checksum,
            "nonce": eoa_nonce,
            "gas": 300_000,
            "maxFeePerGas": w3.to_wei("100", "gwei"),
            "maxPriorityFeePerGas": w3.to_wei("30", "gwei"),
        })

        print(f"  EOA nonce:            {eoa_nonce}")
        print(f"  Gas limit:            {tx['gas']}")

        signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()
        print(f"  TX submitted:         {tx_hash_hex}")
        print(f"  Polygonscan:          https://polygonscan.com/tx/{tx_hash_hex}")
        print("[STEP 7] OK - Transaction submitted.\n")
    except Exception:
        print("[STEP 7] FAILED - execTransaction raised an exception:")
        traceback.print_exc()
        sys.exit(1)

    # ================================================================
    # STEP 8 — Wait for confirmation
    # ================================================================
    print("[STEP 8] Waiting for confirmation...")
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        final_state = "SUCCESS" if receipt["status"] == 1 else "REVERTED"
        print(f"  Status:               {final_state}")
        print(f"  Block:                {receipt['blockNumber']}")
        print(f"  Gas used:             {receipt['gasUsed']}")
        if receipt["status"] == 0:
            print("[STEP 8] FAILED - TX reverted. Check Polygonscan for revert reason.\n")
        else:
            print("[STEP 8] OK - Confirmed on-chain.\n")
    except Exception:
        print("[STEP 8] FAILED - wait_for_transaction_receipt raised an exception:")
        traceback.print_exc()
        final_state = "ERROR"
        print()

    # ================================================================
    # STEP 9 — Check USDC balance after and compute delta
    # ================================================================
    print("[STEP 9] Checking USDC balance after redemption...")
    balance_after_raw = usdc_contract.functions.balanceOf(proxy_checksum).call()
    balance_after = balance_after_raw / 1_000_000
    delta = balance_after - balance_before
    print(f"  Balance before:       {balance_before} USDC")
    print(f"  Balance after:        {balance_after} USDC")
    print(f"  Delta:               {'+' if delta >= 0 else ''}{delta:.6f} USDC")
    if delta > 0:
        print(f"[STEP 9] OK - Redemption successful. Received {delta:.6f} USDC.\n")
    else:
        print("[STEP 9] WARNING - Balance did not increase. "
              "Redemption may have failed silently.\n")

    # ================================================================
    # STEP 10 — Final summary
    # ================================================================
    success = delta > 0
    print("=" * 60)
    print(" REDEMPTION TEST SUMMARY")
    print("=" * 60)
    print(f"  Condition ID:         {short_cid(CONDITION_ID)}")
    print(f"  Neg Risk:             {NEG_RISK}")
    print(f"  CTF tokens held:      {ctf_balance}")
    print(f"  USDC before:          {balance_before}")
    print(f"  USDC after:           {balance_after}")
    print(f"  USDC received:        {'+' if delta >= 0 else ''}{delta:.6f}")
    print(f"  TX hash:              {tx_hash_hex or 'N/A'}")
    print(f"  Final state:          {final_state or 'N/A'}")
    print(f"  Result:               {'SUCCESS ✓' if success else 'FAILED ✗'}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n[FATAL] Unexpected error:")
        traceback.print_exc()
        sys.exit(1)
