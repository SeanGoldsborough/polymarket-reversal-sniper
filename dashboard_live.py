#!/usr/bin/env python3
"""Live trading dashboard - tracks from deposit date."""
import json
import time
import urllib.request
import urllib.parse
import os
from datetime import datetime, timezone

BOT_TOKEN = "8027434003:AAEZPOsAFCXBjdxAdY8gmWGo9-PQwEir-0E"
CHAT_ID = "7142537098"
INTERVAL = 300
STARTING_WALLET = 101.21
DEPOSIT_TIME = "2026-04-22T01:54:00"  # Only count trades after this

BOTS = [
    ("REVERSAL-FIXED", "logs/reversal_fixed_trades.jsonl", None, "reversalFIXED"),
    ("REVERSAL-MID", "logs/reversal_mid_trades.jsonl", None, "reversalMID"),
    ("SNIPE-ALT-LIVE", "logs/snipe_alt_live_trades.jsonl", None, "snipeALTlive"),
]

def send_telegram(msg):
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_wallet():
    try:
        import sys
        sys.path.insert(0, "/home/ubuntu/polymarket-bot")
        from dotenv import load_dotenv
        load_dotenv("/home/ubuntu/polymarket-bot/.env")
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams
        client = ClobClient("https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"), chain_id=137,
            signature_type=int(os.getenv("SIGNATURE_TYPE", "2")),
            funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"))
        client.set_api_creds(client.create_or_derive_api_creds())
        params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
        result = client.get_balance_allowance(params)
        return int(result.get("balance", "0")) / 1_000_000
    except:
        return None

def is_session_running(name):
    try:
        result = os.popen(f"tmux has-session -t {name} 2>&1").read()
        return "no session" not in result and "error" not in result
    except:
        return False

def read_trades_since_deposit(path):
    trades = []
    try:
        with open(f"/home/ubuntu/polymarket-bot/{path}") as f:
            for line in f:
                t = json.loads(line)
                if t.get("time", "") >= DEPOSIT_TIME:
                    trades.append(t)
    except: pass
    return trades

def build_dashboard():
    now = datetime.now(timezone.utc)
    utc_str = now.strftime("%H:%M UTC")
    et_hour = (now.hour - 4) % 24
    et_min = now.strftime("%M")
    am_pm = "AM" if et_hour < 12 else "PM"
    et_12 = et_hour if 1 <= et_hour <= 12 else (et_hour - 12 if et_hour > 12 else 12)
    et_str = f"{et_12}:{et_min} {am_pm} ET"

    wallet = get_wallet()
    if wallet is None: wallet = 0
    wallet_pnl = wallet - STARTING_WALLET
    wsign = "+" if wallet_pnl >= 0 else "-"

    lines = []
    lines.append("\U0001F1EE\U0001F1EA\U0001F4C8 <b>LIVE Trading Dashboard</b>")
    lines.append(f"{utc_str} / {et_str}")
    lines.append(f"\U0001F4B0 Wallet: <b>${wallet:,.2f}</b> ({wsign}${abs(wallet_pnl):,.2f})")
    lines.append(f"Started: ${STARTING_WALLET}")
    lines.append("")

    total_trades = 0
    total_wins = 0
    total_losses = 0

    for bot_name, log_path, vf, tmux_name in BOTS:
        running = is_session_running(tmux_name)
        trades = read_trades_since_deposit(log_path)
        wins = [t for t in trades if t.get("pnl", 0) > 0.01]
        losses_list = [t for t in trades if t.get("pnl", 0) < -0.01]
        n = len(trades)
        w = len(wins)
        lo = len(losses_list)
        total_trades += n
        total_wins += w
        total_losses += lo

        if n == 0:
            if running:
                lines.append(f"\u23F3 <b>{bot_name}</b> | waiting")
            else:
                lines.append(f"\u26D4 <b>{bot_name}</b> | stopped")
        else:
            wr = w / n * 100 if n else 0
            last_t = trades[-1]
            last_result = last_t.get("result", "?")
            last_pnl = last_t.get("pnl", 0)
            lsign = "+" if last_pnl >= 0 else "-"

            if not running:
                icon = "\u26D4"
            elif w > lo:
                icon = "\U0001F7E2"
            elif lo > w:
                icon = "\U0001F534"
            else:
                icon = "\u26AA"

            lines.append(f"{icon} <b>{bot_name}</b> | {n}t {wr:.0f}% ({w}W/{lo}L)")
            lines.append(f"   Last: {last_result} {lsign}${abs(last_pnl):.2f}")

    lines.append("")
    lines.append(f"\U0001F4CA {total_trades}t total ({total_wins}W/{total_losses}L)")

    return lines

def check_bot_health():
    """Check if bots are still running. Alert if any died."""
    alerts = []
    for bot_name, log_path, vf, tmux_name in BOTS:
        if not is_session_running(tmux_name):
            alerts.append(f"\u274C <b>{bot_name}</b> is DOWN!")
    return alerts

def main():
    print("Live dashboard started.", flush=True)
    last_health_check = 0
    alerted_down = set()
    
    while True:
        try:
            lines = build_dashboard()
            msg = chr(10).join(lines)
            send_telegram(msg)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Sent", flush=True)
            
            # Health check every cycle — alert immediately if bot dies
            alerts = check_bot_health()
            for alert in alerts:
                bot_name = alert.split("<b>")[1].split("</b>")[0] if "<b>" in alert else ""
                if bot_name not in alerted_down:
                    send_telegram(f"\U0001F6A8 <b>SERVER ALERT</b>\n{alert}\nMay need Stop/Start from AWS console.")
                    alerted_down.add(bot_name)
                    print(f"[{ts}] ALERT: {bot_name} down!", flush=True)
            
            # Clear alerts for bots that came back
            running_bots = set()
            for _, _, _, tmux_name in BOTS:
                if is_session_running(tmux_name):
                    for bot_name, _, _, tn in BOTS:
                        if tn == tmux_name:
                            running_bots.add(bot_name)
            alerted_down -= running_bots
            
        except Exception as e:
            print(f"Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
