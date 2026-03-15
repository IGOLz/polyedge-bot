"""One-time script: approve Polymarket exchange contracts to spend USDC from EOA.

NOTE: With signature_type=2 (Gnosis Safe proxy mode), Polymarket handles
approvals automatically via their relayer when you deposit through polymarket.com.
This script is only needed for EOA (signature_type=0) mode.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

import config

load_dotenv()

POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
]
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_CONTRACTS = [USDC_E, USDC_NATIVE]
EXCHANGE_CONTRACTS = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
]

# Minimal ERC20 ABI — only balanceOf and approve
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "type": "function",
    },
]

MAX_UINT256 = 2**256 - 1


def main() -> None:
    print("=== USDC Approval for Polymarket Exchange ===\n")

    # 1. Connect to Polygon (try multiple RPCs)
    w3 = None
    for rpc in POLYGON_RPCS:
        try:
            candidate = Web3(Web3.HTTPProvider(rpc))
            if candidate.is_connected():
                print(f"Connected via {rpc}")
                w3 = candidate
                break
        except Exception:
            continue

    if w3 is None:
        print("ERROR: All RPCs failed. Check your internet connection.")
        sys.exit(1)

    # 2. Derive EOA from private key
    pk = config.PRIVATE_KEY
    if not pk.startswith("0x"):
        pk = "0x" + pk
    acct = w3.eth.account.from_key(pk)
    eoa = acct.address
    print(f"EOA address: {eoa}")

    # 3. Check POL balance for gas
    pol_balance = w3.eth.get_balance(eoa)
    pol_ether = w3.from_wei(pol_balance, "ether")
    print(f"\nPOL balance: {pol_ether:.4f} POL")
    if pol_ether < 0.01:
        print("ERROR: Not enough POL for gas. Get free POL at https://faucet.polygon.technology")
        sys.exit(1)

    # 4. Check USDC balances (both contracts)
    usdc_e = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    usdc_n = w3.eth.contract(address=Web3.to_checksum_address(USDC_NATIVE), abi=ERC20_ABI)

    eoa_e = usdc_e.functions.balanceOf(eoa).call() / 1e6
    eoa_n = usdc_n.functions.balanceOf(eoa).call() / 1e6
    print(f"\nUSDC.e in EOA:      ${eoa_e:.2f}")
    print(f"Native USDC in EOA: ${eoa_n:.2f}")
    print(f"Total USDC in EOA:  ${eoa_e + eoa_n:.2f}")

    proxy = config.PROXY_WALLET
    proxy_cs = Web3.to_checksum_address(proxy)
    proxy_e = usdc_e.functions.balanceOf(proxy_cs).call() / 1e6
    proxy_n = usdc_n.functions.balanceOf(proxy_cs).call() / 1e6
    print(f"\nUSDC.e in proxy:      ${proxy_e:.2f}")
    print(f"Native USDC in proxy: ${proxy_n:.2f}")
    print(f"Total USDC in proxy:  ${proxy_e + proxy_n:.2f}")

    if eoa_e + eoa_n + proxy_e + proxy_n == 0:
        print("\nWARNING: All wallets show $0 USDC.")

    # 5. Approve each exchange contract for BOTH USDC tokens
    total_approvals = len(EXCHANGE_CONTRACTS) * len(USDC_CONTRACTS)
    print(f"\nApproving {total_approvals} combinations ({len(USDC_CONTRACTS)} tokens x {len(EXCHANGE_CONTRACTS)} exchanges) …\n")

    nonce = w3.eth.get_transaction_count(eoa)

    for usdc_addr in USDC_CONTRACTS:
        usdc_cs = Web3.to_checksum_address(usdc_addr)
        usdc_contract = w3.eth.contract(address=usdc_cs, abi=ERC20_ABI)
        label = "USDC.e" if usdc_addr == USDC_E else "Native USDC"

        for exchange_addr in EXCHANGE_CONTRACTS:
            exchange_cs = Web3.to_checksum_address(exchange_addr)

            # Check current allowance first
            current = usdc_contract.functions.allowance(eoa, exchange_cs).call()
            if current >= MAX_UINT256 // 2:
                print(f"  [{label}] {exchange_addr} — already approved")
                continue

            try:
                tx = usdc_contract.functions.approve(exchange_cs, MAX_UINT256).build_transaction({
                    "from": eoa,
                    "gas": 100_000,
                    "gasPrice": w3.eth.gas_price,
                    "nonce": nonce,
                })
                signed_tx = w3.eth.account.sign_transaction(tx, pk)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                nonce += 1

                if receipt["status"] == 1:
                    print(f"  [{label}] Approved {exchange_addr} — tx: {tx_hash.hex()}")
                else:
                    print(f"  [{label}] FAILED {exchange_addr} — tx reverted: {tx_hash.hex()}")
            except Exception as e:
                print(f"  [{label}] ERROR approving {exchange_addr}: {e}")

    # 6. Verify via ClobClient
    print("\n=== Verifying via ClobClient ===")
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
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = clob.get_balance_allowance(params)
        print(f"Final balance/allowance: {result}")
    except Exception as e:
        print(f"ClobClient verification failed: {e}")
        print("(Approvals may still have succeeded — check PolygonScan)")

    print("\nDone — run python main.py to start the bot")


if __name__ == "__main__":
    main()
