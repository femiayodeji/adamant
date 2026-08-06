"""The harness CLI. ``analyze`` is offline; ``run`` must refuse to spend by default."""

from __future__ import annotations

import csv
import json

from agents.harness.__main__ import main
from agents.tests.sample_log import abandoned_log, ko_log, sample_log


def test_run_refuses_to_spend_api_budget_without_an_explicit_flag(capsys):
    code = main([
        "run", "--models", "anthropic/claude-opus-5",
        "--http-url", "https://arena.example.com", "--ws-url", "wss://arena.example.com",
    ])
    assert code == 2
    assert "refusing to run" in capsys.readouterr().out


def test_analyze_reads_logs_and_writes_a_csv(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "m1.json").write_text(json.dumps(sample_log()), encoding="utf-8")
    (logs / "m2.json").write_text(json.dumps(ko_log()), encoding="utf-8")
    (logs / "m3.json").write_text(json.dumps(abandoned_log()), encoding="utf-8")
    out = tmp_path / "rows.csv"

    code = main(["analyze", "--logs", str(logs), "--out", str(out), "--strict-bracket"])
    assert code == 0

    printed = capsys.readouterr().out
    assert "exclusions" in printed
    assert "exploitation speed" in printed
    assert "no-moves by cause" in printed

    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 6  # three matches, two corners each


def test_analyze_reports_when_there_is_nothing_to_read(tmp_path, capsys):
    assert main(["analyze", "--logs", str(tmp_path)]) == 1
    assert "no logs matched" in capsys.readouterr().out
