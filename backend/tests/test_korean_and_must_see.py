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


# --------------------------------------------------------------------------
# round 2: the stored bucket, precision, and the muted-sender rule
# --------------------------------------------------------------------------

import pytest

from app import db, pipeline
from datetime import timedelta


def _row(i, subject, sender, body=""):
    return dict(id=i, conversation_id=None, subject=subject, from_name="", from_address=sender,
                to_recipients="[]", cc_recipients="[]", received_at=db.now_iso(),
                is_read=0, is_answered=0, is_flagged=0, has_attachments=0, importance="normal",
                web_link="", folder="INBOX", body_preview=body[:200], body_text=body,
                body_html="", synced_at=db.now_iso())


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


def _persist(rows, muted=()):
    db.upsert_emails(rows)
    for address in muted:
        db.mute_sender(address)
    classes = pipeline._structural_classifications([dict(r) for r in rows], "")
    pipeline._persist(classes, {r["id"]: r for r in rows}, "structural", "")
    with db.connect() as conn:
        return {r["email_id"]: dict(r) for r in conn.execute(
            "SELECT email_id, bucket, deadline, reason_code, reason_arg FROM classifications")}


def test_measured_canvas_notice_is_stored_outside_noise(store):
    """The 2026-10-31 failure end to end: Korean LMS notice from notifications@,
    no priorities, no learning. It must be stored with its date and not as noise."""
    due = date.today() + timedelta(days=3)
    subject = f"과제 제출 마감 안내: {due.month}월 {due.day}일까지"
    out = _persist([_row("k1", subject, "notifications@instructure.com",
                         f"CS101 과제 3 제출 기한은 {due.month}월 {due.day}일 23:59입니다.")])
    assert out["k1"]["deadline"] == due.isoformat()
    assert out["k1"]["bucket"] != "noise"


def test_muted_sender_is_not_rescued_by_the_floor(store):
    out = _persist([_row("m", "2학기 등록금 납부 안내", "billing@school.ac.kr")],
                   muted=["billing@school.ac.kr"])
    assert out["m"]["bucket"] == "noise"


def test_newsletter_mentioning_an_exam_stays_noise(store):
    out = _persist([_row("n", "학과 소식지 11월호", "no-reply@dept.ac.kr",
                         "이번 호: 기말고사 일정, 장학금 소식")])
    assert out["n"]["bucket"] == "noise"


def test_past_dated_noreply_notice_is_not_kept_by_the_date():
    old = mail(subject="과제 제출 마감 안내", from_address="notifications@lms.com")
    assert not R.dated_ask(old, (date.today() - timedelta(days=10)).isoformat())
    assert R.dated_ask(old, (date.today() + timedelta(days=1)).isoformat())


def test_body_words_alone_do_not_make_must_see():
    assert cues.always_important(mail(subject="Catching up", body_text="I'm overdue on laundry")) is None
    assert cues.always_important(mail(subject="근황", body_text="다음 주에 시험이 있어서요")) is None


def test_ad_marker_at_the_end_is_marketing():
    assert R.must_see(mail(subject="시험 대비 특강 안내 (광고)")) is None


def test_bare_invite_word_is_not_a_calendar_invite():
    assert cues.always_important(mail(subject="신제품 런칭 행사에 초대합니다")) is None
    assert cues.always_important(mail(subject="초대: 연구실 회의 @ 11월 5일 (목) 오후 3시")) == "invite"
    assert cues.always_important(mail(subject="회의 초대 - 졸업 논문 심사")) == "invite"


def test_completed_submission_is_not_a_request():
    email = mail(subject="과제 제출 완료되었습니다", body_text="제출이 정상적으로 처리되었습니다.")
    assert priority.structural_bucket(email) == "fyi"


def test_ordinary_korean_requests_are_action():
    assert priority.structural_bucket(mail(subject="초안 검토 부탁드립니다")) == "action"
    assert priority.structural_bucket(mail(subject="발표 자료", body_text="금요일 전까지 올려주세요")) == "action"


def test_more_korean_date_forms():
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("신청: 11. 20.(금) 까지", wed) == "2026-11-20"
    assert priority.extract_deadline("11/20까지 제출", wed) == "2026-11-20"
    assert priority.extract_deadline("내일 오전 10시까지 회신 바랍니다", wed) == "2026-11-05"
    assert priority.extract_deadline("11월 말까지 정리해 주세요", wed) == "2026-11-30"


def test_past_week_is_not_a_deadline():
    assert priority.extract_deadline("지난주 금요일까지 받았습니다", date(2026, 11, 4)) is None


def test_quoted_reply_dates_are_ignored():
    text = "네 알겠습니다.\n\n2026년 10월 2일 (금) 오후 3:00, 김교수 님이 작성:\n> 2026년 10월 9일까지 보내주세요"
    assert priority.extract_deadline(text, date(2026, 11, 4)) is None


