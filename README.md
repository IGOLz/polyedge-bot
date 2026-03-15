# Polymarket Copy-Trading Bot

Monitors a target wallet on Polymarket and automatically mirrors its trades with flat sizing, bankroll protection, and ROI tracking. Uses **signature_type=2** (Gnosis Safe proxy mode) so orders appear in the Polymarket UI and funds stay in your proxy wallet.

## Prerequisites

- Python 3.11+
- A Polymarket account with USDC deposited via polymarket.com
- VPN if you are in a restricted region

## Installation

```bash
pip install -r requirements.txt
```

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Deposit USDC.e to your Polymarket account via [polymarket.com](https://polymarket.com) (this puts funds in the proxy wallet automatically).

3. Generate API credentials:

```bash
python setup.py
```

4. Copy `.env.example` to `.env` and fill in all values:

```bash
cp .env.example .env
```

| Variable | How to get it |
|---|---|
| `PRIVATE_KEY` | Export from MetaMask (0x...) |
| `POLYMARKET_API_KEY` | Output of `setup.py` |
| `POLYMARKET_API_SECRET` | Output of `setup.py` |
| `POLYMARKET_API_PASSPHRASE` | Output of `setup.py` |
| `PROXY_WALLET` | Polymarket profile settings -> address |
| `TARGET_ADDRESS` | Wallet address you want to copy |

5. Run diagnostics to confirm everything works:

```bash
python debug.py
```

You should see your proxy wallet balance (e.g. `$86.76 USDC`) and no errors.

6. Start the bot:

```bash
python main.py
```

Positions will now appear in your polymarket.com UI.

The bot will poll every `POLL_INTERVAL` seconds (default 4), detect new trades from the target, and place flat-sized mirror limit orders.

Logs go to both the console and `bot.log`. ROI stats are persisted in `stats.json`.

## How It Works (Proxy Mode)

The bot uses `signature_type=2` which means:
- Your **EOA** (MetaMask wallet) signs orders
- Your **proxy wallet** (Gnosis Safe) holds the funds
- Orders appear in the Polymarket web UI
- Approvals are handled automatically by Polymarket's relayer

No need to run `approve_usdc.py` — that script is only for legacy EOA mode.

## Sizing Strategy

- **Flat sizing**: every copied trade uses `FIXED_SIZE` dollars (default $5)
- **Bankroll guard**: if USDC balance drops below `FIXED_SIZE * 5` ($25), trading pauses automatically until topped up
- **Daily limit**: spending is capped at `DAILY_LIMIT` per UTC day (default $25) to prevent a single bad day from draining more than ~1/3 of bankroll
- **Win detector**: after placing an order, the bot polls its outcome every 30s and logs wins in green

## Configuration

| Variable | Description | Default |
|---|---|---|
| `PRIVATE_KEY` | Your wallet private key (0x...) | required |
| `POLYMARKET_API_KEY` | From `setup.py` | required |
| `POLYMARKET_API_SECRET` | From `setup.py` | required |
| `POLYMARKET_API_PASSPHRASE` | From `setup.py` | required |
| `PROXY_WALLET` | Polymarket proxy wallet (profile settings) | required |
| `TARGET_ADDRESS` | Wallet to copy-trade | required |
| `FIXED_SIZE` | Dollars per trade (flat sizing) | `5.0` |
| `DAILY_LIMIT` | Max spend per UTC day | `25.0` |
| `POLL_INTERVAL` | Seconds between polls | `4` |

## One-Time Transfer (Optional)

If you have USDC.e stuck in the EOA and need to move it to the proxy wallet:

```bash
python transfer_to_proxy.py
```

This transfers all USDC.e from the EOA to the proxy wallet address.
