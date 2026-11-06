from app.models import Account
from app.services import attribution


def _account(session):
    a = Account(account_number_hash="att".ljust(64, "0"), attribution_click_id="click-123")
    session.add(a)
    session.commit()
    return a


def test_postback_fired_once(session):
    a = _account(session)
    calls = []

    def sender(url, params):
        calls.append((url, params))
        return 200

    fired = attribution.maybe_fire(session, a, base_url="https://track.example/postback", sender=sender)
    assert fired is True
    assert a.attribution_fired is True
    assert calls[0][1] == {"subid": "click-123", "status": "sale"}

    # repeat — not duplicated
    again = attribution.maybe_fire(session, a, base_url="https://track.example/postback", sender=sender)
    assert again is False
    assert len(calls) == 1


def test_no_click_id_no_postback(session):
    a = Account(account_number_hash="no".ljust(64, "0"))
    session.add(a)
    session.commit()
    assert attribution.maybe_fire(session, a, base_url="https://x", sender=lambda u, p: 200) is False
