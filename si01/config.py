from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", env_ignore_empty=True)

    bot_token: str = ""
    admin_chat_id: int = 0
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_aux_model: str = "claude-opus-5-5"
    llm_max_tokens: int = 16000

    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'si01.db'}"

    tribute_api_key: str = ""
    tribute_payment_url: str = ""
    tribute_subscription_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # HTTP-сервер: вебхук Tribute + админка.
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080

    # Веб-админка (/admin). Пусто = выключена.
    admin_panel_password: str = ""
    # Ключ подписи cookie; пусто = выводится из пароля (смена пароля разлогинивает всех).
    admin_panel_secret: str = ""
    # Часовой пояс для дат в админке, часы от UTC (3 = Москва).
    admin_panel_utc_offset: int = 3

    stt_enabled: bool = False
    stt_base_url: str = "https://api.openai.com/v1"
    stt_api_key: str = ""
    stt_model: str = "whisper-1"

    session_idle_hours: int = 48
    log_retention_days: int = 60
    # Сколько последних реплик сессии отдаём модели дословно; более старые сжимаются в summary.
    history_window: int = 40

    facts_path: Path = ROOT / "config" / "facts.yaml"
    greetings_path: Path = ROOT / "config" / "greetings.yaml"
    prompt_path: Path = ROOT / "prompts" / "system.md"

    @field_validator("admin_ids", "tribute_subscription_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
