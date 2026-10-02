from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    partner_api_key: str
    partner_api_base: str = "https://api.thegodapishop.xyz"
    database_url: str = "sqlite+aiosqlite:///./data.db"
    markup_percent: float = 15.0
    admin_ids: list[int] = Field(default_factory=list)
    log_level: str = "INFO"
    rate_limit_per_sec: int = 8
    steam_min_amount: int = 100
    steam_max_amount: int = 15000
    proxy_url: str | None = None
    direct_supplier_checkout_enabled: bool = True
    aethel_api_key: str = ""
    aethel_api_base: str = "https://mail-api.hvmforum.space/api"
    aethel_usd_rub_rate: float = Field(default=0, ge=0, allow_inf_nan=False)

    # Payment providers
    cryptobot_token: str = ""  # from @CryptoBot -> My Apps
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    freekassa_shop_id: str = ""
    freekassa_secret_1: str = ""
    freekassa_secret_2: str = ""
    freekassa_base_url: str = "https://pay.freekassa.net"
    freekassa_webhook_host: str = "0.0.0.0"
    freekassa_webhook_port: int = 8080

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def is_sqlite(self) -> bool:
        return "sqlite" in self.database_url


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
