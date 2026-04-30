#!/usr/bin/env python3
"""Live trading dashboard — tracks all active bots, wallet, and P&L."""
import json
import time
import urllib.request
import urllib.parse
import os
from datetime import datetime, timezone

BOT_TOKEN = "8027434003:AAEZPOsAFCXBjdxAdY8gmWGo9-PQwEir-0E"
CHAT_ID = "7142537098"
INTERVAL = 300  # 5 minutes

LIVE_STARTING_WALLET = 100.98  # Fresh start Apr 29

# ── Bot definitions: (name, log_path, tmux_session, mode) ──
LIVE_BOTS = [
    ("SNIPE-ALT", "logs/snipe_alt_live_trades.jsonl", "snipeALTlive"),
    ("BTC-SNIPE-LADDER", "logs/btc_snipe_ladder_trades.jsonl", "btcSnipeLadder"),
    ("BTC-PENNY", "logs/btc_penny_trades.jsonl", "btcPenny"),
]

PAPER_BOTS = [
    ("BTC-LADDER", "logs/btc_ladder_trades.jsonl", "btcLadder"),
    ("SNIPE-ALL", "logs/snipe_paper_all_trades.jsonl", "snipePaperAll"),
]

PAPER_STARTING_BANKROLL = 100.0


def send_telegram(msg):
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def get_wallet():
    """Get CLOB balance (total tradeable)."""
    try:
        import sys
        sys.path.insert(0, "/home/ubuntu/polymarket-bot")
        from dotenv import load_dotenv
        load_dotenv("/home/ubuntu/polymarket-bot/.env")
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import BalanceAllowanceParams
        client = ClobClient("https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"), chain_id=137,
            signature_type=int(os.getenv("SIGNATURE_TYPE", "2")),
            funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"))
        client.set_api_creds(client.create_or_derive_api_key())
        params = BalanceAllowanceParams(asset_type="COLLATERAL", token_id="", signature_type=2)
        result = client.get_balance_allowance(params)
        return int(result.get("balance", "0")) / 1_000_000
    except:
        return None


