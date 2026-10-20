"""Tests for the LLM client factory (construction only — no network)."""

import pytest

from app.core import config
from app.llm.client import LLMConfigError, build_chat_model


def test_build_chat_model_requires_api_key(monkeypatch):
    monkeypatch.setattr(config.settings, "LLM_API_KEY", "")
    with pytest.raises(LLMConfigError):
        build_chat_model()


def test_build_chat_model_requires_base_url(monkeypatch):
    monkeypatch.setattr(config.settings, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config.settings, "LLM_BASE_URL", "")
    with pytest.raises(LLMConfigError):
        build_chat_model()


def test_build_chat_model_requires_model_name(monkeypatch):
    monkeypatch.setattr(config.settings, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config.settings, "LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(config.settings, "LLM_MODEL", "")
    with pytest.raises(LLMConfigError):
        build_chat_model()


def test_build_chat_model_constructs_with_config(monkeypatch):
    monkeypatch.setattr(config.settings, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config.settings, "LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(config.settings, "LLM_MODEL", "test-model")
    model = build_chat_model()
    assert model.model_name == "test-model"
