from pydantic import field_validator, model_validator
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
    planner_context_char_budget: int = 16000  # keep each request under this many characters (about 9,000 tokens)
    planner_max_validate_calls: int = 3  # how many times the agent may check a draft plan before it must submit
    planner_min_lead_hours: int = 48  # planner policy (not a checker rule): new sessions must start at least this long from now
    planner_min_new_session_size: int = 3  # the prune step drops a new session left with fewer drivers than this
    planner_greedy_baseline: bool = True  # also build a deterministic rule-based plan in every run
    planner_duplicate_overlap: float = 0.8  # a sample sharing more than this fraction of its sessions and drivers with an earlier one is a duplicate
    planner_sample_pause_seconds: float = 3  # pause between samples so Groq's per-minute limits can recover

    # Ranking and alerts (Phase 6)
    score_weight_closure: float = 0.35
    score_weight_confidence: float = 0.20
    score_weight_fairness: float = 0.20
    score_weight_disruption: float = 0.15
    score_weight_resource: float = 0.10
    disruption_cap: float = 20  # drivers touched + 2 x sessions added that count as "maximum disruption"
    trainer_hours_cap: float = 16  # added trainer hours that count as "maximum resource use"
    top_k: int = 3  # plans shown as recommendations; the rest are collapsed
    fairness_flag_burden: float = 0.6  # flag a group that takes more than this share of the burden...
    fairness_flag_pool: float = 0.6  # ...while being under this share of the eligible pool
    alert_writer_temperature: float = 0.2
    auto_plan_on_high_risk: bool = False  # start plan generation for new high-risk alerts after the weekly close

    # Live updates (Phase 7)
    live_heartbeat_seconds: float = 15  # SSE heartbeat period

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _weights_sum_to_one(self):
        total = (
            self.score_weight_closure + self.score_weight_confidence + self.score_weight_fairness
            + self.score_weight_disruption + self.score_weight_resource
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"score weights must sum to 1, got {total}")
        return self

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
