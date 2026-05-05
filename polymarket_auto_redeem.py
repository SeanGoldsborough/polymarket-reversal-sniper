#!/usr/bin/env python3
"""
Polymarket Auto-Redeemer
Checks for redeemable positions and redeems them via Gnosis Safe on Polygon.
Runs as a cron job via OpenClaw.
"""
import os
import sys
import json
import urllib.request

# Config from env
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY")
PROXY_WALLET = os.environ.get("POLYMARKET_PROXY_WALLET", "0x7Fb17449872d8E330D523062879134d3B071D7F1")
EOA = os.environ.get("POLYMARKET_EOA", "0xB3d0525e60cE5D5238cC5b5Ec28E344b8831C6F3")
RPC = os.environ.get("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8027434003:AAEZPOsAFCXBjdxAdY8gmWGo9-PQwEir-0E")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7142537098")
NOTIFY_EMPTY = os.environ.get("NOTIFY_EMPTY", "true").lower() == "true"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CHAIN_ID = 137

if not PRIVATE_KEY:
    print("ERROR: POLYMARKET_PRIVATE_KEY not set")
    sys.exit(1)

try:
    from web3 import Web3
    from eth_account import Account
    from eth_account.messages import encode_defunct
except ImportError:
    print("Installing dependencies...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "web3", "eth-account", "--quiet"])
    from web3 import Web3
    from eth_account import Account
    from eth_account.messages import encode_defunct

def telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.parse
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram notify failed: {e}")

def fetch_redeemable():
    url = f"https://data-api.polymarket.com/positions?user={PROXY_WALLET}&sizeThreshold=0&limit=500"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return [p for p in data if p.get("redeemable")]

SAFE_ABI = [
    {"name": "execTransaction", "type": "function", "inputs": [
        {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
        {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
        {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
        {"name": "refundReceiver", "type": "address"}, {"name": "signatures", "type": "bytes"},
    ], "outputs": [{"name": "success", "type": "bool"}], "stateMutability": "payable"},
    {"name": "nonce", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "getTransactionHash", "type": "function", "inputs": [
        {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
        {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
        {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
        {"name": "refundReceiver", "type": "address"}, {"name": "nonce", "type": "uint256"},
    ], "outputs": [{"name": "", "type": "bytes32"}], "stateMutability": "view"},
]

CTF_ABI = [{"name": "redeemPositions", "type": "function", "inputs": [
    {"name": "collateralToken", "type": "address"}, {"name": "parentCollectionId", "type": "bytes32"},
    {"name": "conditionId", "type": "bytes32"}, {"name": "indexSets", "type": "uint256[]"},
], "outputs": [], "stateMutability": "nonpayable"}]

def main():
    positions = fetch_redeemable()
    if not positions:
        print("No redeemable positions found.")
        if NOTIFY_EMPTY:
            telegram("🔍 Polymarket: no redeemable positions found.")
        return

    total = sum(p.get("currentValue", 0) for p in positions)
    print(f"Found {len(positions)} redeemable position(s), total ~${total:.2f} USDC")

    w3 = Web3(Web3.HTTPProvider(RPC))
    account = Account.from_key(PRIVATE_KEY)
    safe = w3.eth.contract(address=Web3.to_checksum_address(PROXY_WALLET), abi=SAFE_ABI)
    ctf  = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)
    ZERO_BYTES32 = b'\x00' * 32
    ZERO_ADDR = "0x0000000000000000000000000000000000000000"

    nonce = safe.functions.nonce().call()
    redeemed = []
    failed = []

    for p in positions:
        label = p.get("title", p["conditionId"][:20])
        outcome_index = p.get("outcomeIndex", 0)
        index_set = 1 << outcome_index
        cid = bytes.fromhex(p["conditionId"][2:])
        value = p.get("currentValue", 0)

        print(f"\nRedeeming: {label} | {p.get('outcome')} | ${value:.4f}")
        try:
            call_data = ctf.encode_abi("redeemPositions", [
                Web3.to_checksum_address(USDC), ZERO_BYTES32, cid, [index_set]
            ])
            safe_tx_hash = safe.functions.getTransactionHash(
                Web3.to_checksum_address(CTF), 0, call_data, 0, 0, 0, 0, ZERO_ADDR, ZERO_ADDR, nonce
            ).call()

            signed = Account.sign_message(encode_defunct(safe_tx_hash), private_key=PRIVATE_KEY)
            r = signed.r.to_bytes(32, 'big')
            s = signed.s.to_bytes(32, 'big')
            v = (signed.v + 4).to_bytes(1, 'big')
            sig_bytes = r + s + v

            # Always fetch fresh EOA nonce to avoid stale nonce issues
            eoa_nonce = w3.eth.get_transaction_count(EOA, 'pending')
            gas_price = w3.eth.gas_price
            tx = safe.functions.execTransaction(
                Web3.to_checksum_address(CTF), 0, call_data, 0, 0, 0, 0, ZERO_ADDR, ZERO_ADDR, sig_bytes
            ).build_transaction({
                'from': EOA,
                'nonce': eoa_nonce,
                'gas': 300000,
                'gasPrice': int(gas_price * 2.0),
                'chainId': CHAIN_ID,
            })

            signed_tx = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            print(f"  Sent: {tx_hash.hex()}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                print(f"  SUCCESS: https://polygonscan.com/tx/{tx_hash.hex()}")
                redeemed.append({"label": label, "value": value, "tx": tx_hash.hex()})
                nonce += 1
            else:
                print(f"  FAILED (reverted): {tx_hash.hex()}")
                failed.append(label)
                nonce += 1
            # Brief pause between txs
            import time; time.sleep(2)

        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append(label)

    print(f"\n=== Done: {len(redeemed)} redeemed, {len(failed)} failed ===")
    total_claimed = sum(r["value"] for r in redeemed)
    print(f"Total claimed: ${total_claimed:.2f} USDC")
    if failed:
        print(f"Failed: {', '.join(failed)}")

    # Telegram summary
    if redeemed:
        lines = [f"✅ Polymarket: redeemed {len(redeemed)} position(s) for ${total_claimed:.2f} USDC"]
        for r in redeemed:
            lines.append(f"  • {r['label']} — ${r['value']:.4f}")
        if failed:
            lines.append(f"⚠️ Failed: {', '.join(failed)}")
        telegram("\n".join(lines))
    elif failed:
        telegram(f"⚠️ Polymarket: {len(failed)} redemption(s) failed: {', '.join(failed)}")

if __name__ == "__main__":
    main()
