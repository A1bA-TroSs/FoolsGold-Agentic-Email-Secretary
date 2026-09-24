"""The deterministic pre-send checks (P0 of the assist plan).

The tests that matter here are the negative ones. A checker is judged by what
it stays quiet about: the evidence says a checker whose warnings are mostly
false alarms gets ignored wholesale, so every rule below has at least as many
"must not fire" cases as "must fire" ones.
"""
from __future__ import annotations

import pytest

from app.assist import checks
from app.assist.checks import Issue, check, check_attachment, check_honorific, own_text


def kinds(issues: list[Issue]) -> list[str]:
    return [i.kind for i in issues]


# --------------------------------------------------------------- own words

def test_the_quoted_original_is_not_the_users_words():
    body = ("첨부 확인 부탁드립니다.\n\n"
            "On Thu, 17 Sep 2026, Career Programs wrote:\n"
            "> 자료를 첨부합니다.\n> 내일 보자.\n")
    text = own_text(body)
    assert "부탁드립니다" in text
    assert "자료를 첨부합니다" not in text and "내일 보자" not in text


def test_a_signature_is_not_the_users_words():
    body = "확인했습니다.\n\n-- \nDanny Park\n교환학생 | 서울\n"
    assert "Danny Park" not in own_text(body)


def test_offsets_survive_the_stripping():
    body = "첨부합니다.\n> 다른 사람 말.\n"
    text = own_text(body)
    assert len(text) == len(body), "spans must still point at the real body"
    assert text.index("첨부합니다") == body.index("첨부합니다")


# ------------------------------------------------------------------ subject

@pytest.mark.parametrize("subject", ["", "   ", "Re:", "RE: ", "회신:", "답장:",
                                     "(제목 없음)", "[no subject]", "Fwd:", "Re: Re:"])
def test_a_subject_that_says_nothing_is_flagged(subject):
    assert kinds(checks.check_subject(subject)) == ["subject_missing"]


@pytest.mark.parametrize("subject", ["Re: 세미나 등록", "Fwd: contract", "회의", "Re[2]: 안건",
                                     "RE: FW: 제출", "답장 요청드립니다"])
def test_a_real_subject_is_left_alone(subject):
    assert checks.check_subject(subject) == []


# --------------------------------------------------------------- attachment

@pytest.mark.parametrize("body", [
    "이력서를 첨부합니다.",
    "요청하신 자료 첨부드립니다.",
    "파일을 첨부했습니다. 확인 부탁드립니다.",
    "I've attached the report.",
    "Please see the attached slides.",
    "The signed form is enclosed.",
])
def test_a_promised_attachment_that_is_not_there(body):
    assert kinds(check_attachment(body, has_attachment=False)) == ["attachment_missing"]


@pytest.mark.parametrize("body", [
    "보내주신 첨부파일 잘 받았습니다.",                    # theirs, not ours
    "첨부파일 확인했습니다. 감사합니다.",                   # already received
    "말씀하신 첨부 자료는 내일 보내드리겠습니다.",            # promised for later
    "Thanks for the attached draft.",
    "I will attach the revised version tomorrow.",
    "Your attachment did not open on my machine.",
])
def test_an_attachment_someone_else_sent_is_not_our_problem(body):
    assert check_attachment(body, has_attachment=False) == []


def test_nothing_is_said_when_a_file_is_actually_attached():
    assert check_attachment("이력서를 첨부합니다.", has_attachment=True) == []


def test_only_one_attachment_warning_however_many_times_it_is_promised():
    body = "자료를 첨부합니다. 파일을 첨부합니다. 첨부드립니다."
    assert len(check_attachment(body, has_attachment=False)) == 1


# ---------------------------------------------------------------- honorific

def test_polite_and_plain_in_one_message_is_flagged():
    got = check_honorific("교수님, 자료 보내드립니다.\n내일 보자.\n")
    assert kinds(got) == ["honorific_mixed"]
    assert got[0].evidence == "내일 보자."


def test_the_minority_register_is_the_one_shown():
    got = check_honorific("확인했어.\n그때 보자.\n감사합니다.\n")
    assert got[0].evidence == "감사합니다.", "one polite line among plain ones"


@pytest.mark.parametrize("body", [
    "안녕하세요. 자료 보내드립니다. 확인 부탁드립니다.",          # all polite
    "확인했어. 내일 보자. 고마워.",                           # all plain
    "Hi Sarah,\n\nThanks for the update. I'll review it today.",  # no Korean
    "안녕하세요.\n\n- 보고서 제출\n- 발표 자료\n\n감사합니다.",     # bullets have no register
    "제목: 회의 안건\n장소: 302호\n감사합니다.",                 # label lines
    "\"내일 보자\"라고 하셨습니다.",                           # quoting someone's words
])
def test_a_consistent_message_is_left_alone(body):
    assert check_honorific(body) == []


def test_a_plain_line_inside_a_quote_is_not_the_users_register():
    body = ("확인 부탁드립니다.\n\nOn Thu, 17 Sep 2026, 김교수 wrote:\n> 내일 보자.\n")
    assert check(subject="세미나 회신", body=body) == []


# ------------------------------------------------------------------ English

def fake_linter(text: str):
    return [(0, 4, "Spelling", text[:4])]


def exploding_linter(text: str):
    raise RuntimeError("wasm did not load")


def test_english_issues_come_from_the_injected_linter():
    got = check("주제", "teh seminar", linter=fake_linter)
    assert kinds(got) == ["spelling_en"] and got[0].end == 4


def test_a_broken_linter_makes_the_app_quieter_not_broken():
    assert check("주제", "teh seminar", linter=exploding_linter) == []


def test_no_linter_means_no_english_claims():
    assert check("주제", "teh seminar") == []


# --------------------------------------------------------------------- all

def test_issues_are_ordered_and_capped():
    body = "자료를 첨부합니다.\n내일 보자.\n"
    got = check("Re:", body, has_attachment=False, linter=lambda t: [
        (0, 1, "Spelling", "x") for _ in range(20)])
    assert len(got) <= checks.MAX_ISSUES
    assert kinds(got)[:3] == ["subject_missing", "attachment_missing", "honorific_mixed"]


def test_a_clean_message_produces_nothing():
    assert check("세미나 등록 확인", "안녕하세요.\n등록 마쳤습니다. 감사합니다.\n") == []


# ------------------------------------------------- what the English filter keeps

@pytest.mark.parametrize("token", ["Refference", "processment", "Patk", "recieve"])
def test_a_misspelled_word_survives_the_filter(token):
    from app.assist.harper_bridge import keep
    assert keep(token) is True


@pytest.mark.parametrize("token", [
    "fd6ff14",        # a commit hash
    "da49", "a33",    # id fragments
    "fi",             # ligature debris from a pasted PDF
    "out last",       # a phrase lint: an opinion, not a typo
    "SHLRO",          # an acronym
    "v2.1", "report_final", "danny@uni.edu", "C:/temp",
])
def test_things_that_are_not_words_are_dropped(token):
    from app.assist.harper_bridge import keep
    assert keep(token) is False


def test_the_mailbox_teaches_its_own_vocabulary():
    from app.assist.harper_bridge import keep
    assert keep("Qingyue") is True
    assert keep("Qingyue", known=frozenset({"qingyue"})) is False
