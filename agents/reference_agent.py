#!/usr/bin/env python3
"""Adamant reference agent -- copy this and make it yours.

    python agents/reference_agent.py --code RUMBLE-4F2 --url wss://arena.example.com

It is a WebSocket client: it dials the platform with a match code, joins with a
name and a profile, and answers every ``state_update`` with one of the eight
actions before the deadline. It keeps no state between ticks -- everything it
needs arrives in each ``state_update`` -- so a pause and resume is invisible to
it.

It fights as one of the personas in ``common/personas.py``: a named style that
varies the display name and the strategic half of the system prompt, and is
recorded in the profile. Run it twice with the same ``--code`` to fill both
corners and you get two different fighters; ``--persona aggressor`` picks one.

The rules summary, the prompt, and the decision loop live in ``agents/common/``;
this file is only the Anthropic client and the wiring. Requires ``anthropic``
and ``websockets``, and an ``ANTHROPIC_API_KEY`` in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `common` imports as a script

from anthropic import AsyncAnthropic  # noqa: E402

from common.decision import budget_seconds, make_decider  # noqa: E402
from common.personas import PERSONA_KEYS, Persona, pick_persona, prompt_version  # noqa: E402
from common.prompt import build_system_prompt  # noqa: E402
from common.wsloop import run_match  # noqa: E402

AGENT_VERSION = "reference@1.1.0"
DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 512

#: Below this deadline there is no room to think, so we ask for a fast answer;
#: at or above it (the "deep" bracket, spec 12) we let the model reason.
DEEP_BRACKET_MS = 15000


def build_profile(model: str, persona: Persona | None = None) -> dict:
    """Provenance, stored verbatim in the match log (spec 6.1). Required.

    The persona is part of this, not a runtime detail: a match fought by the
    aggressor must say so in its log, or the style is invisible to evaluation.
    """
    return {
        "model": model,
        "provider": "anthropic",
        "persona": persona.key if persona else None,
        "prompt_version": prompt_version(persona),
        # claude-opus-5 rejects `temperature` outright, so this agent never
        # sends one; reported as null rather than as a number we did not pin.
        "temperature": None,
        "agent_version": AGENT_VERSION,
    }


def make_model_call(client: AsyncAnthropic, model: str):
    """Return the ``(system, user, deadline_ms) -> (text, tokens_in, tokens_out)``
    callable that ``decide()`` time-boxes."""

    async def model_call(system: str, user: str, deadline_ms: int):
        request: dict = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if deadline_ms < DEEP_BRACKET_MS:
            # Thinking is on by default on this model; at a 5 s deadline it will
            # not finish. Disabling it is only legal at effort "high" or below.
            request["thinking"] = {"type": "disabled"}
            request["output_config"] = {"effort": "low"}
        budget = budget_seconds(deadline_ms)
        reply = await client.with_options(timeout=budget).messages.create(**request)
        text = "".join(b.text for b in reply.content if b.type == "text")
        return text, reply.usage.input_tokens, reply.usage.output_tokens

    return model_call


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adamant reference agent")
    parser.add_argument("--code", required=True, help="match code, e.g. RUMBLE-4F2")
    parser.add_argument("--url", required=True, help="platform base URL, e.g. wss://host")
    parser.add_argument("--name", default=None, help="display name, [A-Za-z0-9 _-]{1,24}")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--persona",
        default=None,
        choices=PERSONA_KEYS,
        help="fighting style; default: one not already taken for this match code",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict | None:
    persona = pick_persona(args.persona, code=args.code)
    name = args.name or persona.name
    print(f"persona {persona.key} fighting as {name}: {persona.tagline}")
    # A retry inside the SDK would silently eat the whole deadline, so: none.
    client = AsyncAnthropic(max_retries=0)
    try:
        return await run_match(
            args.url,
            args.code,
            name,
            build_profile(args.model, persona),
            make_decider(
                make_model_call(client, args.model), system=build_system_prompt(persona)
            ),
            log=print,
        )
    finally:
        await client.close()


def main(argv=None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run(args))
    if result is None:
        print("match did not finish (connection lost and could not be reclaimed)")
        return 1
    print(f"winner={result.get('winner')} reason={result.get('reason')} "
          f"final_tick={result.get('final_tick')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
