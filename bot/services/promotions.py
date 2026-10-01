from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import CatalogPromotion, Order, OrderStatus, PromotionClaim
from bot.services.partner_api import Product
from bot.services.pricing import calculate_user_price
from bot.utils.money import money


class PromotionError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogQuote:
    regular_price: float
    payable_price: float
    claim_id: int | None = None
    promo_code: str | None = None

    @property
    def discount(self) -> float:
        return float(money(self.regular_price) - money(self.payable_price))


class PromotionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def normalize(code: str) -> str:
        code = code.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{1,32}", code):
            raise PromotionError("Промокод не найден. Проверьте написание.")
        return code

    async def get_campaign(self, code: str) -> CatalogPromotion | None:
        return await self.session.scalar(
            select(CatalogPromotion).where(CatalogPromotion.code == self.normalize(code))
        )

    async def create(self, code: str, product_id: int, limit: int) -> CatalogPromotion:
        if product_id <= 0 or not 1 <= limit <= 1000:
            raise PromotionError("Укажите ID товара и лимит от 1 до 1000.")
        campaign = CatalogPromotion(code=self.normalize(code), product_id=product_id, max_claims=limit)
        self.session.add(campaign)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise PromotionError("Такой промокод уже существует; его лимит не изменён.") from exc
        return campaign

    async def _existing_claim(self, campaign_id: int, user_id: int) -> PromotionClaim | None:
        return await self.session.scalar(
            select(PromotionClaim).where(
                PromotionClaim.promotion_id == campaign_id, PromotionClaim.user_id == user_id
            )
        )

    @staticmethod
    def _check_unused(claim: PromotionClaim) -> None:
        if claim.order_id is not None:
            raise PromotionError("Этот промокод уже применён к вашему заказу.")

    async def activate(self, code: str, user_id: int) -> CatalogPromotion:
        campaign = await self.get_campaign(code)
        if campaign is None:
            raise PromotionError("Промокод не найден. Проверьте написание.")
        campaign_id = campaign.id
        existing = await self._existing_claim(campaign_id, user_id)
        if existing:
            self._check_unused(existing)
            return campaign
        # The conditional update serializes the last slot; inserting the claim
        # and incrementing the count commit together or roll back together.
        try:
            reserved = await self.session.scalar(
                update(CatalogPromotion)
                .where(
                    CatalogPromotion.id == campaign_id,
                    CatalogPromotion.is_active.is_(True),
                    CatalogPromotion.claimed_count < CatalogPromotion.max_claims,
                )
                .values(claimed_count=CatalogPromotion.claimed_count + 1)
                .returning(CatalogPromotion.id)
            )
            if reserved is None:
                await self.session.rollback()
                existing = await self._existing_claim(campaign_id, user_id)
                if existing:
                    self._check_unused(existing)
                    return await self.get_campaign(code)
                raise PromotionError("Акция завершена: новые купоны больше не выдаются.")
            self.session.add(PromotionClaim(promotion_id=campaign_id, user_id=user_id))
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._existing_claim(campaign_id, user_id)
            if existing is None:
                raise
            self._check_unused(existing)
        return await self.get_campaign(code)

    async def quote(self, user_id: int, product: Product, qty: int, markup: float) -> CatalogQuote:
        if not 1 <= qty <= 99:
            raise ValueError("Количество от 1 до 99")
        regular_unit = money(calculate_user_price(product.price, markup))
        regular = regular_unit * qty
        supplier_unit = money(product.price)
        if supplier_unit <= 0 or regular_unit <= supplier_unit:
            return CatalogQuote(float(regular), float(regular))
        row = (
            await self.session.execute(
                select(PromotionClaim.id, CatalogPromotion.code)
                .join(CatalogPromotion, CatalogPromotion.id == PromotionClaim.promotion_id)
                .where(
                    PromotionClaim.user_id == user_id,
                    PromotionClaim.order_id.is_(None),
                    CatalogPromotion.product_id == product.id,
                )
                .order_by(PromotionClaim.id)
                .limit(1)
            )
        ).first()
        if row is None:
            return CatalogQuote(float(regular), float(regular))
        # Only one unit is discounted, including in a multi-unit order.
        return CatalogQuote(float(regular), float(regular_unit * (qty - 1) + supplier_unit), row.id, row.code)

    async def reserve_for_order(self, claim_id: int, user_id: int, product_id: int, order_id: int) -> bool:
        reserved = await self.session.scalar(
            update(PromotionClaim)
            .where(
                PromotionClaim.id == claim_id,
                PromotionClaim.user_id == user_id,
                PromotionClaim.order_id.is_(None),
                PromotionClaim.promotion_id.in_(
                    select(CatalogPromotion.id).where(CatalogPromotion.product_id == product_id)
                ),
            )
            .values(order_id=order_id)
            .returning(PromotionClaim.id)
        )
        return reserved is not None

    async def restore_for_order(self, order_id: int) -> None:
        await self.session.execute(
            update(PromotionClaim).where(PromotionClaim.order_id == order_id).values(order_id=None)
        )

    async def stats(self, code: str) -> tuple[CatalogPromotion, int, int]:
        campaign = await self.get_campaign(code)
        if campaign is None:
            raise PromotionError("Промокод не найден.")
        completed = await self.session.scalar(
            select(func.count(PromotionClaim.id)).join(Order, Order.id == PromotionClaim.order_id)
            .where(PromotionClaim.promotion_id == campaign.id, Order.status == OrderStatus.SUCCESS)
        )
        reserved = await self.session.scalar(
            select(func.count(PromotionClaim.id)).join(Order, Order.id == PromotionClaim.order_id)
            .where(PromotionClaim.promotion_id == campaign.id, Order.status != OrderStatus.SUCCESS)
        )
        return campaign, completed or 0, reserved or 0
