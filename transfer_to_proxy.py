"""One-time script: transfer all USDC.e from EOA to Polymarket proxy wallet."""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

import config

POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
]
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
EOA = config.EOA_ADDRESS
PROXY = config.PROXY_WALLET

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
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


def main() -> None:
    print("=== Transfer USDC.e from EOA to Proxy Wallet ===\n")

    # 1. Connect to Polygon
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
        print("ERROR: All RPCs failed.")
        sys.exit(1)

    # 2. Derive EOA account
    pk = config.PRIVATE_KEY
    if not pk.startswith("0x"):
        pk = "0x" + pk
    acct = w3.eth.account.from_key(pk)
    eoa = acct.address
    proxy_cs = Web3.to_checksum_address(PROXY)

    print(f"EOA:   {eoa}")
    print(f"Proxy: {proxy_cs}")

    # 3. Check USDC.e balance
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)
    raw_balance = usdc.functions.balanceOf(eoa).call()
    balance = raw_balance / 1e6

    print(f"\nUSDC.e in EOA: ${balance:.6f} ({raw_balance} raw)")

    if raw_balance == 0:
        print("Nothing to transfer.")
        sys.exit(0)

    # 4. Check POL for gas
    pol_balance = w3.from_wei(w3.eth.get_balance(eoa), "ether")
    print(f"POL balance:   {pol_balance:.4f} POL")
    if pol_balance < 0.005:
        print("ERROR: Not enough POL for gas.")
        sys.exit(1)

    # 5. Transfer 100% of USDC.e to proxy
    print(f"\nTransferring ${balance:.6f} USDC.e to proxy …")

    tx = usdc.functions.transfer(proxy_cs, raw_balance).build_transaction({
        "from": eoa,
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(eoa),
    })
    signed_tx = w3.eth.account.sign_transaction(tx, pk)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    print(f"TX sent: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] == 1:
        print("Transfer confirmed!")
    else:
        print("ERROR: Transaction reverted!")
        sys.exit(1)

    # 6. Confirm balances
    eoa_after = usdc.functions.balanceOf(eoa).call() / 1e6
    proxy_after = usdc.functions.balanceOf(proxy_cs).call() / 1e6
    print(f"\nEOA balance:   ${eoa_after:.6f}")
    print(f"Proxy balance: ${proxy_after:.6f}")
    print("\nDone! Funds are now in the proxy wallet.")


if __name__ == "__main__":
    main()
