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


# ------------------------------------------------- how much body the model sees

# The email that exposed this, reduced to its shape. An HTML table arrives here
# flattened: every column heading in a run, then the row of values underneath.
# So a deadline schedule reads as a list of labels followed, several hundred
# characters later, by the list of dates that answers them.
TABLE_MAIL = (
    "Dear Sam,\n\nPlease refer to the timeline below. If you fail to meet the "
    "deadlines you will receive grade penalties.\n\n"
    + "Filler paragraph about submission portals and room bookings. " * 34
    + "\nProposal Report\n(Deadline: end of 1st month)\n"
      "Progress Report\n(Deadline: end of 3rd month)\n"
      "Final Report\n(Deadline: end of final month)\n"
      "\nSam Rivera\n30 June 2026\n30 August 2026\n18 December 2026\n"
      "\nKind regards\nPlacements Office\n"
)


def _body_sent(body):
    return base._fmt_email({"id": "x", "subject": "s", "body_text": body}, 1)


def test_a_deadline_table_reaches_the_model_whole():
    """The bug this is here for looked like a model failure and was a cut.

    A flat character limit delivered "Progress Report (Deadline: end of 3rd
    month)" and withheld the "30 August 2026" that answered it, so the model
    did exactly as instructed -- no date you can point at, no task -- and the
    submission never reached the calendar."""
    sent = _body_sent(TABLE_MAIL)
    assert "Progress Report" in sent
    assert "30 August 2026" in sent


def test_the_opening_is_always_sent_whole():
    """Intent lives in the first paragraph. Sampling that for dates too would
    buy nothing and lose the thing every classification depends on."""
    sent = _body_sent(TABLE_MAIL)
    assert "If you fail to meet the deadlines" in sent


def test_a_long_body_with_no_later_dates_stays_cheap():
    """The reach is for dates, not for length. A newsletter must not become
    four times the prompt it was."""
    body = "Marketing copy that never names a date. " * 200
    assert len(_body_sent(body)) < len(body) / 2


def test_the_trim_is_marked_so_the_model_does_not_read_across_the_gap():
    """Two excerpts butted together silently would invite the model to read a
    date from one paragraph as belonging to a sentence in another."""
    sent = _body_sent(TABLE_MAIL)
    assert "trimmed" in sent


def test_a_short_body_is_untouched():
    body = "Please send the form by 3 September 2026."
    assert base._body_for_prompt(body) == body


# --------------------------------------------- the reason line is not a sentence

def test_the_structural_reason_is_codes_not_english():
    """It used to be prose: "Deadline 2026-09-18 found in the text; flagged
    high importance; has an attachment." -- built in the backend, so it sat in
    English in the middle of a Korean reading pane for as long as the feature
    existed. Exactly the bug already fixed once in the briefing.
    """
    import json
    from app import pipeline

    email = {"id": "e1", "subject": "s", "importance": "high", "has_attachments": 1,
             "to_recipients": '[{"address": "me@x.com"}]', "cc_recipients": "[]"}
    codes = pipeline.structural_reason_codes(email, "action", "2026-09-18", "me@x.com")
    keys = [c["key"] for c in codes]

    assert keys == ["sigDeadline", "sigDirect", "sigHighImportance",
                    "sigAsksAction", "sigAttachment"]
    assert codes[0]["vars"] == {"date": "2026-09-18"}
    # Nothing in here may be a phrase: a key is looked up, a sentence is not.
    assert not any(" " in c["key"] for c in codes)

    stored = pipeline._structural_reason(email, "action", "2026-09-18", "me@x.com")
    assert json.loads(stored) == codes


def test_a_message_with_no_signals_says_so_rather_than_nothing():
    from app import pipeline
    email = {"id": "e2", "subject": "s", "importance": "normal", "has_attachments": 0,
             "to_recipients": "[]", "cc_recipients": "[]"}
    codes = pipeline.structural_reason_codes(email, "fyi", None, "me@x.com")
    assert codes == [{"key": "sigNone"}]


def test_cc_only_and_addressed_directly_are_exclusive():
    from app import pipeline
    cc_only = {"id": "e3", "subject": "s", "importance": "normal", "has_attachments": 0,
               "to_recipients": '[{"address": "someone@x.com"}]',
               "cc_recipients": '[{"address": "me@x.com"}]'}
    keys = [c["key"] for c in pipeline.structural_reason_codes(cc_only, "fyi", None, "me@x.com")]
    assert "sigCcOnly" in keys and "sigDirect" not in keys
