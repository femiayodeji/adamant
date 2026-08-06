"""CLI: ``python -m agents.harness run|analyze``.

``run`` spends real API budget, so it refuses to start without
``--yes-spend-api-budget``. ``analyze`` only reads stored logs and never calls a
model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .aggregate import aggregate, require_single_bracket, summarise_exclusions
from .analyze import analyze_logs, exploitation_speed, no_move_breakdown
from .model_client import LiteLLMClient, ModelSpec
from .runner import (
    ArenaEndpoints,
    ArenaRunner,
    Opponent,
    plan_matches,
    run_batch,
    write_csv,
)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def cmd_run(args) -> int:
    if not args.yes_spend_api_budget:
        print("refusing to run: this calls paid models. Pass --yes-spend-api-budget.")
        return 2
    entrants = _split(args.models)
    opponents = [Opponent(name, "bot") for name in _split(args.bots)]
    opponents += [Opponent(name, "model") for name in _split(args.opponent_models or "")]
    specs = plan_matches(
        entrants,
        opponents,
        repeats=args.repeats,
        deadline_ms=args.deadline_ms,
        max_ticks=args.max_ticks,
    )
    # "none" means: do not send the parameter at all (some models reject it).
    raw = str(args.temperature).strip().lower()
    temperature = None if raw in ("", "none", "null") else float(raw)
    runner = ArenaRunner(
        ArenaEndpoints(args.http_url, args.ws_url),
        make_client=lambda spec: LiteLLMClient(
            ModelSpec(model=spec.entrant, temperature=temperature)
        ),
    )
    rows = asyncio.run(run_batch(specs, runner.run_one, concurrency=args.concurrency))
    write_csv(rows, args.out)
    print(f"{len(rows)} matches -> {args.out}")
    print(json.dumps(summarise_exclusions(rows), indent=2))
    return 0


def cmd_analyze(args) -> int:
    paths = sorted(Path(args.logs).glob(args.glob))
    if not paths:
        print(f"no logs matched {args.logs}/{args.glob}")
        return 1
    rows = analyze_logs(paths)
    if args.out:
        write_csv(rows, args.out)
        print(f"{len(rows)} corner-rows -> {args.out}")
    if args.strict_bracket:
        require_single_bracket(rows)
    print("\n== exclusions ==")
    print(json.dumps(summarise_exclusions(rows), indent=2))
    print("\n== no-moves by cause ==")
    print(json.dumps(dict(no_move_breakdown(rows)), indent=2))
    print("\n== exploitation speed (ticks to KO per opponent) ==")
    for (entrant, opponent), stats in sorted(exploitation_speed(rows).items()):
        print(f"  {entrant} vs {opponent}: {json.dumps(stats)}")
    print("\n== aggregates ==")
    for summary in aggregate(rows, fallback_limit=args.fallback_limit):
        print(json.dumps(summary))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agents.harness")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a batch of matches (spends API budget)")
    run.add_argument("--models", required=True, help="comma-separated OpenRouter model ids")
    run.add_argument("--bots", default="jab,random", help="comma-separated ladder bots")
    run.add_argument("--opponent-models", default="", help="comma-separated model opponents")
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--deadline-ms", type=int, default=5000)
    run.add_argument("--max-ticks", type=int, default=60)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--temperature", default="0")
    run.add_argument("--http-url", required=True)
    run.add_argument("--ws-url", required=True)
    run.add_argument("--out", default="agents/harness/results.csv")
    run.add_argument("--yes-spend-api-budget", action="store_true")
    run.set_defaults(func=cmd_run)

    analyze = sub.add_parser("analyze", help="offline metrics from stored logs")
    analyze.add_argument("--logs", default="server/logs")
    analyze.add_argument("--glob", default="*.json")
    analyze.add_argument("--out", default="")
    analyze.add_argument("--fallback-limit", type=float, default=0.10)
    analyze.add_argument("--strict-bracket", action="store_true")
    analyze.set_defaults(func=cmd_analyze)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
