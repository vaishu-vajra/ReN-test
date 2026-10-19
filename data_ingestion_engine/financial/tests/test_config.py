import importlib

import pytest

from data_ingestion_engine.financial import config


def test_import_without_prompt_name_does_not_raise(monkeypatch):
    monkeypatch.delenv("FINANCIAL_PROMPT_NAME", raising=False)
    importlib.reload(config)
    assert config.FINANCIAL_PROMPT_NAME is None


def test_require_raises_when_unset_and_returns_when_set(monkeypatch):
    monkeypatch.delenv("FINANCIAL_PROMPT_NAME", raising=False)
    importlib.reload(config)
    with pytest.raises(RuntimeError, match="FINANCIAL_PROMPT_NAME is not set"):
        config.require("FINANCIAL_PROMPT_NAME")

    monkeypatch.setenv("FINANCIAL_PROMPT_NAME", "my-prompt")
    importlib.reload(config)
    assert config.require("FINANCIAL_PROMPT_NAME") == "my-prompt"
