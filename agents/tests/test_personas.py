"""Personas: distinct fighters, attributable in the log, never a protocol change.

The bug these exist to fix: two bare invocations of ``reference_agent.py`` filling
the two corners of a match used to produce the same agent with the same name — an
illegible demo and a mirror-match simulation.
"""

from __future__ import annotations

import json
import time

import pytest

import agents.reference_agent as ref
from agents.common.personas import (
    CLAIM_TTL_S,
    PERSONA_KEYS,
    PERSONAS,
    Persona,
    get_persona,
    pick_persona,
    prompt_version,
)
from agents.common.prompt import PROMPT_VERSION, build_system_prompt
from agents.common.rules import ACTIONS, RULES_SUMMARY
from agents.common.wsloop import NAME_RE
from agents.tests.mock_platform import MockPlatform, running
from agents.tests.test_reference_agent import FakeAnthropic


def test_there_is_a_small_set_of_personas():
    assert 2 <= len(PERSONAS) <= 8, "a ladder, not a catalogue"
    assert all(isinstance(p, Persona) for p in PERSONAS.values())


@pytest.mark.parametrize("field", ["key", "name", "tagline", "guidance"])
def test_personas_are_distinct_in_every_visible_field(field):
    values = [getattr(p, field) for p in PERSONAS.values()]
    assert len(set(values)) == len(values), f"two personas share a {field}"


def test_display_names_are_legal_and_cannot_collide():
    """Two agents in the same match must be tellable apart, and the name is the
    one free-text channel between adversaries, so it stays inside NAME_RE."""
    names = [p.name for p in PERSONAS.values()]
    assert len(set(names)) == len(names)
    for name in names:
        assert NAME_RE.match(name), name


def test_every_persona_says_something_substantial_and_specific():
    for persona in PERSONAS.values():
        assert len(persona.guidance) > 300, persona.key
        assert persona.tagline and not persona.tagline.endswith("."), persona.key
        # Style must be expressed in the vocabulary of the actual action space.
        assert any(action in persona.guidance for action in ACTIONS), persona.key


def test_personas_vary_which_actions_they_lean_on():
    """A persona that does not change behaviour is decoration. Each one must
    emphasise a different part of the action space."""
    leaned = {
        key: frozenset(a for a in ACTIONS if persona.guidance.count(a) >= 2)
        for key, persona in PERSONAS.items()
    }
    assert len(set(leaned.values())) == len(leaned), f"personas overlap: {leaned}"


# --- a persona is strategy, never protocol ---------------------------------


@pytest.mark.parametrize("key", PERSONA_KEYS)
def test_the_system_prompt_keeps_the_rules_and_the_output_contract(key):
    persona = PERSONAS[key]
    system = build_system_prompt(persona)

    assert RULES_SUMMARY in system, "a persona must not displace the rules"
    assert persona.guidance in system
    assert "SINGLE JSON object" in system
    assert "REPETITION CAP" in system
    for action in ACTIONS:
        assert action in system, action
    # The contract is emitted last, so no persona text can follow and soften it.
    assert system.index(persona.guidance) < system.index("SINGLE JSON object")
    assert system.rstrip().endswith('"<why, at most 200 characters>"}')


def test_the_neutral_prompt_is_still_available_and_carries_no_persona():
    system = build_system_prompt()
    assert "SINGLE JSON object" in system
    for persona in PERSONAS.values():
        assert persona.guidance not in system


def test_prompt_version_is_per_persona_and_the_neutral_one_is_the_base():
    assert prompt_version(None) == PROMPT_VERSION
    versions = {prompt_version(p) for p in PERSONAS.values()}
    assert len(versions) == len(PERSONAS)
    assert PROMPT_VERSION not in versions


# --- selection --------------------------------------------------------------


@pytest.mark.parametrize("key", PERSONA_KEYS)
def test_an_explicit_persona_is_honoured_exactly(key, tmp_path):
    chosen = pick_persona(key, code="RUMBLE-9S4", claim_file=tmp_path / "c.json")
    assert chosen.key == key


def test_an_unknown_persona_is_a_clear_error_not_a_silent_default():
    with pytest.raises(ValueError, match="unknown persona"):
        pick_persona("brawler")
    with pytest.raises(ValueError, match="unknown persona"):
        get_persona("brawler")
    assert get_persona(None) is None