def get_onchain_balance():
    """Get on-chain pUSD balance (available to trade without activation)."""
    try:
        import urllib.request as req2
        proxy = os.getenv("POLYMARKET_PROXY_WALLET", os.getenv("POLYMARKET_FUNDER_ADDRESS", "0x7Fb17449872d8E330D523062879134d3B071D7F1"))
        pusd = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        padded = proxy.lower().replace("0x", "").zfill(64)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": pusd, "data": f"0x70a08231{padded}"}, "latest"]})
        r = req2.Request("https://polygon-bor-rpc.publicnode.com",
            data=payload.encode(), headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        resp = req2.urlopen(r, timeout=10)
        data = json.loads(resp.read())
        return int(data.get("result", "0x0"), 16) / 1_000_000
    except:
        return None


def is_session_running(name):
    try:
        result = os.popen(f"tmux has-session -t {name} 2>&1").read()
        return "no session" not in result and "error" not in result
    except:
        return False


def read_trades(path):
    trades = []
    try:
        with open(f"/home/ubuntu/polymarket-bot/{path}") as f:
            for line in f:
                try:
                    trades.append(json.loads(line))
                except:
                    pass
    except:
        pass
    return trades


def read_summary(bot_name):
    """Read summary JSON for paper bots to get current bankroll."""
    summary_map = {
        "BTC-LADDER": "logs/btc_ladder_summary.json",
        "SNIPE-ALL": "logs/snipe_paper_all_summary.json",
        "BTC-BOTH-SIDES": "logs/btc_both_sides_summary.json",
    }
    path = summary_map.get(bot_name)
    if not path:
        return None
    try:
        with open(f"/home/ubuntu/polymarket-bot/{path}") as f:
            return json.load(f)
    except:
        return None


def format_bot_line(bot_name, trades, running):
    n = len(trades)
    w = sum(1 for t in trades if t.get("pnl", 0) > 0.01)
    lo = sum(1 for t in trades if t.get("pnl", 0) < -0.01)
    pnl = sum(t.get("pnl", 0) for t in trades)

    if n == 0:
        if running:
            return f"\u23F3 <b>{bot_name}</b> | waiting", 0, 0, 0, 0
        else:
            return f"\u26D4 <b>{bot_name}</b> | stopped", 0, 0, 0, 0

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

    line = f"{icon} <b>{bot_name}</b> | {n}t {wr:.0f}% ({w}W/{lo}L) ${pnl:+.2f}"
    line += f"\n   Last: {last_result} {'+'if last_pnl>=0 else '-'}${abs(last_pnl):.2f}"

    return line, n, w, lo, pnl


def build_dashboard():
    now = datetime.now(timezone.utc)
    utc_str = now.strftime("%H:%M UTC")
    et_hour = (now.hour - 4) % 24
    et_min = now.strftime("%M")
    am_pm = "AM" if et_hour < 12 else "PM"
    et_12 = et_hour if 1 <= et_hour <= 12 else (et_hour - 12 if et_hour > 12 else 12)
    et_str = f"{et_12}:{et_min} {am_pm} ET"

    wallet = get_wallet()
    if wallet is None:
        wallet = 0
    wallet_pnl = wallet - LIVE_STARTING_WALLET

    # Check for funds needing activation
    onchain = get_onchain_balance()
    needs_activation = False
    activation_amount = 0
    if onchain is not None and wallet > 0 and onchain < wallet - 2:
        needs_activation = True
        activation_amount = wallet - onchain

    lines = []
    lines.append("\U0001F1EE\U0001F1EA\U0001F4C8 <b>Trading Dashboard</b>")
    lines.append(f"{utc_str} / {et_str}")
    lines.append("")

    # ── ACTIVATION ALERT ──
    if needs_activation:
        lines.append(f"\u26A0\uFE0F <b>ACTIVATE FUNDS</b>: ${activation_amount:,.2f} needs activation on Polymarket UI")
        lines.append("")

    # ── LIVE SECTION ──
    lines.append("\U0001F4B0 <b>LIVE</b>")
    lines.append(f"Wallet: <b>${wallet:,.2f}</b> ({'+'if wallet_pnl>=0 else ''}{wallet_pnl:,.2f})")
    lines.append(f"Started: ${LIVE_STARTING_WALLET:,.2f}")
    lines.append("")

    live_total_t = 0
    live_total_w = 0
    live_total_l = 0

    for bot_name, log_path, tmux_name in LIVE_BOTS:
        running = is_session_running(tmux_name)
        trades = read_trades(log_path)
        line, n, w, lo, pnl = format_bot_line(bot_name, trades, running)
        lines.append(line)
        live_total_t += n
        live_total_w += w
        live_total_l += lo

    lines.append("")

    # ── PAPER SECTION ──
    lines.append("\U0001F4DD <b>PAPER</b>")

    for bot_name, log_path, tmux_name in PAPER_BOTS:
        running = is_session_running(tmux_name)
        summary = read_summary(bot_name)

        if summary and summary.get("trades", 0) > 0:
            n = summary.get("trades", 0)
            w = summary.get("wins", 0)
            lo = summary.get("losses", 0)
            pnl = summary.get("pnl_total", 0)
            paper_bank = summary.get("bankroll", PAPER_STARTING_BANKROLL)
            paper_start = summary.get("starting_bankroll", PAPER_STARTING_BANKROLL)
            paper_pnl = paper_bank - paper_start
            wr = w / n * 100 if n else 0

            if not running:
                icon = "\u26D4"
            elif w > lo:
                icon = "\U0001F7E2"
            elif lo > w:
                icon = "\U0001F534"
            else:
                icon = "\u26AA"

            lines.append(f"{icon} <b>{bot_name}</b> | {n}t {wr:.0f}% ({w}W/{lo}L) ${pnl:+.2f}")
            lines.append(f"   Bank: ${paper_bank:,.2f} ({'+'if paper_pnl>=0 else ''}{paper_pnl:,.2f}) | Start: ${paper_start:.2f}")
        else:
            if running:
                lines.append(f"\u23F3 <b>{bot_name}</b> | waiting")
            else:
                lines.append(f"\u26D4 <b>{bot_name}</b> | stopped")

    lines.append("")

    # ── TOTALS ──
    lines.append(f"\U0001F4CA Live: {live_total_t}t ({live_total_w}W/{live_total_l}L)")

    return lines


def check_bot_health():
    alerts = []
    for bot_name, log_path, tmux_name in LIVE_BOTS:
        if not is_session_running(tmux_name):
            alerts.append(f"\u274C <b>{bot_name}</b> (LIVE) is DOWN!")
    for bot_name, log_path, tmux_name in PAPER_BOTS:
        if not is_session_running(tmux_name):
            alerts.append(f"\u26A0 <b>{bot_name}</b> (PAPER) is DOWN")
    return alerts


def main():
    print("Dashboard started.", flush=True)
    alerted_down = set()

    while True:
        try:
            lines = build_dashboard()
            msg = chr(10).join(lines)
            send_telegram(msg)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Sent", flush=True)

            # Health check
            alerts = check_bot_health()
            for alert in alerts:
                bot_name = alert.split("<b>")[1].split("</b>")[0] if "<b>" in alert else ""
                if bot_name not in alerted_down:
                    send_telegram(f"\U0001F6A8 <b>SERVER ALERT</b>\n{alert}")
                    alerted_down.add(bot_name)
                    print(f"[{ts}] ALERT: {bot_name} down!", flush=True)

            # Clear alerts for bots that came back
            running_bots = set()
            for bot_name, _, tmux_name in LIVE_BOTS + PAPER_BOTS:
                if is_session_running(tmux_name):
                    running_bots.add(bot_name)
            alerted_down -= running_bots

        except Exception as e:
            print(f"Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
