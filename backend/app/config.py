from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class Settings(BaseSettings):
    app_name: str = "Training Optimiser API"
    database_url: str = "sqlite:///./training.db"
    frontend_origin: str = "http://localhost:5173"

    # LLM (Phase 5)
    groq_api_key: str = ""
    groq_model: str = DEFAULT_GROQ_MODEL
    groq_base_url: str = DEFAULT_GROQ_BASE_URL
    llm_timeout_seconds: float = 60
    planner_default_n: int = 5
    planner_max_n: int = 10
    planner_max_steps: int = 8
    planner_context_char_budget: int = 10000  # keep each request under this many characters (about 5,500 tokens)
    planner_sample_pause_seconds: float = 3  # pause between samples so Groq's per-minute limits can recover

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("groq_model", mode="before")
    @classmethod
    def _blank_model_means_default(cls, value):
        """An empty GROQ_MODEL= line in .env should fall back to the default, not become an empty model name."""
        return value or DEFAULT_GROQ_MODEL

    @field_validator("groq_base_url", mode="before")
    @classmethod
    def _blank_url_means_default(cls, value):
        return value or DEFAULT_GROQ_BASE_URL


settings = Settings()
