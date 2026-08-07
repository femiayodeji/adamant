"""The three-fighter OpenRouter app, with the HTTP call stubbed out.

No network call is made and no budget is spent: `StubPost` records the payload
that would have gone to OpenRouter and returns a canned response body.
"""

from __future__ import annotations

import json

import pytest

from agents.common.decision import budget_seconds
from agents.common.openrouter import API_URL, OpenRouterClient, OpenRouterError, have_api_key
from agents.common.personas import PERSONAS
from agents.common.prompt import OUTPUT_CONTRACT
from agents.common.wsloop import NAME_RE
from agents.openrouter_app import __main__ as cli
from agents.openrouter_app import roster
from agents.openrouter_app.fighter import build_client, play
from agents.tests.mock_platform import MATCH_END, MockPlatform, running, state_update


def completion(text: str, model: str = "anthropic/claude-haiku-4.5-20251001") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 640, "completion_tokens": 25},
    }


class StubPost:
    """Stands in for the one HTTP POST, recording what it was handed."""

    def __init__(self, body=None):
        self.body = body if body is not None else completion(
            json.dumps({"action": "hook", "reason": "trade into the jab"})
        )
        self.payloads: list[dict] = []
        self.headers: list[dict] = []
        self.timeouts: list[float] = []

    async def __call__(self, payload, headers, timeout):
        self.payloads.append(payload)
        self.headers.append(headers)
        self.timeouts.append(timeout)
        return self.body


def stub_client(fighter, body=None) -> OpenRouterClient:
    client = build_client(fighter, api_key="test-key")
    client.post = StubPost(body)
    return client


# ─── the roster ───


def test_three_fighters_that_differ_in_both_model_and_style():
    """An agent is prompt + model + logic as a bundle, so two entrants that share a
    model or a persona are not two entrants — they are one, sampled twice."""
    assert len(roster.ROSTER) == 3
    assert len({f.key for f in roster.ROSTER}) == 3
    assert len({f.model for f in roster.ROSTER}) == 3
    assert len({f.persona_key for f in roster.ROSTER}) == 3
    assert all(f.persona_key in PERSONAS for f in roster.ROSTER)


def test_every_fighter_can_actually_enter_the_ring():
    """A name the platform would reject surfaces as a rejected join, at the worst
    possible moment. Checked at import too — this asserts that check is real."""
    for fighter in roster.ROSTER:
        assert NAME_RE.match(fighter.name)


def test_the_output_contract_comes_last_so_style_cannot_displace_it():
    for fighter in roster.ROSTER:
        prompt = fighter.system_prompt()
        assert prompt.endswith(OUTPUT_CONTRACT)
        assert fighter.persona.guidance in prompt


def test_profile_is_complete_and_versions_the_prompt_with_the_persona():
    versions = set()
    for fighter in roster.ROSTER:
        profile = fighter.profile()
        assert set(profile) == {
            "model", "provider", "agent", "persona", "prompt_version",
            "temperature", "agent_version",
        }
        assert profile["provider"] == "openrouter"
        assert profile["agent"] == fighter.key
        assert fighter.persona_key in profile["prompt_version"]
        assert profile["agent_version"] == roster.APP_VERSION
        json.dumps(profile)  # stored verbatim in the log
        versions.add(profile["prompt_version"])
    assert len(versions) == 3, "two fighters must not share a prompt version"


def test_profile_reports_the_model_that_was_actually_used():
    profile = roster.get_fighter("cipher").profile(model="some/other-model")
    assert profile["model"] == "some/other-model"


def test_profile_can_report_an_omitted_temperature_as_null():
    """`None` means "the parameter was not sent", which some models require. It has
    to survive into the log as null rather than being read as "unspecified"."""
    assert roster.get_fighter("cipher").profile(temperature=None)["temperature"] is None
    assert roster.get_fighter("cipher").profile()["temperature"] == roster.TEMPERATURE


def test_an_unknown_fighter_key_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="bulldozer"):
        roster.get_fighter("nobody")


def test_describe_is_serialisable_and_matches_the_roster():
    described = roster.describe()
    assert [entry["key"] for entry in described] == list(roster.FIGHTER_KEYS)
    json.dumps(described)  # served by GET /api/agents


# ─── the OpenRouter client ───


