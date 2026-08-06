"""ADAM Arena rules engine — pure, no I/O."""

from .resolver import (  # noqa: F401
    Corner,
    Fighter,
    MatchResult,
    MatchState,
    Move,
    NoMove,
    check_end,
    initial_state,
    resolve,
    to_egocentric,
)
