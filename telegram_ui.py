# telegram_ui.py
import logging
import aiohttp
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

BTN_STATUS = "📊 Status"
BTN_HISTORY = "📋 History"
BTN_EARN = "💰 Earn"
BTN_FUNDING = "📈 Funding"
BTN_REBALANCE = "🔄 Rebalance"
BTN_STOP = "⏹ Stop"
BTN_SETBOOST = "🎯 SetBoost"

KEYBOARD = {
    "keyboard": [
        [BTN_STATUS, BTN_HISTORY, BTN_EARN],
        [BTN_FUNDING, BTN_REBALANCE, BTN_STOP],
        [BTN_SETBOOST],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


class TelegramUI:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{token}"
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset = 0
        self._callbacks: dict[str, Callable[..., Awaitable]] = {}
        self._text_handler: Optional[Callable[..., Awaitable]] = None
        self.enabled = bool(token and chat_id)

    async def _ensure_session(self):
        if not self._session:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    def register_callback(self, button: str, handler: Callable[..., Awaitable]):
        self._callbacks[button] = handler

    def register_text_handler(self, handler: Callable[..., Awaitable]):
        self._text_handler = handler

    async def send_message(self, text: str, with_keyboard: bool = True):
        if not self.enabled:
            return
        await self._ensure_session()
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if with_keyboard:
            import json
            payload["reply_markup"] = json.dumps(KEYBOARD)
        try:
            async with self._session.post(
                f"{self._base}/sendMessage", json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Telegram send failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")

    async def send_alert(self, text: str):
        await self.send_message(text, with_keyboard=False)

    async def poll_updates(self):
        if not self.enabled:
            return
        await self._ensure_session()
        try:
            async with self._session.get(
                f"{self._base}/getUpdates",
                params={"offset": self._offset, "timeout": 1},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    if str(msg.get("chat", {}).get("id")) != self._chat_id:
                        continue
                    text = msg.get("text", "")
                    if text in self._callbacks:
                        await self._callbacks[text]()
                    elif text.startswith("/") and self._text_handler:
                        await self._text_handler(text)
        except Exception as e:
            logger.debug(f"Telegram poll: {e}")
