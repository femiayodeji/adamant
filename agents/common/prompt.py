"""Prompt construction, shared by the reference agent and the harness.

Bump ``PROMPT_VERSION`` whenever the text below changes: it is reported in the
agent profile and stored in the match log, and comparing two agents across a
silent prompt change is not a measurement (plan, Evaluation Design).
"""

from __future__ import annotations

from .rules import ACTIONS, MAX_CONSECUTIVE_REPEATS, RULES_SUMMARY

PROMPT_VERSION = "ref-v2"

INSTRUCTIONS = """\
You are one of the two fighters. Read the state and the recent history, predict
what the opponent will do this tick, and pick the action that beats it.

Think about the payoff, not just about staying safe. Blocking everything is
survivable and scores nothing; a match where neither fighter deals damage is a
draw. Landing damage and riposting is how you win, and trading (attacking into
an incoming attack) is sometimes the best line available.

The repetition cap makes that concrete: no single action can carry you, and it
cuts both ways. Watch the opponent's last_action and repeat_streak as closely as
your own - an opponent at a streak of 3 must change action this tick or be
voided and defenseless, and that is the cheapest read in the game.\
"""

#: Kept separate, and always emitted last, so that a persona can add strategy
#: without ever being able to displace the protocol contract (spec 10).
OUTPUT_CONTRACT = """\
Reply with a SINGLE JSON object and nothing else. No prose before or after, no
markdown fences, no XML tags:

{"action": "<one of: %s>", "reason": "<why, at most 200 characters>"}\
""" % ", ".join(ACTIONS)


def build_system_prompt(persona=None) -> str:
    """Rules, then instructions, then the persona's style, then the contract.

    ``persona`` is any object with a ``guidance`` string (see
    ``agents/common/personas.py``); ``None`` yields the neutral prompt. This
    module deliberately does not import personas, so the strategy layer stays
    optional for anyone copying the example.
    """
    parts = [RULES_SUMMARY, INSTRUCTIONS]
    if persona is not None and getattr(persona, "guidance", ""):
        parts.append(persona.guidance)
    parts.append(OUTPUT_CONTRACT)
    return "\n".join(parts)


SYSTEM_PROMPT = build_system_prompt()


def _fmt_event(event: dict) -> str:
    """Render one agent-facing event object (spec 4.5, egocentric parties)."""
    kind = event.get("type")
    who = {"self": "you", "opponent": "the opponent"}
    attacker = who.get(event.get("attacker"), event.get("attacker"))
    defender = who.get(event.get("defender"), event.get("defender"))
    subject = who.get(event.get("who"), event.get("who"))
    if kind == "hit":
        return f"{attacker} hit {defender} with {event.get('action')} for {event.get('damage')}"
    if kind == "blocked":
        return f"{defender} blocked {attacker}'s {event.get('action')} with {event.get('defense')}"
    if kind == "dodged":
        return (
            f"{defender} dodged {attacker}'s {event.get('action')} with "
            f"{event.get('defense')} and riposted {event.get('riposte')}"
        )
    if kind == "exhausted":
        return f"{subject} could not afford {event.get('attempted')} and idled"
    if kind == "void":
        return (
            f"{subject} repeated {event.get('action')} {event.get('streak')} times in a "
            f"row: VOID, stripped to nothing and defenseless"
        )
    if kind == "no_move":
        return f"{subject} made no move ({event.get('cause')}), penalty {event.get('penalty')}"
    if kind == "ko":
        return f"{subject} was knocked out"
    return str(event)


def _fmt_fighter(fighter: dict) -> str:
    line = (
        f"hp {fighter.get('hp')}  stamina {fighter.get('stamina')}  "
        f"points {fighter.get('points')}  penalties {fighter.get('penalties')}"
    )
    last = fighter.get("last_action")
    if last:
        line += f"  last_action {last} (repeat_streak {fighter.get('repeat_streak') or 0})"
    return line


def _cap_line(me: dict) -> str | None:
    """Spell out the repetition cap for this tick, in the imperative.

    The numbers are already in ``state_update.self``; stating the consequence in
    words is what stops a model from reading past them.
    """
    last = me.get("last_action")
    if not last:
        return None
    try:
        streak = int(me.get("repeat_streak") or 0)
    except (TypeError, ValueError):
        return None
    if streak >= MAX_CONSECUTIVE_REPEATS:
        return (
            f"REPETITION CAP REACHED: you have played {last} {streak} times in a row. "
            f"Another {last} this tick is VOID - it does nothing, defends nothing, "
            f"still costs stamina, and leaves you defenseless. Choose a different action."
        )
    return (
        f"Repetition: you have played {last} {streak} time(s) in a row; "
        f"{MAX_CONSECUTIVE_REPEATS - streak} more before a {last} would be voided."
    )


def build_user_prompt(
    state: dict,
    opponent_name: str | None = None,
    max_ticks: int | None = None,
) -> str:
    """Turn one ``state_update`` message into the user turn of the prompt."""
    tick = state.get("tick")
    header = f"Tick {tick}"
    if max_ticks:
        header += f" of {max_ticks}"
    header += f". You have {state.get('deadline_ms')} ms to answer."

    me = state.get("self") or {}
    lines = [
        header,
        "",
        f"You:      {_fmt_fighter(me)}",
        f"Opponent: {_fmt_fighter(state.get('opponent', {}))}"
        + (f"   (name: {opponent_name})" if opponent_name else ""),
        "",
    ]
    cap = _cap_line(me)
    if cap:
        lines += [cap, ""]

    history = state.get("history") or []
    if history:
        lines.append("Recent ticks, most recent first:")
        for entry in history:
            moves = entry.get("moves", {})
            events = "; ".join(_fmt_event(e) for e in entry.get("events", []))
            line = (
                f"  tick {entry.get('tick')}: you played {moves.get('self')}, "
                f"opponent played {moves.get('opponent')}"
            )
            if events:
                line += f" -> {events}"
            lines.append(line)
    else:
        lines.append("No history yet: this is the opening tick.")

    lines += ["", "Choose your action for this tick. Reply with the JSON object only."]
    return "\n".join(lines)