async def test_the_request_is_the_one_openrouter_expects():
    client = stub_client(roster.get_fighter("bulldozer"))
    text, tokens_in, tokens_out = await client("SYS", "USER", 5000)

    assert (tokens_in, tokens_out) == (640, 25)
    assert json.loads(text)["action"] == "hook"
    payload = client.post.payloads[0]
    assert payload["model"] == "anthropic/claude-haiku-4.5"
    assert payload["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert payload["temperature"] == roster.TEMPERATURE
    assert payload["max_tokens"] == roster.MAX_TOKENS
    # A five-second bracket has no room for extended thinking (spec §12).
    assert payload["reasoning"] == roster.LOW_REASONING
    assert client.post.headers[0]["Authorization"] == "Bearer test-key"
    assert client.post.timeouts[0] == pytest.approx(budget_seconds(5000))


async def test_temperature_is_omitted_entirely_when_it_is_none():
    client = OpenRouterClient(model="m", temperature=None, api_key="k")
    client.post = StubPost()
    await client("SYS", "USER", 5000)
    assert "temperature" not in client.post.payloads[0]


async def test_an_upstream_failure_arriving_as_a_200_is_still_an_error():
    """OpenRouter reports a provider outage as HTTP 200 with an `error` object and
    no choices. Indexing into it blindly would report an outage as a KeyError."""
    client = stub_client(roster.get_fighter("cipher"), body={"error": {"message": "upstream 502"}})
    with pytest.raises(OpenRouterError, match="upstream 502"):
        await client("SYS", "USER", 5000)


async def test_the_url_is_not_restated_by_the_caller():
    assert API_URL.endswith("/chat/completions")


def test_have_api_key_answers_rather_than_raising(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert have_api_key(tmp_path / "nothing.env") is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert have_api_key(tmp_path / "nothing.env") is True


# ─── a fighter in a corner ───


async def test_a_fighter_plays_a_whole_match_against_the_mock_platform():
    fighter = roster.get_fighter("bulldozer")
    client = stub_client(fighter)
    platform = MockPlatform()

    async with running(platform) as url:
        end = await play(url, "RUMBLE-4F2", fighter, client=client, log=lambda _: None)

    assert end["reason"] == "ko"
    assert platform.join["name"] == "Bulldozer"
    assert platform.join["profile"]["agent"] == "bulldozer"
    assert [m["action"] for m in platform.moves] == ["hook", "hook", "hook"]
    assert all(m["telemetry"]["fallback"] is False for m in platform.moves)
    assert platform.moves[0]["telemetry"]["tokens_in"] == 640
    # The persona reached the model: its guidance is in the system prompt sent.
    assert fighter.persona.guidance in client.post.payloads[0]["messages"][0]["content"]


async def test_a_chatty_model_becomes_a_logged_hedge_not_a_crash():
    fighter = roster.get_fighter("ricochet")
    client = stub_client(fighter, body=completion("Sure! I'd throw a big right hand."))
    platform = MockPlatform()

    async with running(platform) as url:
        await play(url, "R", fighter, client=client, log=lambda _: None)

    for move in platform.moves:
        assert move["action"] == "block_high"
        assert move["telemetry"]["fallback"] is True


async def test_a_provider_outage_costs_a_tick_not_the_match():
    fighter = roster.get_fighter("cipher")
    client = stub_client(fighter, body={"error": "everything is on fire"})
    platform = MockPlatform(script=[
        {"type": "match_start", "you": "agent_a", "opponent_name": "Jab-bot", "config": {}},
        state_update(1, me={"last_action": "block_high", "repeat_streak": 3}),
        MATCH_END,
    ])

    async with running(platform) as url:
        end = await play(url, "R", fighter, client=client, log=lambda _: None)

    assert end["reason"] == "ko"  # the match finished; we did not drop the socket
    move = platform.moves[0]
    assert move["telemetry"]["fallback"] is True
    # A hedge pinned to block_high would be the 4th repeat here, and voided.
    assert move["action"] == "block_low"


async def test_a_requested_corner_rides_on_the_join():
    """Filling both corners from one process needs it; without it the two clients
    race for whichever slot is free (spec §6.1)."""
    fighter = roster.get_fighter("bulldozer")
    platform = MockPlatform(script=[MATCH_END])

    async with running(platform) as url:
        await play(url, "R", fighter, client=stub_client(fighter), corner="agent_b",
                   log=lambda _: None)

    assert platform.join["corner"] == "agent_b"


async def test_a_lone_fighter_asks_for_no_particular_corner():
    fighter = roster.get_fighter("bulldozer")
    platform = MockPlatform(script=[MATCH_END])

    async with running(platform) as url:
        await play(url, "R", fighter, client=stub_client(fighter), log=lambda _: None)

    assert "corner" not in platform.join


# ─── the command line ───


def test_cli_defaults_to_one_fighter_and_localhost():
    args = cli.parse_args(["--code", "RUMBLE-4F2"])
    assert args.code == "RUMBLE-4F2"
    assert args.url == cli.DEFAULT_URL
    assert args.agent is None  # resolved to the first of the roster at run time


def test_cli_takes_two_agents_to_fill_both_corners():
    args = cli.parse_args(["--code", "R", "--agent", "bulldozer", "--agent", "cipher"])
    assert args.agent == ["bulldozer", "cipher"]


def test_cli_lists_the_roster_without_a_code(capsys):
    assert cli.main(["--list"]) == 0
    printed = capsys.readouterr().out
    for fighter in roster.ROSTER:
        assert fighter.name in printed and fighter.model in printed


def test_cli_needs_a_code_to_fight():
    assert cli.main([]) == 2


async def test_cli_refuses_more_fighters_than_there_are_corners():
    args = cli.parse_args(
        ["--code", "R", "--agent", "bulldozer", "--agent", "cipher", "--agent", "ricochet"]
    )
    assert await cli.run(args) == 2


async def test_cli_refuses_a_single_agent_override_for_two_fighters():
    """`--model` with two fighters is ambiguous, and guessing which one it meant is
    worse than saying so."""
    args = cli.parse_args(
        ["--code", "R", "--agent", "bulldozer", "--agent", "cipher", "--model", "x/y"]
    )
    assert await cli.run(args) == 2
