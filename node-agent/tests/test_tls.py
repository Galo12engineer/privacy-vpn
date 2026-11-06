import os
import ssl

from agent import registration, tls
from agent.config import AgentSettings


def test_generate_self_signed_roundtrip():
    m = tls.generate_self_signed()
    assert "BEGIN CERTIFICATE" in m.cert_pem
    assert "BEGIN" in m.key_pem and "PRIVATE KEY" in m.key_pem
    # fingerprint = colon-hex SHA-256, 32 bytes → 95 characters
    assert len(m.fingerprint) == 95
    assert m.fingerprint == tls.fingerprint_pem(m.cert_pem)
    # the cert actually loads into an SSL context (it is valid)
    ctx = ssl.create_default_context(cadata=m.cert_pem)
    assert ctx is not None


def test_write_materials_tmpfs_perms(tmp_path):
    m = tls.generate_self_signed()
    cert_path, key_path = tls.write_materials(m, directory=str(tmp_path / "tls"))
    assert os.path.exists(cert_path) and os.path.exists(key_path)
    assert oct(os.stat(key_path).st_mode)[-3:] == "600"
    # cert+key are consistent: load_cert_chain does not fail
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert_path, key_path)


def test_registration_sends_tls_cert():
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"node_id": "n1", "agent_secret": "s"}

    class FakeClient:
        def post(self, url, json):
            captured["json"] = json
            return FakeResp()

        def close(self):
            pass

    registration.register(
        AgentSettings(bootstrap=False),
        public_key="PUB=",
        tls_cert_pem="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n",
        client=FakeClient(),
    )
    assert "agent_tls_cert" in captured["json"]
    assert "BEGIN CERTIFICATE" in captured["json"]["agent_tls_cert"]
