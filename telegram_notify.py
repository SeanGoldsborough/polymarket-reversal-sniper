#!/usr/bin/env python3
"""
Telegram Notification Helper for Polymarket Bots
==================================================
Sends alerts to your phone via Telegram.

SETUP:
  1. Open Telegram, search for @BotFather
  2. Send /newbot, follow prompts, get your BOT_TOKEN
  3. Search for @userinfobot, send /start, get your CHAT_ID
  4. Add to .env:
     TELEGRAM_BOT_TOKEN=your_bot_token_here
     TELEGRAM_CHAT_ID=your_chat_id_here

USAGE:
  from telegram_notify import TelegramNotifier
  notifier = TelegramNotifier()
  await notifier.send("Trade opened: BTC UP @ $0.80")
"""

import os
import aiohttp
from datetime import datetime, timezone


class TelegramNotifier:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self.last_error_time = 0
        self.error_cooldown = 300  # don't spam errors more than once per 5 min

    async def send(self, message: str, silent: bool = False):
        """Send a message to Telegram. Fails silently if not configured."""
        if not self.enabled:
            return

        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_notification": silent,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        pass  # silently fail
        except Exception:
            pass  # never crash the bot for a notification failure

    async def send_trade(self, action: str, trade_id: int, direction: str,
                         price: float, pnl: float = None, reason: str = None,
                         bankroll: float = None, bot_name: str = "ENDGAME"):
        """Send a formatted trade notification."""
        if action == "OPEN":
            msg = (f"🟢 <b>{bot_name}</b> OPEN #{trade_id}\n"
                   f"Direction: {direction}\n"
                   f"Entry: ${price:.2f}\n"
                   f"Bankroll: ${bankroll:.2f}" if bankroll else "")
        else:
            emoji = "✅" if pnl and pnl > 0 else "❌"
            msg = (f"{emoji} <b>{bot_name}</b> CLOSE #{trade_id}\n"
                   f"Exit: ${price:.2f}\n"
                   f"P&L: ${pnl:+.2f}\n"
                   f"Reason: {reason}\n"
                   f"Bankroll: ${bankroll:.2f}" if bankroll else "")
        await self.send(msg)

    async def send_status(self, bankroll: float, pnl: float, trades: int,
                          win_rate: float, drawdown: float, runtime_min: float,
                          bot_name: str = "ENDGAME"):
        """Send periodic status update."""
        msg = (f"📊 <b>{bot_name} Status</b>\n"
               f"Bankroll: ${bankroll:,.2f} (${pnl:+,.2f})\n"
               f"Trades: {trades} | Win: {win_rate:.0f}%\n"
               f"Max DD: {drawdown:.1f}%\n"
               f"Runtime: {runtime_min:.0f}min")
        await self.send(msg, silent=True)

    async def send_error(self, error_msg: str, bot_name: str = "ENDGAME"):
        """Send error alert (rate-limited to avoid spam)."""
        import time
        now = time.time()
        if now - self.last_error_time < self.error_cooldown:
            return
        self.last_error_time = now
        msg = f"⚠️ <b>{bot_name} ERROR</b>\n{error_msg}"
        await self.send(msg)

    async def send_startup(self, bot_name: str = "ENDGAME", mode: str = "PAPER"):
        """Send startup notification."""
        msg = f"🚀 <b>{bot_name}</b> started in {mode} mode"
        await self.send(msg)

    async def send_shutdown(self, bot_name: str = "ENDGAME", bankroll: float = 0,
                            pnl: float = 0, trades: int = 0):
        """Send shutdown notification."""
        msg = (f"🛑 <b>{bot_name}</b> stopped\n"
               f"Final bankroll: ${bankroll:,.2f} (${pnl:+,.2f})\n"
               f"Total trades: {trades}")
        await self.send(msg)
