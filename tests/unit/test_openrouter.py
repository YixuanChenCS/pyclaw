from pathlib import Path

from pyclaw.models import ModelInfoManager
from pyclaw.openrouter import OpenRouterModelManager, _cost_per_token


class DummyResponse:
    """Minimal stand-in for requests.Response used in tests."""

    def __init__(self, json_data):
        self.status_code = 200
        self._json_data = json_data

    def json(self):
        return self._json_data


def test_openrouter_get_model_info_from_cache(monkeypatch, tmp_path):
    """
    OpenRouterModelManager should return correct metadata taken from the
    downloaded (and locally cached) models JSON payload.
    """
    payload = {
        "data": [
            {
                "id": "mistralai/mistral-medium-3",
                "context_length": 32768,
                "pricing": {"prompt": "100", "completion": "200"},
                "top_provider": {"context_length": 32768},
            }
        ]
    }

    # Fake out the network call and the HOME directory used for the cache file
    monkeypatch.setattr("requests.get", lambda *a, **k: DummyResponse(payload))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    manager = OpenRouterModelManager()
    info = manager.get_model_info("openrouter/mistralai/mistral-medium-3")

    assert info["max_input_tokens"] == 32768
    assert info["input_cost_per_token"] == 100.0
    assert info["output_cost_per_token"] == 200.0
    assert info["litellm_provider"] == "openrouter"


def test_model_info_manager_uses_openrouter_manager(monkeypatch):
    """
    ModelInfoManager should delegate to OpenRouterModelManager when litellm
    provides no data for an OpenRouter-prefixed model.
    """
    # Ensure litellm path returns no info so that fallback logic triggers
    monkeypatch.setattr("pyclaw.models.litellm.get_model_info", lambda *a, **k: {})

    stub_info = {
        "max_input_tokens": 512,
        "max_tokens": 512,
        "max_output_tokens": 512,
        "input_cost_per_token": 100.0,
        "output_cost_per_token": 200.0,
        "litellm_provider": "openrouter",
    }

    # Force OpenRouterModelManager to return our stub info
    monkeypatch.setattr(
        "pyclaw.models.OpenRouterModelManager.get_model_info",
        lambda self, model: stub_info,
    )

    mim = ModelInfoManager()
    info = mim.get_model_info("openrouter/fake/model")

    assert info == stub_info


def test_openrouter_cost_per_token_rejects_invalid_values():
    # Verifies invalid price strings fail closed to None instead of pretending to be numeric.
    # This catches permissive parsing bugs where bad pricing data could become misleading token costs.
    # The expected results are correct because only real numeric strings and explicit "0" are meaningful here.
    assert _cost_per_token("12.5") == 12.5
    assert _cost_per_token("0") == 0.0
    assert _cost_per_token("") is None
    assert _cost_per_token(None) is None
    assert _cost_per_token("not-a-number") is None


def test_openrouter_corrupt_cache_is_treated_as_missing(monkeypatch, tmp_path):
    # Verifies a corrupt OpenRouter cache does not become the main proof of correctness and is treated as missing.
    # This catches cases where malformed cache JSON would be silently used instead of forcing a clean fallback path.
    # The expected empty result is correct because corrupt cache content should not resolve any model metadata.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    manager = OpenRouterModelManager()
    manager.cache_dir.mkdir(parents=True, exist_ok=True)
    manager.cache_file.write_text("{bad json", encoding="utf-8")

    with monkeypatch.context() as ctx:
        ctx.setattr(manager, "_update_cache", lambda: None)
        info = manager.get_model_info("openrouter/example/model")

    assert info == {}
    assert manager._cache_loaded is True
    assert manager.content is None
