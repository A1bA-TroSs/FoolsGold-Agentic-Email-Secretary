"""F2 and A3 from the 2026-11-01 roadmap.

F2: the no-AI path reads Korean -- dates, action words, the (광고) marker, and
    Hangul in the embedder.
A3: exam, payment, ticket and invitation mail is never hidden as noise, even
    from a no-reply sender on day one.

The probes are the ones measured on 2026-10-31 (ranker-code-map.md), where the
Korean Canvas notice came back as noise with no deadline.
"""
from __future__ import annotations

from datetime import date

from app import cues, priority
from app import relevance as R
from app.relevance import Thresholds

TODAY = date(2026, 11, 1)  # a Sunday


def mail(**over):
    base = dict(
        id="e1", subject="Project update", from_address="lee@uni.edu", from_name="Lee",
        to_recipients="[]", cc_recipients="[]", body_text="Some body text.",
        is_read=0, is_answered=0, is_flagged=0, has_attachments=0,
    )
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Korean dates
# --------------------------------------------------------------------------

def test_korean_month_day():
    assert priority.extract_deadline("과제 제출 마감 안내: 11월 3일까지", TODAY) == "2026-11-03"


def test_korean_full_date_and_dotted_date():
    assert priority.extract_deadline("2027년 1월 5일 오후 6시 마감", TODAY) == "2027-01-05"
    assert priority.extract_deadline("신청 기한: 2026.11.20.", TODAY) == "2026-11-20"


def test_korean_month_day_rolls_into_next_year():
    assert priority.extract_deadline("1월 10일까지 제출", TODAY) == "2027-01-10"


def test_slash_date_only_with_weekday():
    assert priority.extract_deadline("11/7(토) 오후 2시", TODAY) == "2026-11-07"
    assert priority.extract_deadline("점수는 1/2 반영", TODAY) is None


def test_korean_relative_days():
    assert priority.extract_deadline("내일까지 회신 부탁드립니다", TODAY) == "2026-11-02"
    assert priority.extract_deadline("오늘 중까지 확인해 주세요", TODAY) == "2026-11-01"


def test_korean_weekday():
    # Wednesday 2026-11-04: this Friday is 11-06, next week's Friday is 11-13.
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("금요일 오후 6시까지 제출", wed) == "2026-11-06"
    assert priority.extract_deadline("다음 주 금요일까지 제출", wed) == "2026-11-13"
    assert priority.extract_deadline("수요일까지", wed) == "2026-11-04"


def test_english_dates_still_win():
    assert priority.extract_deadline("Due Nov 3. 참고: 11월 9일 설명회", TODAY) == "2026-11-03"


def test_no_date_in_plain_korean():
    assert priority.extract_deadline("안녕하세요, 지난 회의 자료 공유드립니다.", TODAY) is None


# --------------------------------------------------------------------------
# Korean action and noise words (structural bucket)
# --------------------------------------------------------------------------

def test_korean_action_words_make_action():
    email = mail(subject="연구실 보고서 제출 요청", body_text="이번 주 내로 부탁드립니다.")
    assert priority.structural_bucket(email) == "action"


def test_korean_ad_marker_is_noise():
    email = mail(subject="(광고) 가을 신상 특가 안내", from_address="shop@store.kr")
    assert priority.structural_bucket(email) == "noise"
    assert R.structural_noise(email) == 1.0


def test_plain_korean_mail_is_fyi():
    email = mail(subject="지난 회의 자료 공유", body_text="자료 첨부합니다.")
    assert priority.structural_bucket(email) == "fyi"


# --------------------------------------------------------------------------
# Hangul in the embedder
# --------------------------------------------------------------------------

def test_korean_text_embeds_to_a_non_zero_vector():
    vec = R.hashing_embed("과제 제출 마감 안내")
    assert any(vec)


def test_korean_particles_share_dimensions():
    """`과제를` and `과제` must be close: bigrams are what they share."""
    a = R.hashing_embed("과제를 제출하세요")
    b = R.hashing_embed("과제 제출")
    assert R.cosine(a, b) > 0.4


def test_english_embedding_unchanged():
    """Latin tokens are hashed exactly as before, so learned English centroids
    stay valid."""
    import hashlib
    vec = R.hashing_embed("thesis")
    idx = int.from_bytes(hashlib.blake2b(b"thesis", digest_size=4).digest(), "big") % 256
    assert vec[idx] == 1.0


# --------------------------------------------------------------------------
# A3: always-important types
# --------------------------------------------------------------------------

def test_kinds():
    assert cues.always_important(mail(subject="CS101 Midterm room change")) == "exam"
    assert cues.always_important(mail(subject="기말고사 일정 안내")) == "exam"
    assert cues.always_important(mail(subject="2학기 등록금 납부 안내")) == "payment"
    assert cues.always_important(mail(subject="Your boarding pass for KE017")) == "ticket"
    assert cues.always_important(mail(subject="항공권 예약 확정")) == "ticket"
    assert cues.always_important(mail(subject="Invitation: Lab meeting @ Tue 3pm")) == "invite"
    assert cues.always_important(mail(subject="Project update")) is None


def test_marketing_is_never_must_see():
    assert R.must_see(mail(subject="(광고) 토익 시험 30% 할인")) is None
    assert R.must_see(mail(subject="Exam prep webinar: 50% off")) is None


def test_invite_word_in_body_does_not_count():
    assert cues.always_important(mail(body_text="You are invited to our webinar")) is None


def test_noreply_exam_notice_is_not_noise_on_day_one():
    """The measured failure: a Canvas notice from notifications@ scored R=0.08
    and was hidden. An exam is never hidden."""
    email = mail(subject="Midterm exam seat assignment",
                 from_address="notifications@instructure.com")
    axes = R.axes_for(email, model_bucket="fyi")
    assert axes.relevance < Thresholds().relevance  # still low relevance...
    assert axes.must_see == "exam"
    assert axes.bucket(Thresholds()) != "noise"     # ...but never hidden


def test_noreply_korean_assignment_gets_deadline_and_is_kept():
    email = mail(subject="과제 제출 마감 안내: 11월 3일까지",
                 from_address="notifications@instructure.com")
    assert priority.extract_deadline(email["subject"], TODAY) == "2026-11-03"


def test_structural_bucket_keeps_noreply_tuition_notice():
    email = mail(subject="등록금 납부 기한 안내", from_address="no-reply@univ.ac.kr")
    assert priority.structural_bucket(email) == "action"


def test_ordinary_noreply_is_still_noise():
    email = mail(subject="Your weekly activity", from_address="no-reply@service.com")
    assert R.axes_for(email).bucket(Thresholds()) == "noise"
    assert priority.structural_bucket(email) == "noise"


def test_reason_names_the_kind():
    email = mail(subject="Invitation: Lab meeting", from_address="calendar-notification@google.com")
    code, arg = R.reason_for(R.axes_for(email))
    assert (code, arg) == ("mustSee", "invite")
