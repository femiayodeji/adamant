"""The harness model client: LiteLLM -> OpenRouter, with the call stubbed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.common.decision import budget_seconds
from agents.harness.model_client import HARNESS_VERSION, LiteLLMClient, ModelSpec, load_api_key


def fake_completion(recorder, text='{"action": "jab", "reason": "probe"}',
                   resolved="anthropic/claude-opus-5-20260101"):
    async def completion(**request):
        recorder.append(request)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=612, completion_tokens=18),
            model=resolved,
        )

    return completion


@pytest.mark.asyncio
async def test_request_shape_targets_openrouter_and_pins_temperature():
    calls: list[dict] = []
    client = LiteLLMClient(
        ModelSpec(model="anthropic/claude-opus-5", temperature=0.0, max_tokens=256),
        api_key="sk-test",
        completion=fake_completion(calls),
    )
    text, tokens_in, tokens_out = await client("SYS", "USER", 5000)

    assert (tokens_in, tokens_out) == (612, 18)
    assert text.startswith("{")
    request = calls[0]
    assert request["model"] == "openrouter/anthropic/claude-opus-5"
    assert request["messages"][0] == {"role": "system", "content": "SYS"}
    assert request["messages"][1] == {"role": "user", "content": "USER"}
    assert request["temperature"] == 0.0
    assert request["max_tokens"] == 256
    assert request["num_retries"] == 0  # a retry would eat the whole deadline
    assert request["timeout"] == pytest.approx(budget_seconds(5000))


@pytest.mark.asyncio
async def test_temperature_none_means_do_not_send_the_parameter():
    calls: list[dict] = []
    client = LiteLLMClient(
        ModelSpec(model="anthropic/claude-opus-5", temperature=None),
        api_key="sk-test",
        completion=fake_completion(calls),
    )
    await client("SYS", "USER", 5000)
    assert "temperature" not in calls[0]


@pytest.mark.asyncio
async def test_profile_records_the_resolved_model_not_just_the_alias():
    client = LiteLLMClient(
        ModelSpec(model="anthropic/claude-opus-5", temperature=0.0),
        api_key="sk-test",
        completion=fake_completion([]),
    )
    assert client.profile()["resolved_model"] is None
    await client("SYS", "USER", 5000)

    profile = client.profile()
    assert profile["resolved_model"] == "anthropic/claude-opus-5-20260101"
    assert profile["model"] == "anthropic/claude-opus-5"
    assert profile["provider"] == "openrouter"
    assert profile["temperature"] == 0.0
    assert profile["agent_version"] == HARNESS_VERSION
    assert profile["prompt_version"]


def test_api_key_is_never_repr_ed():
    client = LiteLLMClient(ModelSpec(model="m"), api_key="sk-secret", completion=lambda: None)
    assert "sk-secret" not in repr(client)


def test_load_api_key_reads_the_env_first(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    assert load_api_key() == "sk-from-env"


def test_load_api_key_raises_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        load_api_key(env_path=tmp_path / "missing.env")
