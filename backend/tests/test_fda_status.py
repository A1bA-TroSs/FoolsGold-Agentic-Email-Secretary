"""The Full Disk Access status is a key the UI words, and names the right app.

Regression (2026-09-21): a user who downloaded the DMG was told to "add the app
you launched from -- Terminal if you ran `npm start` there", in English, on a
Korean screen. The backend now names *what* is wrong and *who* needs the
permission; the frontend decides how that reads.
"""
from __future__ import annotations

import sys

from app.sources import applemail


def test_permission_denied_is_a_key_not_a_sentence(monkeypatch):
    def denied(self):
        raise PermissionError("Operation not permitted")
    monkeypatch.setattr(applemail.AppleMailSource, "_status", denied)
    st = applemail.AppleMailSource().status().as_dict()
    assert st["ready"] is False and st["needs_setup"] is True
    assert st["detail_key"] == "fdaNeeded"
    assert st["detail_vars"] == {"launcher": "terminal"}   # tests run unfrozen
    assert "npm" not in st["detail"] and "Terminal" not in st["detail"]


def test_a_frozen_build_names_the_app(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert applemail._launcher() == "app"
