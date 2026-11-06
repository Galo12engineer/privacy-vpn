from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

from app.models import Account
from app.models.enums import AccountStatus
from app.scheduler.reminders import send_due_reminders
from app.services.account_email import set_email
from app.services.billing import apply_credit
from app.services.emailer import RecordingEmailer

KEY = Fernet.generate_key().decode()
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _account(session, suffix, expires_at, *, with_email=True, opt_in=True) -> Account:
    a = Account(
        account_number_hash=f"rm{suffix}".ljust(64, "0"),
        status=AccountStatus.ACTIVE,
        expires_at=expires_at,
    )
    session.add(a)
    session.commit()
    if with_email:
        set_email(session, a, f"u{suffix}@e.com", opt_in=opt_in, key=KEY)
        session.commit()
    return a


def test_3day_reminder_sent_once(session):
    a = _account(session, "1", NOW + timedelta(days=2))  # ≤3 days, >1 day
    em = RecordingEmailer()
    assert send_due_reminders(session, emailer=em, now=NOW, key=KEY) == 1
    assert em.sent[0]["to"] == "u1@e.com"
    assert a.reminder_3d_sent_at is not None
    assert a.reminder_1d_sent_at is None
    # a repeat run does not duplicate
    assert send_due_reminders(session, emailer=em, now=NOW, key=KEY) == 0


def test_1day_reminder_sent(session):
    a = _account(session, "2", NOW + timedelta(hours=12))  # ≤1 day
    a.reminder_3d_sent_at = NOW - timedelta(days=1)  # 3d was already sent
    session.commit()
    em = RecordingEmailer()
    assert send_due_reminders(session, emailer=em, now=NOW, key=KEY) == 1
    assert a.reminder_1d_sent_at is not None


def test_short_plan_does_not_send_both_reminders(session):
    # regression: a plan shorter than 3 days is, at purchase time, already past the threshold of both
    # windows → previously it sent the 3d AND the 1d emails at once. Now the windows are mutually
    # exclusive: for a <1d plan only the 1-day one goes out.
    a = _account(session, "s", NOW + timedelta(hours=12))  # neither 3d nor 1d has been sent yet
    em = RecordingEmailer()
    assert send_due_reminders(session, emailer=em, now=NOW, key=KEY) == 1
    assert a.reminder_3d_sent_at is None  # the 3-day one did NOT go out (already in the 1-day window)
    assert a.reminder_1d_sent_at is not None
    assert len(em.sent) == 1


def test_no_email_no_reminder(session):
    _account(session, "3", NOW + timedelta(days=2), with_email=False)
    em = RecordingEmailer()
    assert send_due_reminders(session, emailer=em, now=NOW, key=KEY) == 0


def test_opt_out_no_reminder(session):
    _account(session, "4", NOW + timedelta(days=2), opt_in=False)
    em = RecordingEmailer()
    assert send_due_reminders(session, emailer=em, now=NOW, key=KEY) == 0


class _FlakyEmailer:
    """Raises on one recipient, sends to the rest (checks per-account isolation)."""

    def __init__(self, fail_to: str) -> None:
        self.fail_to = fail_to
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        if to == self.fail_to:
            raise RuntimeError("smtp reject")
        self.sent.append({"to": to})


def test_one_bad_account_does_not_abort_batch(session):
    bad = _account(session, "b", NOW + timedelta(days=2))
    good = _account(session, "g", NOW + timedelta(days=2))
    em = _FlakyEmailer(fail_to="ub@e.com")
    # without isolation the RuntimeError would abort the whole batch; with isolation the bad one is
    # skipped and the good one is delivered
    sent = send_due_reminders(session, emailer=em, now=NOW, key=KEY)
    assert sent == 1
    assert em.sent == [{"to": "ug@e.com"}]
    session.refresh(bad)
    session.refresh(good)
    assert bad.reminder_3d_sent_at is None  # marker not set → a retry is possible
    assert good.reminder_3d_sent_at is not None


def test_topup_resets_reminders(session):
    a = _account(session, "5", NOW + timedelta(days=2))
    em = RecordingEmailer()
    send_due_reminders(session, emailer=em, now=NOW, key=KEY)
    assert a.reminder_3d_sent_at is not None
    apply_credit(session, a, days=30, now=NOW)  # extending the plan resets the reminders
    assert a.reminder_3d_sent_at is None
    assert a.reminder_1d_sent_at is None
