"""
/settings — настройки уведомлений пользователя.
Хранение в памяти (MVP).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

# MVP: хранение настроек в памяти
# user_id -> dict
USER_SETTINGS: dict[int, dict] = {}

DEFAULT_SETTINGS = {
    "alerts_enabled": True,
    "min_score": 45,
    "notify_early_warning": False,
    "notify_overheated": True,
    "notify_reversal_risk": True,
    "notify_dump_started": True,
    "quiet_mode": False,
}


def get_user_settings(user_id: int) -> dict:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = DEFAULT_SETTINGS.copy()
    return USER_SETTINGS[user_id]


def settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    s = get_user_settings(user_id)
    builder = InlineKeyboardBuilder()

    # Уведомления вкл/выкл
    alerts_label = "🔔 Уведомления: ВКЛ ✅" if s["alerts_enabled"] else "🔕 Уведомления: ВЫКЛ ❌"
    builder.button(text=alerts_label, callback_data="settings:toggle:alerts_enabled")

    # Минимальный score
    score_options = [30, 45, 60, 75]
    for score in score_options:
        marker = "✅ " if s["min_score"] == score else ""
        builder.button(
            text=f"{marker}Score ≥{score}",
            callback_data=f"settings:score:{score}",
        )

    # Типы сигналов
    signal_types = [
        ("notify_early_warning", "⚠️ Раннее предупреждение"),
        ("notify_overheated", "🔥 Перегрев"),
        ("notify_reversal_risk", "⬇️ Риск разворота"),
        ("notify_dump_started", "💥 Слив начался"),
    ]
    for key, label in signal_types:
        marker = "✅" if s[key] else "❌"
        builder.button(
            text=f"{marker} {label}",
            callback_data=f"settings:toggle:{key}",
        )

    # Тихий режим
    quiet_label = "🌙 Тихий режим: ВКЛ ✅" if s["quiet_mode"] else "🌙 Тихий режим: ВЫКЛ ❌"
    builder.button(text=quiet_label, callback_data="settings:toggle:quiet_mode")

    # Сброс
    builder.button(text="🔄 Сбросить настройки", callback_data="settings:reset")

    builder.adjust(1, 4, 1, 1, 1, 1, 1, 1)
    return builder.as_markup()


def _format_settings(user_id: int) -> str:
    s = get_user_settings(user_id)

    alerts_em = "✅ ВКЛ" if s["alerts_enabled"] else "❌ ВЫКЛ"
    quiet_em = "🌙 ВКЛ" if s["quiet_mode"] else "💡 ВЫКЛ"

    signal_lines = ""
    signal_map = {
        "notify_early_warning": "⚠️ Раннее предупреждение",
        "notify_overheated": "🔥 Перегрев",
        "notify_reversal_risk": "⬇️ Риск разворота",
        "notify_dump_started": "💥 Слив начался",
    }
    for key, label in signal_map.items():
        em = "✅" if s[key] else "❌"
        signal_lines += f"  {em} {label}\n"

    return (
        f"⚙️ <b>Настройки уведомлений</b>\n\n"
        f"🔔 Уведомления: <b>{alerts_em}</b>\n"
        f"📊 Минимальный score: <b>{s['min_score']}</b>\n"
        f"🌙 Тихий режим: <b>{quiet_em}</b>\n\n"
        f"<b>Типы сигналов:</b>\n{signal_lines}\n"
        f"<i>Нажмите кнопку для изменения настройки</i>"
    )


@router.message(Command("settings"))
async def cmd_settings(msg: Message) -> None:
    if not msg.from_user:
        return
    user_id = msg.from_user.id
    await msg.answer(
        _format_settings(user_id),
        reply_markup=settings_keyboard(user_id),
    )


@router.callback_query(F.data.startswith("settings:toggle:"))
async def cb_settings_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    if not query.from_user:
        return

    user_id = query.from_user.id
    key = query.data.split(":")[-1]
    s = get_user_settings(user_id)

    if key in s:
        s[key] = not s[key]
        logger.info("Setting toggled", user_id=user_id, key=key, value=s[key])

    try:
        await query.message.edit_text(
            _format_settings(user_id),
            reply_markup=settings_keyboard(user_id),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("settings:score:"))
async def cb_settings_score(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    if not query.from_user:
        return

    user_id = query.from_user.id
    score = int(query.data.split(":")[-1])
    s = get_user_settings(user_id)
    s["min_score"] = score

    logger.info("Min score changed", user_id=user_id, score=score)

    try:
        await query.message.edit_text(
            _format_settings(user_id),
            reply_markup=settings_keyboard(user_id),
        )
    except Exception:
        pass


@router.callback_query(F.data == "settings:reset")
async def cb_settings_reset(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Настройки сброшены")
    except Exception:
        pass

    if not query.from_user:
        return

    user_id = query.from_user.id
    USER_SETTINGS[user_id] = DEFAULT_SETTINGS.copy()

    try:
        await query.message.edit_text(
            _format_settings(user_id),
            reply_markup=settings_keyboard(user_id),
        )
    except Exception:
        pass