# --------------------------------------------------------------------------
# round 3: marketing never rescued, stricter kinds and requests, reasons
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subject,sender", [
    ("(광 고) 중간고사 족보 판매", "marketing@shop.kr"),
    ("（광고）기말고사 대비 특강", "shop@x.kr"),
    ("【광고】 시험 대비반", "shop@x.kr"),
    ("<광고> 등록금 대출 안내", "shop@x.kr"),
    ("[AD] Final exam prep", "shop@x.com"),
    ("Exam week survival guide", "newsletter@campus.com"),
    ("Invitation: Free AI summit", "events@vendor.com"),
    ("Ace your final exam with us", "promotions@kaplan.com"),
])
def test_marketing_is_never_must_see_by_marker_or_sender(subject, sender):
    assert R.must_see(mail(subject=subject, from_address=sender)) is None


def test_marketing_sender_with_dated_ask_stays_noise(store):
    out = _persist([
        _row("p1", "Last chance to register by Nov 30", "marketing@vendor.com"),
        _row("p2", "마감 임박! 11/7(토)까지 신청하세요", "no-reply@academy.kr"),
        _row("p3", "Product update", "updates@saas.com",
             f"Please confirm your email by {(date.today() + timedelta(days=3)).isoformat()}."),
    ])
    assert {k: v["bucket"] for k, v in out.items()} == {"p1": "noise", "p2": "noise", "p3": "noise"}


def test_quiz_and_test_send_are_not_exams():
    assert cues.always_important(mail(subject="Quiz: which Pokemon are you?")) is None
    assert cues.always_important(mail(subject="[시험 발송] 템플릿 확인")) is None
    assert cues.always_important(mail(subject="CS101 Quiz 3 moved to Friday")) == "exam"


def test_settled_payment_is_not_must_see():
    assert cues.always_important(mail(subject="등록금 납부 완료 안내")) is None
    assert cues.always_important(mail(subject="Your exam results are available")) is None


@pytest.mark.parametrize("subject", [
    "요청하신 자료 보내드립니다", "회의록 공유 (회신 불필요)", "조기 마감되어 안내드립니다", "이메일 서명 변경",
])
def test_bare_request_words_are_not_action(subject):
    assert priority.structural_bucket(mail(subject=subject)) == "fyi"


def test_deadline_beside_its_word_beats_an_earlier_date():
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("11월 1일 공지한 과제는 11월 7일까지 제출", wed) == "2026-11-07"


def test_this_month_two_digit_year_and_after_deadline_word():
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("이번 달 20일까지 회신", wed) == "2026-11-20"
    assert priority.extract_deadline("신청: 26.11.20(금)까지", wed) == "2026-11-20"
    assert priority.extract_deadline("마감 11/20 23:59", wed) == "2026-11-20"
    assert priority.extract_deadline("macOS 10.15.7 지원 종료", wed) is None


def test_reasons_match_the_bucket(store):
    far = date.today() + timedelta(days=30)
    out = _persist([
        _row("far", f"과제 제출 마감 안내: {far.month}월 {far.day}일까지", "notifications@lms.com"),
        _row("mut", "기말고사 고사실 안내", "no-reply@school.ac.kr"),
    ], muted=["no-reply@school.ac.kr"])
    assert out["far"]["bucket"] != "noise" and out["far"]["reason_code"] != "noise"
    assert out["mut"]["bucket"] == "noise" and out["mut"]["reason_code"] != "mustSee"


# --------------------------------------------------------------------------
# round 4: promotions from no-reply senders, number-like dates, past dates
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subject,sender", [
    ("Exam season: get 3 months free", "no-reply@quizlet.com"),
    ("Invitation: exclusive launch event", "noreply@brand.com"),
    ("기말고사 대비 족보 무료 제공", "noreply@studyapp.com"),
    ("Cheap flights! E-ticket deals", "no-reply@travel.com"),
    ("[이벤트] 기말고사 응원 선물", "no-reply@shop.kr"),
    ("항공권 최저가 알림", "no-reply@air.kr"),
])
def test_promotions_from_noreply_senders_are_not_must_see(subject, sender):
    assert R.must_see(mail(subject=subject, from_address=sender)) is None


def test_noreply_must_see_needs_evidence():
    # School / LMS / calendar sender, or a booking reference in the body.
    assert R.must_see(mail(subject="기말고사 고사실 안내", from_address="no-reply@snu.ac.kr")) == "exam"
    assert R.must_see(mail(subject="Midterm grades", from_address="notifications@instructure.com")) == "exam"
    assert R.must_see(mail(subject="Invitation: Lab sync @ Tue",
                           from_address="calendar-notification@google.com")) == "invite"
    assert R.must_see(mail(subject="[대한항공] 전자 항공권 발행 안내", from_address="no-reply@koreanair.com",
                           body_text="예약번호: ABC123 / 편명 KE017")) == "ticket"
    assert R.must_see(mail(subject="[대한항공] 전자 항공권 발행 안내",
                           from_address="no-reply@koreanair.com")) is None


