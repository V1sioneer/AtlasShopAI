from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OrderType(str, enum.Enum):
    CATALOG = "catalog"
    STEAM = "steam"
    GAME = "game"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class DepositStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # TG user_id
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    balance_rub: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="user", lazy="selectin")
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="user", lazy="selectin"
    )


class Order(Base):
    __tablename__ = "orders"

    request_key: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    partner_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    type: Mapped[OrderType] = mapped_column(Enum(OrderType))
    product_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    variation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    partner_price: Mapped[float] = mapped_column(Float, default=0.0)
    user_price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.PENDING
    )
    delivered_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="orders")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    delta: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(255))
    order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="transactions")


class PartnerDeposit(Base):
    __tablename__ = "partner_deposits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deposit_id_partner: Mapped[int] = mapped_column(Integer, unique=True)
    method: Mapped[str] = mapped_column(String(20))  # crypto | ton
    amount_rub: Mapped[float] = mapped_column(Float)
    amount_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pay_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    wallet: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    memo: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GameProduct(Base):
    """Admin-managed game products for the /games section."""
    __tablename__ = "game_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    variation_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    fields_schema: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Deposit(Base):
    """User balance top-up via payment gateways."""
    __tablename__ = "deposits"
    __table_args__ = (
        UniqueConstraint("method", "external_id", name="uq_deposits_method_external_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    amount_rub: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(20))  # cryptobot | yookassa | card | sbp
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    pay_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | paid | expired
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

