"""
Access control middleware.
If allowed_user_ids is empty, all users are allowed (public mode).
"""
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_ids: list[int]) -> None:
        self.allowed_ids = set(allowed_ids)
        super().__init__()

    async def __call__(
        self,
        handler: Callable,
        event: Message | CallbackQuery,
        data: Dict[str, Any],
    ) -> Any:
        if not self.allowed_ids:
            return await handler(event, data)

        user = event.from_user

        # Временный лог для диагностики
        import logging
        logging.getLogger(__name__).warning(
            f"Access check: user_id={user.id if user else None}, allowed={self.allowed_ids}"
        )

        if user and user.id in self.allowed_ids:
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("⛔ Access denied.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Access denied.", show_alert=True)
        return None
