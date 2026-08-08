"""LLM corners: `GET /api/agents` and `POST /api/matches/{ref}/fill_agent` (spec §8.6).

**No API budget is spent here.** `server.llm_agents.build_model_client` is patched
to a stub that answers instantly with a fixed JSON object, so what is exercised is
everything except the network hop: the roster, the endpoint's validation order, the
join over the public protocol, the profile that reaches the log, the decision loop's
hedge, and the rematch path.

The one thing these tests therefore cannot prove is that a real model returns a
parseable action. That is what the Phase 3.5 gate is for.
"""

from __future__ import annotations

import json

import pytest

from agents.openrouter_app import roster
from server import llm_agents

from .conftest import Agent, Spectator

# The stub answers instantly, so a long deadline costs the suite nothing — a tick
# resolves as soon as both corners have answered, not when the clock runs out. It has
# to clear `MIN_DEADLINE_MS` because `fill_agent` now refuses a bracket the roster
# cannot answer inside, and it is read from the roster rather than copied so that
# moving the floor does not silently start testing something else.
FAST = {"deadline_ms": roster.MIN_DEADLINE_MS, "max_ticks": 2}


class StubModel:
    """Stands in for `OpenRouterClient`: same call signature, no socket.

    `reply` is returned verbatim, so a test can hand it something unparseable and
    watch the decision loop hedge rather than crash the corner.
    """

    def __init__(self, model: str = "stub/model", reply: str | None = None):
        self.model = model
        self.temperature = 0.3
        self.reply = reply if reply is not None else json.dumps({"action": "hook", "reason": "x"})
        self.calls: list[tuple[str, str, int]] = []
        self.closed = False

    async def __call__(self, system: str, user: str, deadline_ms: int):
        self.calls.append((system, user, deadline_ms))
        return self.reply, 11, 7

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def stub_model(monkeypatch):
    """Every fighter the platform spawns gets this instead of OpenRouter."""
    made: list[StubModel] = []

    def build(fighter, model=None):
        stub = StubModel(model=model or fighter.model)
        made.append(stub)
        return stub

    monkeypatch.setattr(llm_agents, "build_model_client", build)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    return made


# ─── §8.6 the roster ───


async def test_agents_endpoint_lists_the_roster_from_the_app_not_a_copy(arena):
    """Asserted against `agents.openrouter_app`, for the same reason `/api/rules` is
    asserted against `engine.rules`: a hand-copied roster is one edit from lying
    about which model is in the corner."""
    from agents.openrouter_app import roster as app_roster

    payload = (await arena.get("/api/agents")).json()

    assert payload["provider"] == "openrouter"
    assert payload["agents"] == app_roster.describe()
    assert [entry["key"] for entry in payload["agents"]] == list(app_roster.FIGHTER_KEYS)
    assert len(payload["agents"]) == 3


