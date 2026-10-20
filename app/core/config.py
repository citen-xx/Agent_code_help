"""Application configuration (Pydantic Settings).

Values are read from environment variables, with `.env` file support.
See `.env.example` for the available options.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings.

    The ``LLM_*`` fields are reserved for the upcoming agent implementation
    and are not consumed anywhere yet.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "java-code-agent"
    APP_ENV: str = "development"

    # --- LLM provider ---
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = ""

    # --- Workspace ---
    WORKSPACE_ROOT: str = ".workspaces"
    WORKSPACE_CLONE_TIMEOUT_SECONDS: float = 300.0
    BUILD_TEST_TIMEOUT_SECONDS: float = 300.0
    DOCKER_MAVEN_IMAGE: str = "maven:3.9-eclipse-temurin-17"
    DOCKER_MAVEN_CPUS: str = "1.0"
    DOCKER_MAVEN_MEMORY: str = "1g"
    DOCKER_MAVEN_PIDS_LIMIT: int = 256
    DOCKER_MAVEN_TMPFS_SIZE: str = "256m"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


settings = get_settings()
