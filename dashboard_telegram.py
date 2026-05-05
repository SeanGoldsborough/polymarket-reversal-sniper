#!/usr/bin/env python3
"""Sends strategy dashboard to Telegram every 5 minutes."""
import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BOT_TOKEN = "8027434003:AAEZPOsAFCXBjdxAdY8gmWGo9-PQwEir-0E"
CHAT_ID = "7142537098"
INTERVAL = 300  # 5 minutes

BOTS = [
    ("LATENCY-ARB", "/home/ubuntu/polymarket-bot/logs/latency_arb_summary.json"),
    ("IRONCLAD-BTC", "/home/ubuntu/polymarket-bot/logs/ironclad_btc_summary.json"),
    ("IRONCLAD-SOL", "/home/ubuntu/polymarket-bot/logs/ironclad_sol_summary.json"),
    ("Paper MM", "/home/ubuntu/polymarket-bot/logs/paper_market_maker_summary.json"),
    ("Paper SOL", "/home/ubuntu/polymarket-bot/logs/paper_sol_omega_summary.json"),
    ("Paper Omega", "/home/ubuntu/polymarket-bot/logs/paper_omega_summary.json"),
    ("Paper E", "/home/ubuntu/polymarket-bot/logs/paper_strategy_e_summary.json"),
    ("Paper F", "/home/ubuntu/polymarket-bot/logs/paper_strategy_f_summary.json"),
    ("Paper G", "/home/ubuntu/polymarket-bot/logs/paper_strategy_g_summary.json"),
    ("Paper H", "/home/ubuntu/polymarket-bot/logs/paper_strategy_h_summary.json"),
]


def send_telegram(msg):
    try:
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def format_standalone_bot(name, path):
    """Format a standalone bot (non-variant) for the dashboard."""
    try:
        with open(path) as f:
            d = json.load(f)
        trades = d.get("trades", 0)
        pnl = d.get("pnl_total", 0)
        bankroll = d.get("bankroll", 100)
        wr = d.get("win_rate", 0)
        elapsed = d.get("elapsed_minutes", 0)

        if trades == 0:
            return f"⏳ <b>{name}</b>: ${bankroll:,.2f} | waiting ({elapsed:.0f}min)"

        icon = "🟢" if pnl > 0 else ("🔴" if pnl < -1 else "⚪")
        extra = ""
        if "both_sl" in d:
            extra = f" | 1W1SL={d.get('one_win_one_sl',0)} 2SL={d.get('both_sl',0)}"
        if d.get("execution_failures", 0) > 0:
            extra += f" | execFail={d['execution_failures']}"
        sign = "+" if pnl >= 0 else ""
        return f"{icon} <b>{name}</b>: ${bankroll:,.2f} ({sign}{pnl:,.2f}) | {trades}t {wr:.0f}%{extra}"
    except FileNotFoundError:
        return None
    except Exception:
        return None


def build_dashboard():
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [f"📊 <b>Strategy Dashboard</b> — {now}", ""]

    standalone_names = {"LATENCY-ARB", "IRONCLAD-BTC", "IRONCLAD-SOL", "Paper MM", "Paper SOL"}
    standalone_lines = []
    for bot_name, path in BOTS:
        if bot_name in standalone_names:
            line = format_standalone_bot(bot_name, path)
            if line:
                standalone_lines.append(line)

    if standalone_lines:
        lines.append("<b>Active Bots:</b>")
        lines.extend(standalone_lines)
        lines.append("")

    all_variants = []
    for bot_name, path in BOTS:
        if bot_name in standalone_names:
            continue
        try:
            with open(path) as f:
                d = json.load(f)
            for k, v in d.get("variants", {}).items():
                trades = v.get("trades", 0)
                if trades == 0:
                    continue
                all_variants.append({
                    "key": k,
                    "trades": trades,
                    "wins": v.get("wins", 0),
                    "losses": v.get("losses", 0),
                    "wr": v.get("win_rate", 0),
                    "bankroll": v.get("bankroll", 100),
                    "pnl": v.get("pnl_total", 0),
                    "peak": v.get("peak", 100),
                })
        except Exception:
            pass

    all_variants.sort(key=lambda x: x["bankroll"], reverse=True)

    if all_variants:
        lines.append("<b>Top Paper Strategies:</b>")
        for v in all_variants[:10]:
            pnl = v["pnl"]
            sign = "+" if pnl >= 0 else ""
            icon = "🟢" if pnl > 0 else ("🔴" if pnl < -1 else "⚪")
            lines.append(
                f"{icon} <b>{v['key']}</b>: ${v['bankroll']:,.2f} ({sign}{pnl:,.2f})"
                f" | {v['trades']}t {v['wr']:.0f}%"
            )

    if len(all_variants) > 10:
        lines.append("")
        lines.append("<b>Bottom:</b>")
        for v in all_variants[-10:]:
            pnl = v["pnl"]
            sign = "+" if pnl >= 0 else ""
            lines.append(f"🔴 {v['key']}: ${v['bankroll']:.2f} ({sign}{pnl:.2f})")

    return "\n".join(lines)


def main():
    print(f"Dashboard bot started. Sending to Telegram every {INTERVAL}s.")
    while True:
        try:
            msg = build_dashboard()
            send_telegram(msg)
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Dashboard sent")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
