"""LLM response parsing -- the other place things go wrong quietly."""
from __future__ import annotations

import pytest

from app.llm import base

IDS = ["a1", "b2", "c3"]


def test_parses_plain_json_array():
    raw = '[{"id":"a1","bucket":"action","deadline":"2026-09-01","matched":["Thesis"],"rationale":"Needs a reply"}]'
    out = base.parse_classifications(raw, ["a1"])
    assert out[0].bucket == "action"
    assert out[0].deadline == "2026-09-01"
    assert out[0].matched == ["Thesis"]


def test_strips_markdown_fences():
    raw = 'Here you go:\n```json\n[{"id":"a1","bucket":"fyi","deadline":null}]\n```\nHope that helps!'
    assert base.parse_classifications(raw, ["a1"])[0].bucket == "fyi"


def test_recovers_json_buried_in_prose():
    raw = 'Sure! [{"id": "a1", "bucket": "noise"}] — let me know if you need more.'
    assert base.parse_classifications(raw, ["a1"])[0].bucket == "noise"


def test_unwraps_a_results_object():
    raw = '{"results":[{"id":"a1","bucket":"action"}]}'
    assert base.parse_classifications(raw, ["a1"])[0].bucket == "action"


def test_unknown_bucket_falls_back_to_fyi_not_a_crash():
    raw = '[{"id":"a1","bucket":"URGENT!!"}]'
    assert base.parse_classifications(raw, ["a1"])[0].bucket == "fyi"


@pytest.mark.parametrize("raw", [
    '[{"id":"a1","bucket":"fyi","deadline":"not a date"}]',
    '[{"id":"a1","bucket":"fyi","deadline":"null"}]',
    '[{"id":"a1","bucket":"fyi","deadline":""}]',
    '[{"id":"a1","bucket":"fyi","deadline":"2026-13-45"}]',
    '[{"id":"a1","bucket":"fyi","deadline":null}]',
    '[{"id":"a1","bucket":"fyi"}]',
])
def test_junk_deadlines_become_none(raw):
    assert base.parse_classifications(raw, ["a1"])[0].deadline is None


def test_matches_by_id_not_by_position():
    """The model reordered its answers; each email must still get its own."""
    raw = '[{"id":"c3","bucket":"action"},{"id":"a1","bucket":"noise"},{"id":"b2","bucket":"fyi"}]'
    out = {c.email_id: c.bucket for c in base.parse_classifications(raw, IDS)}
    assert out == {"a1": "noise", "b2": "fyi", "c3": "action"}


def test_falls_back_to_position_when_ids_are_missing():
    raw = '[{"bucket":"action"},{"bucket":"fyi"},{"bucket":"noise"}]'
    out = [c.bucket for c in base.parse_classifications(raw, IDS)]
    assert out == ["action", "fyi", "noise"]


def test_a_skipped_email_is_omitted_rather_than_guessed():
    """Better to retry that email next pass than to label it wrongly."""
    raw = '[{"id":"a1","bucket":"action"},{"id":"c3","bucket":"noise"}]'
    out = base.parse_classifications(raw, IDS)
    assert {c.email_id for c in out} == {"a1", "c3"}


def test_garbage_response_raises_so_the_caller_can_fall_back():
    with pytest.raises(ValueError):
        base.parse_classifications("I'm sorry, I can't help with that.", IDS)


def test_prompt_includes_priorities_and_every_email_id():
    emails = [{"id": i, "subject": f"s{i}", "body_text": "x"} for i in IDS]
    prompt = base.build_classify_prompt(emails, ["Thesis", "Internship"])
    assert "Thesis" in prompt and "Internship" in prompt
    assert all(i in prompt for i in IDS)


# ------------------------------------------------------------ digest agenda

AGENDA_IDS = ["a1", "b2", "c3"]


def test_parses_headline_and_items():
    raw = ('{"headline":"Two deadlines today.",'
           '"items":[{"id":"a1","note":"Confirm the room by 5pm"},'
           '{"id":"c3","note":"Send chapter three"}]}')
    headline, items = base.parse_agenda(raw, AGENDA_IDS)
    assert headline == "Two deadlines today."
    assert [i.email_id for i in items] == ["a1", "c3"]
    assert items[0].note == "Confirm the room by 5pm"


def test_agenda_survives_fences_and_prose():
    raw = 'Sure:\n```json\n{"headline":"Quiet day.","items":[{"id":"b2","note":"Reply"}]}\n```'
    headline, items = base.parse_agenda(raw, AGENDA_IDS)
    assert headline == "Quiet day."
    assert items[0].email_id == "b2"


def test_hallucinated_ids_are_dropped_not_shown():
    """A briefing row that opens nothing is worse than a missing row."""
    raw = '{"headline":"x","items":[{"id":"NOT-REAL","note":"?"},{"id":"a1","note":"ok"}]}'
    _, items = base.parse_agenda(raw, AGENDA_IDS)
    assert [i.email_id for i in items] == ["a1"]


def test_duplicate_ids_appear_once():
    raw = '{"headline":"x","items":[{"id":"a1","note":"one"},{"id":"a1","note":"again"}]}'
    _, items = base.parse_agenda(raw, AGENDA_IDS)
    assert len(items) == 1


def test_agenda_with_no_items_is_not_an_error():
    headline, items = base.parse_agenda('{"headline":"Nothing today.","items":[]}', AGENDA_IDS)
    assert headline == "Nothing today." and items == []


def test_agenda_garbage_raises_so_the_caller_can_fall_back():
    with pytest.raises(ValueError):
        base.parse_agenda("I could not do that", AGENDA_IDS)


def test_digest_prompt_carries_the_ids_the_model_must_choose_from():
    emails = [{"id": i, "subject": f"s{i}", "body_text": "x"} for i in AGENDA_IDS]
    prompt = base.build_digest_prompt(emails, ["Thesis"])
    assert all(f"id: {i}" in prompt for i in AGENDA_IDS)
