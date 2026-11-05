"""Who sent it, and official mail from no-reply addresses.

Two failures found by probing the v1.2.7 ranker against what research says
users act on (marketing thread, 2026-10-31):

1. A Canvas "Assignment due" from notifications@instructure.com, which the
   model called action with a deadline, scored relevance 0.083 and landed in
   noise. The must-see and dated-ask floors in `cues` now keep it; these tests
   pin that from the user's side.
2. Sender relationship, direct address and highlighted senders were computed
   but had no day-one weight, so they could not lift any mail on a fresh
   install.
"""
from __future__ import annotations

from datetime import date, timedelta

from app import priority, relevance
from app.relevance import Thresholds

# Dated asks only keep mail out of noise while the date is still ahead, so the
# fixtures name a date ten days from whenever the suite runs.
DUE = date.today() + timedelta(days=10)
CANVAS = {"subject": f"Assignment due {DUE:%b} {DUE.day}: Lab report 4",
          "from_address": "notifications@instructure.com",
          "body_text": f"Your assignment Lab report 4 is due {DUE:%b} {DUE.day} at 23:59."}
KO_PORTAL = {"subject": f"과제 제출 마감 안내: {DUE.month}월 {DUE.day}일까지",
             "from_address": "no-reply@portal.snu.ac.kr",
             "body_text": "기한 내 제출 바랍니다."}
SHOP_ACTION = {"subject": "Complete your order before midnight",
               "from_address": "noreply@shop.example",
               "body_text": "Finish checkout to keep your cart."}
SHOP_SALE = {"subject": "Sale ends tonight: 40% off",
             "from_address": "noreply@shop.example",
             "body_text": "Unsubscribe any time."}
UNI_DIGEST = {"subject": "Campus weekly digest",
              "from_address": "noreply@mail.uni.edu",
              "body_text": "Events this week."}


def _bucket(email, **kw):
    return relevance.axes_for(email, **kw).bucket(Thresholds())


# ------------------------------------------------------- official machine mail

def test_lms_deadline_the_model_flagged_is_action_not_noise():
    assert _bucket(CANVAS, model_bucket="action", deadline=DUE.isoformat(), task_count=1) == "action"


def test_korean_portal_deadline_is_not_noise_without_any_ai():
    assert priority.structural_bucket(KO_PORTAL) != "noise"


def test_lms_mail_is_not_noise_on_the_structural_path():
    assert priority.structural_bucket(CANVAS) == "action"


def test_official_newsletter_subject_is_still_noise():
    assert relevance.structural_noise(UNI_DIGEST) == 1.0
    assert priority.structural_bucket(UNI_DIGEST) == "noise"


# ---------------------------------------------- model verdict vs the sender guess

def test_model_action_alone_does_not_overrule_it():
    assert _bucket(SHOP_ACTION, model_bucket="action") == "noise"


def test_a_sales_subject_keeps_the_penalty_whatever_the_model_says():
    assert _bucket(SHOP_SALE, model_bucket="action", deadline="2026-11-01", task_count=1) == "noise"


# ------------------------------------------------------------ relational priors

def test_direct_address_and_affinity_raise_relevance_on_day_one():
    base = relevance.relevance(relevance.features_for(SHOP_ACTION | {"from_address": "a@b.com"}, 0.0))
    direct = relevance.relevance(relevance.features_for(
        SHOP_ACTION | {"from_address": "a@b.com"}, 0.0, addressed_directly=True))
    known = relevance.relevance(relevance.features_for(
        SHOP_ACTION | {"from_address": "a@b.com"}, 0.0, addressed_directly=True, affinity=1.0))
    assert base < direct < known
    assert known > 0.75


def test_a_highlighted_sender_beats_the_bulk_sender_penalty():
    feats = relevance.features_for(SHOP_SALE, 0.0, highlighted=True)
    assert relevance.relevance(feats) > relevance.DEFAULT_THETA_REL


def test_relational_priors_cannot_outweigh_a_full_topic_match():
    rel = relevance.PRIOR_DIRECT + relevance.PRIOR_AFFINITY
    assert rel < relevance.PRIOR_TOPIC


def test_explain_names_the_relational_contributions():
    axes = relevance.axes_for(SHOP_ACTION | {"from_address": "a@b.com"},
                              addressed_directly=True, affinity=0.4)
    contrib = relevance.explain(axes, Thresholds())["contributions"]
    assert contrib["direct_address"] == round(relevance.PRIOR_DIRECT, 4)
    assert contrib["affinity"] == round(relevance.PRIOR_AFFINITY * 0.4, 4)
