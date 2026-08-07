"""The published example, with the Anthropic client stubbed out.

No network call is made: ``FakeAnthropic`` records the request that would have
been sent and returns a canned reply.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agents.reference_agent as ref
from agents.common.decision import budget_seconds, make_decider
from agents.common.personas import PERSONAS, prompt_version
from agents.common.prompt import PROMPT_VERSION
from agents.tests.mock_platform import MATCH_END, MockPlatform, running, state_update


class FakeMessages:
    def __init__(self, owner, text):
        self.owner = owner
        self.text = text

    async def create(self, **request):
        self.owner.requests.append(request)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=640, output_tokens=25),
            model="claude-opus-5-20260101",
        )


class FakeAnthropic:
    """Just enough of AsyncAnthropic for the reference agent."""

    def __init__(self, text='{"action": "hook", "reason": "trade into the jab"}'):
        self.requests: list[dict] = []
        self.timeouts: list[float] = []
        self.text = text
        self.closed = False

    def with_options(self, **options):
        self.timeouts.append(options.get("timeout"))
        return self

    @property
    def messages(self):
        return FakeMessages(self, self.text)

    async def close(self):
        self.closed = True


def test_profile_is_complete_and_declares_the_prompt_version():
    profile = ref.build_profile("claude-opus-5")
    assert set(profile) == {
        "model", "provider", "persona", "prompt_version", "temperature", "agent_version"
    }
    assert profile["model"] == "claude-opus-5"
    assert profile["provider"] == "anthropic"
    assert profile["persona"] is None
    assert profile["prompt_version"] == PROMPT_VERSION
    assert profile["agent_version"] == ref.AGENT_VERSION
    # claude-opus-5 rejects the parameter, so we report null rather than lie.
    assert profile["temperature"] is None
    json.dumps(profile)  # must survive being stored verbatim in the log


@pytest.mark.asyncio
async def test_standard_bracket_asks_for_a_fast_answer_and_sends_no_temperature():
    client = FakeAnthropic()
    call = ref.make_model_call(client, "claude-opus-5")
    text, tokens_in, tokens_out = await call("SYS", "USER", 5000)

    assert (text, tokens_in, tokens_out) == (client.text, 640, 25)
    request = client.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["system"] == "SYS"
    assert request["messages"] == [{"role": "user", "content": "USER"}]
    assert request["thinking"] == {"type": "disabled"}
    assert request["output_config"] == {"effort": "low"}
    # Sampling params are rejected outright on this model.
    assert "temperature" not in request and "top_p" not in request
    assert client.timeouts[0] == pytest.approx(budget_seconds(5000))


@pytest.mark.asyncio
async def test_deep_bracket_lets_the_model_think():
    client = FakeAnthropic()
    await ref.make_model_call(client, "claude-opus-5")("SYS", "USER", 30000)
    request = client.requests[0]
    assert "thinking" not in request  # model default: adaptive thinking is on
    assert "output_config" not in request
    assert client.timeouts[0] == pytest.approx(budget_seconds(30000))


@pytest.mark.asyncio
async def test_end_to_end_against_the_mock_platform():
    client = FakeAnthropic()
    decider = make_decider(ref.make_model_call(client, "claude-opus-5"))
    platform = MockPlatform()

    async with running(platform) as url:
        end = await ref.run_match(
            url, "RUMBLE-4F2", "RefBot", ref.build_profile("claude-opus-5"), decider
        )

    assert end["reason"] == "ko"
    moves = platform.moves
    assert [m["tick"] for m in moves] == [1, 2, 2]
    assert {m["action"] for m in moves} == {"hook"}
    for move in moves:
        assert move["telemetry"]["fallback"] is False
        assert move["telemetry"]["tokens_in"] == 640
        assert move["telemetry"]["reason"] == "trade into the jab"


@pytest.mark.asyncio
async def test_a_chatty_model_becomes_a_logged_hedge_not_a_crash():
    client = FakeAnthropic(text="Sure! I think you should throw a hook.")
    decider = make_decider(ref.make_model_call(client, "claude-opus-5"))
    platform = MockPlatform()

    async with running(platform) as url:
        await ref.run_match(url, "R", "RefBot", ref.build_profile("m"), decider)

    for move in platform.moves:
        assert move["action"] == "block_high"
        assert move["telemetry"]["fallback"] is True


@pytest.mark.asyncio
async def test_the_published_stack_hedges_around_the_repetition_cap():
    """Whole stack — Anthropic client, decision loop, socket — against a platform
    that reports a hedge streak already at the cap. A fallback pinned to
    block_high would be voided here and stand the agent up defenceless."""
    client = FakeAnthropic(text="not JSON, so every tick is a hedge")
    decider = make_decider(ref.make_model_call(client, "claude-opus-5"))
    platform = MockPlatform(script=[
        {"type": "match_start", "you": "agent_a", "opponent_name": "Jab-bot", "config": {}},
        state_update(1, me={"last_action": "block_high", "repeat_streak": 2}),
        state_update(2, me={"last_action": "block_high", "repeat_streak": 3}),
        state_update(3, me={"last_action": "block_low", "repeat_streak": 1}),
        state_update(4, me={"last_action": "block_high", "repeat_streak": 9}),
        MATCH_END,
    ])

    async with running(platform) as url:
        await ref.run_match(url, "R", "RefBot", ref.build_profile("m"), decider)

    assert [m["action"] for m in platform.moves] == [
        "block_high", "block_low", "block_high", "block_low"
    ]
    assert all(m["telemetry"]["fallback"] is True for m in platform.moves)


def test_cli_arguments():
    args = ref.parse_args(["--code", "RUMBLE-4F2", "--url", "wss://arena.example.com"])
    assert (args.code, args.url) == ("RUMBLE-4F2", "wss://arena.example.com")
    assert args.model == ref.DEFAULT_MODEL
    # Both default to "unset" so the persona supplies the identity.
    assert args.name is None and args.persona is None


def test_cli_takes_persona_and_name_overrides():
    args = ref.parse_args(
        ["--code", "R", "--url", "ws://x", "--persona", "aggressor", "--name", "MyGuy"]
    )
    assert (args.persona, args.name) == ("aggressor", "MyGuy")


def test_profile_records_the_persona_and_versions_the_prompt_with_it():
    """A match fought by the aggressor must say so in its log: profile fields are
    the one thing that cannot be backfilled."""
    versions = set()
    for key, persona in PERSONAS.items():
        profile = ref.build_profile("claude-opus-5", persona)
        assert profile["persona"] == key
        assert profile["prompt_version"] == prompt_version(persona)
        assert key in profile["prompt_version"]
        assert profile["prompt_version"] != PROMPT_VERSION, "the persona must version the prompt"
        json.dumps(profile)  # stored verbatim in the log
        versions.add(profile["prompt_version"])
    assert len(versions) == len(PERSONAS), "two personas must not share a prompt version"
