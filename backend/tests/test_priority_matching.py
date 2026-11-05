"""Does a declared priority actually match the mail it names?

Every one of these failed before 2026-09-19, silently, while 360 other tests
were green -- because every fixture topic in the suite was an English word
longer than three letters, which is the one shape the old matcher handled.

The bug was found by running the real model: three emails came back with
relevance 0.4013, which is sigmoid(PRIOR_BIAS) to four places. Identical
relevance across three different emails is not a number, it is a default.
"""
from __future__ import annotations

import pytest

from app import priority, relevance

EN = {"subject": "Action required: confirm your CO-OP placement by 2026-09-22",
      "from_name": "Career Centre", "from_address": "coop@uni.edu",
      "body_text": "Please confirm your placement in writing."}
KO = {"subject": "논문 심사 일정 확정 안내 - 2026-09-22까지 회신 바랍니다",
      "from_name": "대학원 교학팀", "from_address": "grad@uni.edu",
      "body_text": "논문 심사 희망 일정을 회신해 주시기 바랍니다."}
NOISE = {"subject": "Weekly round-up: 40% off boots this weekend",
         "from_name": "ShopCo", "from_address": "noreply@shop.example",
         "body_text": "Unsubscribe at any time. I said email me if you want out."}
AI = {"subject": "AI seminar next Tuesday", "from_name": "CSE",
      "from_address": "cse@uni.edu", "body_text": "A talk on AI safety."}


def matched(email, topic):
    return bool(priority.match_priorities(email, [{"topic": topic}]))


@pytest.mark.parametrize("email,topic", [
    # The whole point of the app, in the app's primary language. A Korean topic
    # used to tokenise to nothing, because the splitter was `[^a-z0-9]+` and
    # every Korean character is therefore a separator.
    (KO, "논문 심사"),
    (KO, "논문"),
    (KO, "심사 일정"),          # words apart in the text, both present
    # Short and punctuated ASCII topics: exactly what people put on a list.
    (EN, "CO-OP"),
    (EN, "co-op"),
    (AI, "AI"),
])
def test_a_declared_topic_matches_the_mail_that_names_it(email, topic):
    assert matched(email, topic)


@pytest.mark.parametrize("email,topic", [
    # The reason the old code threw away short terms: substring matching turns
    # `AI` into a match for "said" and "email". Fixed by matching short ASCII
    # terms on a word boundary rather than by discarding them.
    (NOISE, "AI"),
    (NOISE, "TA"),
    (NOISE, "R&D"),
    (NOISE, "CO-OP"),
    (NOISE, "논문 심사"),
    (EN, "thesis defence"),
])
def test_a_topic_does_not_match_mail_that_merely_contains_its_letters(email, topic):
    assert not matched(email, topic)


def test_a_match_actually_moves_relevance():
    """The matcher is only worth fixing because relevance depends on it. A
    topic that matches and does not move R(e) is still a broken priority list."""
    prios = [{"topic": "CO-OP"}]
    hits = [p["topic"] for p in priority.match_priorities(EN, prios)]
    assert hits == ["CO-OP"]

    score, _ = relevance.topic_match(EN, {}, keyword_hits=hits)
    with_topic = relevance.relevance(relevance.features_for(EN, score))
    without = relevance.relevance(relevance.features_for(EN, 0.0))
    assert with_topic > without + 0.3, (with_topic, without)
    # 0.4013 is sigmoid(PRIOR_BIAS). Seeing it here means the topic contributed
    # nothing, which is the exact signature of the bug this file exists for.
    assert with_topic != pytest.approx(0.4013, abs=1e-4)


def test_every_language_the_ui_supports_can_carry_a_priority():
    """The UI ships in en/ko/zh/ja. A matcher that only works for one of them
    is a matcher that works for a quarter of the users it was built for."""
    cases = [
        ({"subject": "Scholarship application deadline", "body_text": ""}, "scholarship"),
        ({"subject": "장학금 신청 안내", "body_text": ""}, "장학금"),
        ({"subject": "奖学金申请通知", "body_text": ""}, "奖学金"),
        ({"subject": "奨学金の申請について", "body_text": ""}, "奨学金"),
    ]
    for email, topic in cases:
        assert matched(email, topic), (topic, email["subject"])
