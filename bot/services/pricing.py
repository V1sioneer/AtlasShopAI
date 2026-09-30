from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
import math

from bot.utils.money import money


def calculate_user_price(partner_price: float, markup_percent: float) -> float:
    """Calculate user price with markup, rounded up to nearest 1 RUB."""
    if not math.isfinite(markup_percent) or not 0 <= markup_percent <= 500:
        raise ValueError("Некорректная наценка")
    raw = money(partner_price) * (1 + Decimal(str(markup_percent)) / 100)
    return float(raw.to_integral_value(rounding=ROUND_CEILING))
