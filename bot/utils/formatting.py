from __future__ import annotations

from datetime import datetime

from bot.db.models import OrderStatus


STATUS_EMOJI = {
    OrderStatus.WAITING_PAYMENT: "💳",
    OrderStatus.ATTENTION: "🛠",
    OrderStatus.EXPIRED: "⌛",
    OrderStatus.PENDING: "⏳",
    OrderStatus.PROCESSING: "🔄",
    OrderStatus.SUCCESS: "✅",
    OrderStatus.FAILED: "❌",
    OrderStatus.UNCERTAIN: "⚠️",
}


def format_price(amount: float) -> str:
    """Format price in RUB."""
    if amount == int(amount):
        return f"{int(amount)} ₽"
    return f"{amount:.2f} ₽"


def format_status(status: OrderStatus) -> str:
    emoji = STATUS_EMOJI.get(status, "❓")
    labels = {
        OrderStatus.WAITING_PAYMENT: "Ожидает оплаты",
        OrderStatus.ATTENTION: "Требуется помощь поддержки",
        OrderStatus.EXPIRED: "Счёт истёк",
        OrderStatus.PENDING: "Ожидание",
        OrderStatus.PROCESSING: "В обработке",
        OrderStatus.SUCCESS: "Выполнен",
        OrderStatus.FAILED: "Ошибка",
        OrderStatus.UNCERTAIN: "Уточняется",
    }
    return f"{emoji} {labels.get(status, status.value)}"


def format_datetime(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def truncate(text: str, max_len: int = 100) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
