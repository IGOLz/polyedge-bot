import os
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

load_dotenv()

# Polygon RPC
RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# Contract addresses
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

OPERATORS = [
    ("CLOB Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("Neg Risk Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
]

CTF_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"}
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"}
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    }
]

def main():
    private_key = os.getenv("PRIVATE_KEY")
    proxy_wallet = os.getenv("PROXY_WALLET")

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    account = Account.from_key(private_key)
    eoa_address = account.address

    print(f"EOA: {eoa_address}")
    print(f"Proxy wallet: {proxy_wallet}")

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=CTF_ABI
    )

    # Check and set approval for each operator
    print("\n--- On-chain CTF approvals ---")
    for name, operator in OPERATORS:
        try:
            is_approved = ctf.functions.isApprovedForAll(
                Web3.to_checksum_address(proxy_wallet),
                Web3.to_checksum_address(operator)
            ).call()
            print(f"{name} ({operator[:10]}...): {is_approved}")

            if not is_approved:
                print(f"Setting approval for {name}...")
                nonce = w3.eth.get_transaction_count(eoa_address, 'pending')
                tx = ctf.functions.setApprovalForAll(
                    Web3.to_checksum_address(operator),
                    True
                ).build_transaction({
                    'from': eoa_address,
                    'nonce': nonce,
                    'gas': 100000,
                    'gasPrice': w3.eth.gas_price,
                    'chainId': 137
                })
                signed = account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                print(f"Done — status: {'success' if receipt.status == 1 else 'failed'}")
            else:
                print(f"{name}: already approved")
        except Exception as e:
            print(f"Error checking/setting {name}: {e}")

    # CLOB client allowances
    print("\n--- CLOB client allowances ---")
    creds = ApiCreds(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
    )
    client = ClobClient(
        "https://clob.polymarket.com",
        key=private_key, chain_id=137,
        creds=creds, signature_type=2,
        funder=proxy_wallet,
    )

    print("Updating USDC (collateral) allowance via CLOB client...")
    client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print("USDC allowance set")
    try:
        print("Updating conditional token allowance via CLOB client...")
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
        print("Conditional token allowance set")
    except Exception as e:
        print(f"Conditional token allowance skipped (requires specific token ID): {e}")
    print("\nAll approvals complete")

if __name__ == "__main__":
    main()
