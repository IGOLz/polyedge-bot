"""USDC balance checker via ClobClient get_balance_allowance (proxy wallet)."""

from __future__ import annotations

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

import config
from utils import log

# Module-level client (lazy init)
_clob: ClobClient | None = None


def _get_clob() -> ClobClient:
    global _clob
    if _clob is not None:
        return _clob
    creds = ApiCreds(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        api_passphrase=config.API_PASSPHRASE,
    )
    _clob = ClobClient(
        config.CLOB_BASE_URL,
        key=config.PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        creds=creds,
        signature_type=2,
        funder=config.PROXY_WALLET,
    )
    return _clob


async def get_usdc_balance() -> float:
    """Return USDC balance in dollars from proxy wallet via ClobClient."""
    try:
        clob = _get_clob()
        bal = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(bal.get("balance", "0")) / 1_000_000
    except Exception:
        log.exception("Failed to fetch USDC balance")
        return -1.0  # sentinel: unknown
