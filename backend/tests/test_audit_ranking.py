"""Run the objective audit as part of the ordinary test run.

Two reasons this exists rather than living only as a command to remember.

First, the audit found four real defects that 322 unit tests did not, so it is
not an optional extra -- if it only runs when someone remembers it, it will stop
running. Second, the command itself is a trap: this repo has two `scripts/`
directories, so `python3 scripts/audit_ranking.py` from the repo root finds the
wrong one and reports a missing file. `pytest` is the command that is already
in everyone's fingers.

It is deterministic (fixed seeds) and takes about two seconds.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

AUDIT = Path(__file__).resolve().parent.parent / "scripts" / "audit_ranking.py"


def load_audit():
    spec = importlib.util.spec_from_file_location("audit_ranking", AUDIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_ranking"] = module
    spec.loader.exec_module(module)
    return module


def test_the_audit_file_is_where_the_docs_say_it_is():
    """Guards the failure that prompted this file: a path that reads plausibly
    and resolves to nothing."""
    assert AUDIT.is_file(), f"expected the audit at {AUDIT}"


def test_every_audit_claim_passes(monkeypatch, tmp_path):
    from app import db

    # The audit points db at its own temp directories, but restoring the module
    # globals afterwards keeps it from leaking into whatever test runs next.
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "audit.db")

    audit = load_audit()
    assert audit.main() == 0, "see the printed report above for the failed claim"


def test_a_claim_that_raises_is_reported_as_failed_not_swallowed(capsys):
    """An audit that dies mid-run and prints nothing is the silent failure it
    exists to catch -- this is how that regression would show up."""
    audit = load_audit()
    report = audit.Report()
    report.check(True, "first claim")
    with report.guard("second claim"):
        raise RuntimeError("boom")
    report.check(True, "third claim")

    assert report.render() is False
    printed = capsys.readouterr().out
    assert "first claim" in printed and "third claim" in printed
    assert "RuntimeError: boom" in printed
