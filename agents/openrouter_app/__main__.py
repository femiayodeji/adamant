"""The fight app's command line.

    python -m agents.openrouter_app --list
    python -m agents.openrouter_app --code RUMBLE-4F2 --agent bulldozer
    python -m agents.openrouter_app --code RUMBLE-4F2 --agent bulldozer --agent cipher

One ``--agent`` fills one corner and waits for whoever takes the other. Two fill
both corners of the same match from this one process, each in its own task with
its own socket and its own model — the platform cannot tell them apart from two
people running the app on two laptops, because there is no privileged path and
they are using the ordinary public protocol.

Needs ``OPENROUTER_API_KEY`` in the environment or in ``.env``. **Running this
spends money**: one model call per fighter per tick, up to ``max_ticks`` ticks.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..common.openrouter import load_api_key
from .fighter import play
from .roster import FIGHTER_KEYS, ROSTER, get_fighter

DEFAULT_URL = "ws://localhost:8000"
CORNERS = ("agent_a", "agent_b")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m agents.openrouter_app",
        description="Send one or two OpenRouter-powered fighters into an Adamant match",
    )
    parser.add_argument("--code", help="match code, e.g. RUMBLE-4F2")
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"platform base URL (default {DEFAULT_URL})"
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=None,
        choices=FIGHTER_KEYS,
        metavar="KEY",
        help="fighter to send in; repeat once to fill both corners. "
        f"One of: {', '.join(FIGHTER_KEYS)}",
    )
    parser.add_argument(
        "--model", default=None, help="override the OpenRouter model id (one agent only)"
    )
    parser.add_argument("--name", default=None, help="override the display name (one agent only)")
    parser.add_argument("--temperature", type=float, default=None, help="override the sampler")
    parser.add_argument("--list", action="store_true", help="print the roster and exit")
    return parser.parse_args(argv)


def print_roster() -> None:
    for fighter in ROSTER:
        print(f"{fighter.key:<12} {fighter.name:<12} {fighter.model}")
        print(f"{'':<12} {fighter.blurb}")


def prefixed(key: str):
    """Tag each fighter's log lines, or two concurrent fights interleave into one
    unreadable stream."""
    return lambda line: print(f"[{key}] {line}")


async def run(args: argparse.Namespace) -> int:
    keys = args.agent or [ROSTER[0].key]
    if len(keys) > 2:
        print("a match has two corners; pass --agent at most twice", file=sys.stderr)
        return 2
    if len(keys) == 2 and (args.model or args.name):
        print("--model and --name apply to a single agent; drop one --agent", file=sys.stderr)
        return 2
    if len(set(keys)) != len(keys):
        # Not a protocol error -- the platform would rename the second one -- but
        # two identical fighters is a mirror match, which measures nothing.
        print(f"warning: {keys[0]} is fighting itself", file=sys.stderr)

    # Read once, here: a missing key is a startup error, not a per-tick fallback
    # that would quietly play a whole match on hedges.
    api_key = load_api_key()
    fighters = [get_fighter(key) for key in keys]

    results = await asyncio.gather(
        *(
            play(
                args.url,
                args.code,
                fighter,
                name=args.name,
                model=args.model,
                temperature=args.temperature,
                api_key=api_key,
                # Only pinned when this process owns both corners; a lone fighter
                # takes whatever is free, which is what a guest agent should do.
                corner=CORNERS[index] if len(fighters) == 2 else None,
                log=prefixed(fighter.key),
            )
            for index, fighter in enumerate(fighters)
        ),
        return_exceptions=True,
    )

    exit_code = 0
    for fighter, result in zip(fighters, results):
        if isinstance(result, BaseException):
            print(f"[{fighter.key}] failed: {result!r}", file=sys.stderr)
            exit_code = 1
        elif result is None:
            print(f"[{fighter.key}] match did not finish (connection lost)", file=sys.stderr)
            exit_code = 1
        else:
            print(
                f"[{fighter.key}] winner={result.get('winner')} "
                f"reason={result.get('reason')} final_tick={result.get('final_tick')}"
            )
    return exit_code


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list:
        print_roster()
        return 0
    if not args.code:
        print("--code is required (or --list to see the roster)", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
