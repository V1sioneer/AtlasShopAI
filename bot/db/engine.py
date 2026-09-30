from __future__ import annotations

from sqlalchemy import event, inspect, text, update

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.db.models import Base, Order, OrderStatus


def upgrade_schema(connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("orders")}
    if "request_key" not in columns:
        connection.execute(text("ALTER TABLE orders ADD COLUMN request_key VARCHAR(255)"))
    connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_request_key ON orders (request_key)"))
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_deposits_method_external_id "
            "ON deposits (method, external_id)"
        )
    )


def configure_sqlite(connection, _record) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(database_url: str) -> None:
    global _engine, _session_factory
    connect_args: dict = {}
    if "sqlite" in database_url:
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
    _engine = create_async_engine(
        database_url,
        echo=False,
        connect_args=connect_args,
    )
    if "sqlite" in database_url:
        event.listen(_engine.sync_engine, "connect", configure_sqlite)
    _session_factory = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(upgrade_schema)
        # A process may have died after the provider accepted a request. Never
        # submit it again or refund it until its outcome has been reconciled.
        await conn.execute(
            update(Order).where(Order.status == OrderStatus.PENDING)
            .values(status=OrderStatus.UNCERTAIN, error_code="PROCESS_INTERRUPTED")
        )


async def close_db() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    assert _session_factory is not None, "Database not initialised. Call init_db first."
    return _session_factory