def test_numbers_after_deadline_words_are_not_dates():
    wed = date(2026, 11, 4)
    for text in ["Payment due 3.5 million won", "Version due 2.1", "기한 2.5배 연장"]:
        assert priority.extract_deadline(text, wed) is None, text


def test_past_tense_dates_are_not_deadlines():
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("1/3 (월) 진행된 회의록 공유", wed) is None
    assert priority.extract_deadline("2026년 10월 1일에 보낸 메일 확인 부탁드립니다", wed) is None


def test_more_admin_kinds_and_dated_asks():
    assert cues.always_important(mail(subject="KTX 승차권 발권 안내", from_address="info@letskorail.com")) == "ticket"
    assert cues.always_important(mail(subject="카드 결제 예정 금액 안내", from_address="card@bank.kr")) == "payment"
    assert cues.DATED_ASK_SUBJECT.search("2학기 장학금 신청 기간 안내")
    assert cues.DATED_ASK_SUBJECT.search("성적 이의신청 안내")


# --------------------------------------------------------------------------
# round 5: English noise bucket pinned to develop; more Korean admin forms
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subject", [
    "Q3 sales numbers - please review", "Deals pipeline review", "Weekly digest", "Spring sale: 20% off",
])
def test_english_noise_words_match_develop(subject):
    """The English lists are develop's, moved, not changed."""
    import re as _re
    develop = _re.compile(
        r"\b(unsubscribe|newsletter|webinar|sale|% ?off|deal|promo|digest|"
        r"weekly round[- ]?up|invoice|receipt|order (?:confirm|ship))\b", _re.I)
    assert bool(cues.EN_NOISE_SUBJECT.search(subject)) == bool(develop.search(subject))
    assert cues.EN_NOISE_SUBJECT.pattern == develop.pattern


def test_tuition_discount_notice_is_not_noise():
    email = mail(subject="Tuition discount application deadline Nov 20", from_address="bursar@uni.edu")
    assert priority.structural_bucket(email) == "action"
    assert R.axes_for(email).bucket(Thresholds()) != "noise"


def test_promos_with_kind_words_are_blocked_from_the_floor():
    assert R.must_see(mail(subject="Exam prep course: save 30% this week", from_address="no-reply@uni.edu")) is None
    assert cues.always_important(mail(subject="Tuition-free MBA info session")) is None
    assert cues.always_important(mail(subject="시험 삼아 보내봅니다")) is None
    assert cues.always_important(mail(subject="등록금 분납 안내", body_text="... 수신거부 ...")) is None


def test_lms_notices_without_deadline_words_are_kept(store):
    due = date.today() + timedelta(days=5)
    out = _persist([
        _row("a", f"과제 공지 ({due.month}/{due.day}까지)", "no-reply@lms.ac.kr"),
        _row("b", "새 과제가 등록되었습니다", "notifications@instructure.com",
             f"제출 기한: {due.month}월 {due.day}일"),
    ])
    assert out["a"]["bucket"] != "noise"
    assert out["b"]["bucket"] != "noise"


def test_iso_date_before_hangul_and_bare_day():
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("2026-11-20까지 제출", wed) == "2026-11-20"
    assert priority.extract_deadline("5일까지 회신 바랍니다", wed) == "2026-11-05"


def test_more_korean_requests():
    for subject in ["장학금 신청하세요", "설문 참여 부탁드립니다", "질의 답변 요청"]:
        assert priority.structural_bucket(mail(subject=subject)) == "action", subject


# --------------------------------------------------------------------------
# round 6: promo words never put real school mail into noise
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subject", [
    "등록금 납부 마감 임박 안내", "장학금 신청 마감 임박", "선착순 기숙사 신청 안내", "수강료 할인 안내",
])
def test_school_mail_with_promo_words_is_not_noise(subject):
    email = mail(subject=subject, from_address="bursar@uni.ac.kr")
    assert R.structural_noise(email) == 0.0
    assert R.axes_for(email).bucket(Thresholds()) != "noise"


def test_promo_words_still_block_the_floor():
    assert R.must_see(mail(subject="기말고사 족보 할인", from_address="no-reply@uni.ac.kr")) is None


def test_day_spans_are_not_dates():
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("최대 5일까지 연장 가능", wed) is None
    assert priority.extract_deadline("총 3일까지 사용할 수 있습니다", wed) is None


def test_slash_year_and_trailing_deadline_word():
    wed = date(2026, 11, 4)
    assert priority.extract_deadline("2026/11/20까지 제출", wed) == "2026-11-20"
    assert priority.extract_deadline("보고서 11/20 마감", wed) == "2026-11-20"


def test_friend_mentioning_an_exam_gets_no_exam_label():
    code, _ = R.reason_for(R.axes_for(mail(subject="기말고사 끝나고 회식?", from_address="friend@gmail.com")))
    assert code != "mustSee"
