"""Tests for ducktest.mnemonic (moved out of the library module so they are
normal, auto-collected pytest tests)."""

from datetime import datetime

from ducktest.mnemonic import _ADJECTIVES, _NOUNS, mnemonic, run_id


def test_mnemonic_shape():
    name = mnemonic()
    assert name.count("-") == 2
    a, n, d = name.split("-")
    assert a in _ADJECTIVES and n in _NOUNS
    assert 0 <= int(d) <= 99


def test_mnemonic_word_count():
    assert mnemonic(words=3).count("-") == 3


def test_run_id_sortable_and_memorable():
    rid = run_id(now=datetime(2026, 6, 23, 23, 44, 22))
    stamp, sep, mnem = rid.partition("--")
    assert sep == "--"
    assert stamp == "2026-06-23T23-44-22Z"  # ISO basic-style, UTC; no colons (Windows-safe)
    assert ":" not in rid
    assert "-" in mnem
    adj, noun, digits = mnem.split("-")
    assert adj in _ADJECTIVES and noun in _NOUNS
    assert len(digits) == 2 and int(digits) >= 0
