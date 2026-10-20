"""OpenAI-compatible LLM client factory.

Wraps LangChain's ChatOpenAI. Only the ChatModel layer is used — no
high-level agent APIs anywhere in the project.
"""

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Per-request timeout for a single LLM call.
LLM_CALL_TIMEOUT_SECONDS = 60.0


class LLMConfigError(RuntimeError):
    """Raised when the LLM is not fully configured (see .env.example)."""


def build_chat_model() -> BaseChatModel:
    """Build an OpenAI-compatible chat model from application settings.

    Raises LLMConfigError when LLM_API_KEY / LLM_BASE_URL / LLM_MODEL are
    not configured in the environment or .env.
    """
    if not settings.LLM_API_KEY:
        raise LLMConfigError("LLM_API_KEY 未配置：请在项目根目录 .env 中填写（参考 .env.example）")
    if not settings.LLM_BASE_URL:
        raise LLMConfigError("LLM_BASE_URL 未配置：请在项目根目录 .env 中填写（参考 .env.example）")
    if not settings.LLM_MODEL:
        raise LLMConfigError("LLM_MODEL 未配置：请在项目根目录 .env 中填写（参考 .env.example）")

    logger.info(
        "Creating OpenAI-compatible chat model (model=%s, base_url=%s)",
        settings.LLM_MODEL,
        settings.LLM_BASE_URL,
    )
    return ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=0,
        timeout=LLM_CALL_TIMEOUT_SECONDS,
        max_retries=2,
    )