async def test_roster_is_listed_even_with_no_key_but_marked_unavailable(arena, monkeypatch):
    """The lobby shows the fighters and explains why the button is dead. An empty
    list would read as "this platform has no LLM agents", which is a different and
    untrue statement."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(llm_agents, "_have_key", lambda: False)

    payload = (await arena.get("/api/agents")).json()

    assert payload["available"] is False
    assert "OPENROUTER_API_KEY" in payload["reason"]
    assert len(payload["agents"]) == 3


async def test_fill_agent_is_503_when_no_key_is_configured(arena, monkeypatch):
    """Not a quiet downgrade to a bot: a corner logged as a model that was in fact
    a heuristic would poison the dataset."""
    monkeypatch.setattr(llm_agents, "_have_key", lambda: False)
    created = await arena.create_match(**FAST)

    response = await arena.fill_agent(
        created["match_code"], created["host_token"], corner="agent_a", agent="bulldozer"
    )

    assert response.status_code == 503
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


async def test_fill_agent_is_503_when_the_key_cannot_reach_the_model(arena, monkeypatch):
    """A key that is present but unusable — no credit, revoked, wrong org — has to be
    refused exactly like a missing one.

    This is the failure that cost a real match. `availability()` can only see whether
    a key is *configured*, so a declined account filled the corner, the host started
    the fight, and all 120 model calls came back HTTP 402. Every move was the hedge:
    block_high against block_high for 60 ticks, no damage, no events, a 0-0 draw, and
    a log naming two models that never answered once. Nothing about that looks like a
    billing problem, which is why the corner must not be taken at all.
    """

    class Declined(StubModel):
        async def preflight(self) -> None:
            raise RuntimeError("HTTP 402: Insufficient credits")

    monkeypatch.setattr(llm_agents, "build_model_client", lambda fighter, model=None: Declined())
    monkeypatch.setattr(llm_agents, "_have_key", lambda: True)
    created = await arena.create_match(**FAST)

    response = await arena.fill_agent(
        created["match_code"], created["host_token"], corner="agent_a", agent="bulldozer"
    )

    assert response.status_code == 503
    assert "402" in response.json()["detail"]
    status = (await arena.get(f"/api/matches/{created['match_code']}")).json()
    assert status["corners"]["agent_a"] is None, "a corner it cannot fight in stays empty"


async def test_fill_agent_refuses_a_bracket_the_roster_cannot_answer_inside(arena, stub_model):
    """The roster runs on free models, which need seconds per decision. Under the
    floor every tick times out into a hedge — legal, unpenalised, invisible — and the
    match finishes scoreless. Saying so up front beats discovering it at tick 60."""
    created = await arena.create_match(deadline_ms=500, max_ticks=2)

    response = await arena.fill_agent(
        created["match_code"], created["host_token"], corner="agent_a", agent="bulldozer"
    )

    assert response.status_code == 409
    assert str(roster.MIN_DEADLINE_MS) in response.json()["detail"]


async def test_the_roster_endpoint_publishes_the_deadline_floor(arena, stub_model):
    payload = (await arena.get("/api/agents")).json()
    assert payload["min_deadline_ms"] == roster.MIN_DEADLINE_MS


# ─── §8.6 validation ───


async def test_fill_agent_rejects_an_unknown_agent(arena, stub_model):
    created = await arena.create_match(**FAST)
    response = await arena.fill_agent(
        created["match_code"], created["host_token"], corner="agent_a", agent="nobody"
    )
    assert response.status_code == 422
    assert "bulldozer" in response.json()["detail"]


async def test_fill_agent_rejects_a_bad_corner(arena, stub_model):
    created = await arena.create_match(**FAST)
    response = await arena.fill_agent(
        created["match_code"], created["host_token"], corner="ringside", agent="bulldozer"
    )
    assert response.status_code == 422


async def test_fill_agent_requires_the_host_token(arena, stub_model):
    created = await arena.create_match(**FAST)
    response = await arena.fill_agent(
        created["match_code"], "not-the-token", corner="agent_a", agent="bulldozer"
    )
    assert response.status_code == 401


async def test_fill_agent_refuses_an_occupied_corner(arena, stub_model):
    created = await arena.create_match(**FAST)
    code, token = created["match_code"], created["host_token"]
    first = await arena.fill_agent(code, token, corner="agent_a", agent="cipher")
    assert first.status_code == 200

    response = await arena.fill_agent(code, token, corner="agent_a", agent="ricochet")

    assert response.status_code == 409


# ─── the corner it actually takes ───


async def test_filled_agent_joins_the_corner_and_publishes_its_profile(arena, stub_model):
    """It dials `/ws/join/{code}` like anyone else, so the lobby sees it exactly as
    it sees a guest agent — and the profile it declares is the one the log keeps."""
    created = await arena.create_match(**FAST)
    code, token = created["match_code"], created["host_token"]

    response = await arena.fill_agent(code, token, corner="agent_b", agent="ricochet")

    assert response.status_code == 200
    assert response.json()["corner"] == "agent_b"
    assert response.json()["model"] == roster.get_fighter("ricochet").model

    corner = (await arena.get(f"/api/matches/{code}")).json()["corners"]["agent_b"]
    assert corner["name"] == "Ricochet"
    assert corner["profile"]["provider"] == "openrouter"
    assert corner["profile"]["agent"] == "ricochet"
    assert corner["profile"]["persona"] == "counter_puncher"
    assert corner["profile"]["prompt_version"].endswith("counter_puncher-v1")


async def test_model_override_reaches_the_client_and_the_profile(arena, stub_model):
    created = await arena.create_match(**FAST)
    code, token = created["match_code"], created["host_token"]

    response = await arena.fill_agent(
        code, token, corner="agent_a", agent="bulldozer", model="some/other-model"
    )

    assert response.json()["model"] == "some/other-model"
    assert stub_model[0].model == "some/other-model"
    corner = (await arena.get(f"/api/matches/{code}")).json()["corners"]["agent_a"]
    assert corner["profile"]["model"] == "some/other-model"


# ─── a whole match ───


async def _fight(arena, stub_model, **fills):
    created = await arena.create_match(**FAST)
    code, token = created["match_code"], created["host_token"]
    for corner, spec in fills.items():
        filler = arena.fill_agent if spec[0] == "llm" else arena.fill_bot
        key = "agent" if spec[0] == "llm" else "bot"
        assert (await filler(code, token, corner=corner, **{key: spec[1]})).status_code == 200

    watcher = await Spectator.connect(arena, code)
    try:
        assert (await arena.start(code, token)).status_code == 200
        await watcher.drain_until("match_end", timeout=30.0)
    finally:
        await watcher.close()
    return created


async def test_an_llm_corner_fights_a_bot_to_the_final_bell(arena, stub_model):
    """The stub always answers `hook`, so the repetition cap does the interesting
    part: the 4th consecutive one is voided, which is exactly what the cap is for
    and what a real model has to avoid on its own."""
    created = await _fight(arena, stub_model, agent_a=("llm", "bulldozer"), agent_b=("bot", "jab"))

    log = (await arena.get(f"/api/matches/{created['match_code']}/log")).json()

    assert log["match_end"] is not None
    assert log["match_start"]["agents"]["agent_a"]["profile"]["agent"] == "bulldozer"
    assert [tick["moves"]["agent_a"] for tick in log["ticks"]] == ["hook", "hook"]
    telemetry = log["ticks"][0]["telemetry"]["agent_a"]
    assert telemetry["fallback"] is False
    assert telemetry["tokens_in"] == 11 and telemetry["tokens_out"] == 7
    assert telemetry["reason"] == "x"
    assert all(stub.closed for stub in stub_model)


async def test_two_roster_fighters_can_fill_both_corners(arena, stub_model):
    created = await _fight(
        arena, stub_model, agent_a=("llm", "bulldozer"), agent_b=("llm", "cipher")
    )

    log = (await arena.get(f"/api/matches/{created['match_code']}/log")).json()

    agents = log["match_start"]["agents"]
    assert agents["agent_a"]["name"] == "Bulldozer"
    assert agents["agent_b"]["name"] == "Cipher"
    # Two personas are two prompts, and the log has to be able to tell them apart.
    assert (
        agents["agent_a"]["profile"]["prompt_version"]
        != agents["agent_b"]["profile"]["prompt_version"]
    )


async def test_an_unparseable_reply_hedges_instead_of_losing_the_corner(arena, monkeypatch):
    """A model that ignores the output contract is a fallback, tick after tick —
    never a dropped socket, which the platform would read as infrastructure failure
    and pause the match for."""
    monkeypatch.setattr(
        llm_agents,
        "build_model_client",
        lambda fighter, model=None: StubModel(reply="I'd like to throw a big right hand."),
    )
    monkeypatch.setattr(llm_agents, "_have_key", lambda: True)

    created = await _fight(arena, None, agent_a=("llm", "cipher"), agent_b=("bot", "jab"))

    log = (await arena.get(f"/api/matches/{created['match_code']}/log")).json()
    first = log["ticks"][0]["telemetry"]["agent_a"]
    assert first["fallback"] is True
    assert log["ticks"][0]["moves"]["agent_a"] == "block_high"
    assert log["match_end"]["reason"] != "abandoned"


async def test_a_permanently_failing_model_drops_its_corner_instead_of_hedging_on(
    arena, monkeypatch
):
    """A transport blip is worth a hedge; a declined account is not.

    The corner answers the tick it is already holding — the hedge is decided and
    withholding it would only add a no-move penalty — and then stops, so the match
    pauses on a missing corner (spec §6.4). That is the honest record: infrastructure
    failure, distinguishable from combat, rather than a full match of hedges logged
    under a model's name.
    """

    class Broke(StubModel):
        permanent_error = "HTTP 402: Insufficient credits"

        async def __call__(self, system, user, deadline_ms):
            raise RuntimeError(self.permanent_error)

    monkeypatch.setattr(llm_agents, "build_model_client", lambda fighter, model=None: Broke())
    monkeypatch.setattr(llm_agents, "_have_key", lambda: True)

    created = await arena.create_match(deadline_ms=roster.MIN_DEADLINE_MS, max_ticks=20)
    code, token = created["match_code"], created["host_token"]
    match = arena.match(code)
    match.config.resume_window_ms = 1000  # do not sit out the full two minutes
    assert (await arena.fill_agent(code, token, corner="agent_a", agent="cipher")).status_code == 200
    await arena.fill_bot(code, token, corner="agent_b", bot="jab")
    spectator = await Spectator.connect(arena, code)
    await arena.start(code, token)

    end = await spectator.expect("match_end", timeout=30)

    assert end["reason"] == "abandoned", "the corner left rather than hedging 20 ticks"
    assert end["winner"] == "agent_b"
    log = (await arena.get(f"/api/matches/{code}/log")).json()
    assert len(log["ticks"]) < 20, "it stopped early instead of running the clock out"
    assert log["match_end"]["final_state"]["agent_b"]["points"] == 0, (
        "and no combat was recorded under a model that never answered"
    )
    await spectator.close()


async def test_a_human_can_take_the_other_corner_against_a_roster_fighter(arena, stub_model):
    """No privileged path: the LLM corner is a client of the same endpoint, so an
    ordinary agent joins beside it with nothing special to do."""
    created = await arena.create_match(**FAST)
    code, token = created["match_code"], created["host_token"]
    assert (
        await arena.fill_agent(code, token, corner="agent_a", agent="bulldozer")
    ).status_code == 200

    human = await Agent.connect(arena, code)
    reply = await human.join("Guest")

    assert reply["type"] == "joined"
    assert reply["corner"] == "agent_b"
    await human.close()


# ─── §8.4 rematch ───


async def test_rematch_puts_the_same_fighter_back_in_its_corner(arena, stub_model):
    """And says which filler it used: a fill dict that cannot name its own kind
    cannot be replayed, because the two spawners take different arguments."""
    created = await _fight(arena, stub_model, agent_a=("llm", "cipher"), agent_b=("bot", "jab"))
    code, token = created["match_code"], created["host_token"]

    async with arena.client() as http:
        response = await http.post(
            f"/api/matches/{code}/rematch", headers={"X-Host-Token": token}
        )
    assert response.status_code == 200
    payload = response.json()

    refilled = {entry["corner"]: entry for entry in payload["bots"]}
    assert refilled["agent_a"]["kind"] == "llm"
    assert refilled["agent_a"]["agent"] == "cipher"
    assert refilled["agent_b"]["kind"] == "bot"

    status = (await arena.get(f"/api/matches/{payload['match_code']}")).json()
    assert status["corners"]["agent_a"]["name"] == "Cipher"
    assert status["corners"]["agent_b"]["name"] == "Jab-bot"
