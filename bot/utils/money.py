from decimal import Decimal, InvalidOperation


def money(value: object) -> Decimal:
    """Reject non-finite values and fractions smaller than a kopeck."""
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Некорректная сумма") from exc
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
        raise ValueError("Сумма должна быть конечной и содержать не более двух знаков после запятой")
    return result


def topup_amount(value: object) -> int:
    amount = money(value)
    if not Decimal(50) <= amount <= Decimal(50000):
        raise ValueError("Сумма от 50 до 50000 ₽")
    return int(amount.to_integral_value(rounding="ROUND_CEILING"))
