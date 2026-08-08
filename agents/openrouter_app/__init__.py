"""A small standalone app that fights Adamant matches with OpenRouter models.

    python -m agents.openrouter_app --list
    python -m agents.openrouter_app --code RUMBLE-4F2 --agent bulldozer --agent cipher

Three fighters (``roster.py``), one fighter-in-a-corner coroutine (``fighter.py``),
the HTTP client in ``agents/common/openrouter.py``, and the shared protocol and
decision loop from ``agents/common``. Separate from ``agents/reference_agent.py``
on purpose: that file is a single-file example written to be copied, this is an
app with a roster you can point at a match code — including from the platform's
own lobby, which spawns these same three through ``server/llm_agents.py``.

**It spends money.** One model call per fighter per tick.

Deliberately empty of re-exports: the two importers of this package take the
submodule they need (``from agents.openrouter_app import roster``), and a
convenience alias here would be a second name for the same thing to keep in step.
"""
