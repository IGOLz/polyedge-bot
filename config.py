import os

import httpx
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


# ── Authentication (unchanged) ──────────────────────────────────────────
PRIVATE_KEY: str = _require("PRIVATE_KEY")
API_KEY: str = _require("POLYMARKET_API_KEY")
API_SECRET: str = _require("POLYMARKET_API_SECRET")
API_PASSPHRASE: str = _require("POLYMARKET_API_PASSPHRASE")
PROXY_WALLET: str = _require("PROXY_WALLET")
EOA_ADDRESS: str = _require("EOA_ADDRESS")

CLOB_BASE_URL = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

# ── Proxy (unchanged) ───────────────────────────────────────────────────
PROXY_URL: str = os.getenv("PROXY_URL", "").strip()

# ── PostgreSQL ──────────────────────────────────────────────────────────
POSTGRES_HOST: str = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT: int = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER: str = os.getenv("POSTGRES_USER", "polymarket")
POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "")
POSTGRES_DB: str = os.getenv("POSTGRES_DB", "polymarket_tracker")

# ── Strategy toggles ────────────────────────────────────────────────────
STRATEGY_MOMENTUM_ENABLED: bool = os.getenv("STRATEGY_MOMENTUM_ENABLED", "true").lower() == "true"

# ── Betting ──────────────────────────────────────────────────────────────
BET_SIZE_USD: float = float(os.getenv("BET_SIZE_USD", "1.5"))
DAILY_LOSS_LIMIT: float = float(os.getenv("DAILY_LOSS_LIMIT", "30.0"))
LOOP_INTERVAL: int = int(os.getenv("LOOP_INTERVAL", "5"))

# ── Dry-run mode (set via --dry-run CLI flag) ────────────────────────────
DRY_RUN: bool = False


# ── HTTP helpers (unchanged) ────────────────────────────────────────────
def get_http_client(**kwargs) -> httpx.AsyncClient:
    """Create a proxy-aware async HTTP client."""
    if PROXY_URL:
        kwargs.setdefault("proxy", PROXY_URL)
    kwargs.setdefault("timeout", 30.0)
    return httpx.AsyncClient(**kwargs)


def get_sync_http_client(**kwargs) -> httpx.Client:
    """Create a proxy-aware sync HTTP client."""
    if PROXY_URL:
        kwargs.setdefault("proxy", PROXY_URL)
    kwargs.setdefault("timeout", 30.0)
    return httpx.Client(**kwargs)


def patch_clob_client_proxy(proxy_url: str = "") -> None:
    """Replace py_clob_client's internal httpx.Client with a proxy-aware one."""
    if not proxy_url:
        return

    import py_clob_client.http_helpers.helpers as clob_helpers

    clob_helpers._http_client = httpx.Client(
        proxy=proxy_url,
        timeout=30.0,
    )


