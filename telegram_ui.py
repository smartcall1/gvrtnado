# telegram_ui.py
import logging
import aiohttp
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

BTN_STATUS = "📊 Status"
BTN_FUNDING = "💰 Funding"
BTN_HISTORY = "📋 History"
BTN_POSITIONS = "📌 Positions"
BTN_CLOSE = "🔚 Close Now"
BTN_STOP = "⏹ Stop"
BTN_KILL = "💀 Kill"

# legacy alias
BTN_RESYNC = BTN_CLOSE
BTN_REBALANCE = BTN_CLOSE

KEYBOARD = {
    "keyboard": [
        [BTN_STATUS, BTN_FUNDING],
        [BTN_HISTORY, BTN_POSITIONS],
        [BTN_CLOSE, BTN_STOP],
        [BTN_KILL],
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

    async def flush_pending_updates(self):
        """봇 오프라인 동안 쌓인 대기 업데이트 전부 무시 (Stop 누적 방지)"""
        if not self.enabled:
            return
        await self._ensure_session()
        try:
            async with self._session.get(
                f"{self._base}/getUpdates",
                params={"offset": -1, "timeout": 0},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("result", [])
                    if results:
                        self._offset = results[-1]["update_id"] + 1
                        logger.info("텔레그램 대기 업데이트 %d건 flush", len(results))
        except Exception as e:
            logger.debug("텔레그램 flush 실패 (무시): %s", e)

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
                params={"offset": self._offset, "timeout": 2},
                timeout=aiohttp.ClientTimeout(total=8),
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
