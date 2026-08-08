"""Put a roster fighter in a corner and play the match out.

Everything that is not OpenRouter-specific is borrowed rather than rewritten:
``common.wsloop`` speaks the protocol, ``common.decision`` time-boxes the model
call and hedges when it overruns, and ``common.prompt`` builds the prompt. What
this module adds is one line of wiring — which model, under which persona.

That is the whole point of the layout: an agent is the bundle, and swapping the
client underneath it must not change how it fights.
"""

from __future__ import annotations

from ..common.decision import make_decider
from ..common.openrouter import OpenRouterClient
from ..common.wsloop import run_match
from .roster import LOW_REASONING, Fighter


def build_client(
    fighter: Fighter,
    *,
    model: str | None = None,
    temperature: float | None = None,
    api_key: str = "",
) -> OpenRouterClient:
    """The model client for one fighter, for one match."""
    return OpenRouterClient(
        model=model or fighter.model,
        temperature=fighter.temperature if temperature is None else temperature,
        max_tokens=fighter.max_tokens,
        reasoning=LOW_REASONING,
        api_key=api_key,
    )


async def play(
    url: str,
    code: str,
    fighter: Fighter,
    *,
    name: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    corner: str | None = None,
    api_key: str = "",
    client: OpenRouterClient | None = None,
    log=print,
) -> dict | None:
    """Join ``code`` as ``fighter`` and fight to ``match_end``.

    Returns the ``match_end`` payload, or ``None`` if the socket could not be
    reclaimed after a drop. ``client`` is injectable so tests can fight a whole
    match without spending a cent.
    """
    owned = client is None
    client = client or build_client(
        fighter, model=model, temperature=temperature, api_key=api_key
    )
    try:
        # Fail here rather than in the ring. Without this a key that cannot pay takes
        # the corner, the host starts the fight, and every tick hedges — a full match
        # of blocks, no damage, no events and a 0-0 draw, under a model that never
        # answered once. `preflight` costs one token when it works and nothing at all
        # when it does not. Only for a client we built: an injected one is a test stub.
        if owned:
            await client.preflight()
        return await run_match(
            url,
            code,
            name or fighter.name,
            fighter.profile(model=client.model, temperature=client.temperature),
            make_decider(client, system=fighter.system_prompt()),
            corner=corner,
            log=log,
        )
    finally:
        if owned:
            await client.aclose()