def test_two_bare_invocations_for_one_match_code_do_not_collide(tmp_path):
    """The reported bug, directly: `--code RUMBLE-9S4` twice must give two
    different fighters with two different names."""
    claims = tmp_path / "claims.json"
    first = pick_persona(code="RUMBLE-9S4", claim_file=claims)
    second = pick_persona(code="RUMBLE-9S4", claim_file=claims)
    assert first.key != second.key
    assert first.name != second.name


def test_a_whole_card_of_invocations_exhausts_the_roster_before_repeating(tmp_path):
    claims = tmp_path / "claims.json"
    drawn = [pick_persona(code="RUMBLE-9S4", claim_file=claims).key for _ in PERSONAS]
    assert sorted(drawn) == sorted(PERSONA_KEYS)
    # Past the roster it must still return someone rather than fail.
    assert pick_persona(code="RUMBLE-9S4", claim_file=claims).key in PERSONA_KEYS


def test_a_different_match_code_draws_from_the_full_roster(tmp_path):
    claims = tmp_path / "claims.json"
    for _ in PERSONAS:
        pick_persona(code="RUMBLE-AAA", claim_file=claims)
    assert pick_persona(code="RUMBLE-BBB", claim_file=claims).key in PERSONA_KEYS


def test_the_draw_is_random_rather_than_a_fixed_first_entry(tmp_path):
    """Without a code there is nothing to deconflict against, so it must at least
    not hand out the same fighter every time."""
    drawn = {pick_persona(claim_file=tmp_path / "c.json").key for _ in range(60)}
    assert len(drawn) > 1


def test_stale_claims_expire_so_yesterdays_codes_do_not_constrain_today(tmp_path):
    claims = tmp_path / "claims.json"
    stale = time.time() - CLAIM_TTL_S - 60
    claims.write_text(
        json.dumps({"RUMBLE-9S4": {"at": stale, "keys": list(PERSONA_KEYS)}}),
        encoding="utf-8",
    )
    assert pick_persona(code="RUMBLE-9S4", claim_file=claims).key in PERSONA_KEYS


@pytest.mark.parametrize("content", ["not json at all", "[]", '{"RUMBLE-9S4": 7}'])
def test_a_corrupt_claim_file_never_stops_a_match(tmp_path, content):
    claims = tmp_path / "claims.json"
    claims.write_text(content, encoding="utf-8")
    assert pick_persona(code="RUMBLE-9S4", claim_file=claims).key in PERSONA_KEYS


def test_an_unwritable_claim_file_never_stops_a_match(tmp_path):
    unwritable = tmp_path / "no" / "such" / "dir" / "claims.json"
    assert pick_persona(code="RUMBLE-9S4", claim_file=unwritable).key in PERSONA_KEYS


# --- the published path -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", PERSONA_KEYS)
async def test_a_persona_reaches_the_join_profile_and_the_model(key):
    """End to end over a socket: the name the spectator sees and the persona the
    log records both come from the persona, and the model gets its guidance."""
    persona = PERSONAS[key]
    client = FakeAnthropic()
    platform = MockPlatform()

    async with running(platform) as url:
        await ref.run_match(
            url,
            "RUMBLE-9S4",
            persona.name,
            ref.build_profile("claude-opus-5", persona),
            ref.make_decider(
                ref.make_model_call(client, "claude-opus-5"),
                system=ref.build_system_prompt(persona),
            ),
        )

    assert platform.join["name"] == persona.name
    assert platform.join["profile"]["persona"] == key
    assert platform.join["profile"]["prompt_version"] == prompt_version(persona)
    assert client.requests, "the model was never called"
    for request in client.requests:
        assert persona.guidance in request["system"]
        assert "SINGLE JSON object" in request["system"]


@pytest.mark.asyncio
async def test_two_personas_fill_one_match_as_two_different_fighters(tmp_path):
    """The simulation case: same code, two processes, two legible fighters."""
    claims = tmp_path / "claims.json"
    joins = []
    for _ in range(2):
        persona = pick_persona(code="RUMBLE-9S4", claim_file=claims)
        platform = MockPlatform()
        async with running(platform) as url:
            await ref.run_match(
                url,
                "RUMBLE-9S4",
                persona.name,
                ref.build_profile("claude-opus-5", persona),
                ref.make_decider(ref.make_model_call(FakeAnthropic(), "claude-opus-5")),
            )
        joins.append(platform.join)

    assert joins[0]["name"] != joins[1]["name"]
    assert joins[0]["profile"]["persona"] != joins[1]["profile"]["persona"]
    assert joins[0]["profile"]["prompt_version"] != joins[1]["profile"]["prompt_version"]
