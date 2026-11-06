"""#27: push_health must raise an exception on non-2xx (previously 401/404/500 = a silent "success",
the node stayed quiet, and the CP flipped it DOWN by staleness — masking exactly the agent_secret desync)."""
import httpx
import pytest

from agent import registration, run
from agent.config import AgentSettings

_S = AgentSettings(cp_base_url="https://cp.example")
_SECRET = "a" * 48


def test_push_health_raises_on_401():
    c = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, text="bad sig")))
    with pytest.raises(httpx.HTTPStatusError):
        registration.push_health(_S, node_id="N1", agent_secret=_SECRET, metrics={"peers": 0}, client=c)


def test_push_health_returns_status_on_ok():
    def handler(req):
        assert req.url.path.endswith("/v1/nodes/N1/health")
        return httpx.Response(200, json={"ok": True})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert (
        registration.push_health(_S, node_id="N1", agent_secret=_SECRET, metrics={"peers": 0}, client=c)
        == 200
    )


# --- transit packet loss (29.07): one lost handshake used to burn a whole health interval ---


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _push(client, sleeps, settings=_S) -> bool:
    return run._push_with_retries(
        settings,
        client,
        node_id="N1",
        agent_secret=_SECRET,
        metrics={"peers": 0},
        sleep=sleeps.append,
    )


def test_retries_transport_error_then_succeeds():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(204)

    sleeps = []
    assert _push(_client(handler), sleeps) is True
    assert len(calls) == 3
    assert sleeps == [_S.health_push_retry_seconds] * 2  # slept between attempts, not after the last


def test_gives_up_after_configured_attempts():
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("timed out")

    sleeps = []
    assert _push(_client(handler), sleeps) is False
    assert len(calls) == _S.health_push_attempts
    assert len(sleeps) == _S.health_push_attempts - 1


def test_non_2xx_is_not_retried():
    """A 401 is a CP that HEARD us (agent_secret desync, #27) — retrying only delays the warning."""
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(401, text="bad sig")

    sleeps = []
    assert _push(_client(handler), sleeps) is False
    assert len(calls) == 1
    assert sleeps == []


def test_keepalive_outlives_the_push_interval():
    """httpx defaults keepalive_expiry to 5s — below the 30s interval the connection would expire
    between pushes and every push would pay a fresh handshake again, which is the bug being fixed."""
    s = AgentSettings(cp_base_url="https://cp.example")
    limits = run._health_limits(s)
    assert limits.keepalive_expiry > s.health_interval_seconds
    assert limits.max_keepalive_connections >= 1
