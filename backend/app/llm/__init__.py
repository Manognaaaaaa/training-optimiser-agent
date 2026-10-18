from app.config import settings
from app.llm.base import LLMClient, LLMError


def llm_configured() -> bool:
    return bool(settings.groq_api_key)


def get_llm_client() -> LLMClient:
    """FastAPI dependency. Tests override it with a FakeLLMClient."""
    if not llm_configured():
        raise LLMError("unavailable", "GROQ_API_KEY is not set in backend/.env")
    from app.llm.groq_client import build_groq_client

    return build_groq_client()
