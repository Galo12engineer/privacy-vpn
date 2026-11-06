import pytest

from app.services.reality_dest import DestError, check_dest, evaluate


def test_evaluate_ok():
    evaluate(tls_version="TLSv1.3", alpn="h2", server_header="nginx")  # does not raise


def test_evaluate_ok_without_server_header():
    evaluate(tls_version="TLSv1.3", alpn="h2", server_header=None)  # CDN detection is best-effort → skip


def test_evaluate_rejects_not_tls13():
    with pytest.raises(DestError):
        evaluate(tls_version="TLSv1.2", alpn="h2", server_header="nginx")
    with pytest.raises(DestError):
        evaluate(tls_version=None, alpn="h2", server_header="nginx")


def test_evaluate_rejects_no_h2():
    with pytest.raises(DestError):
        evaluate(tls_version="TLSv1.3", alpn="http/1.1", server_header="nginx")
    with pytest.raises(DestError):
        evaluate(tls_version="TLSv1.3", alpn=None, server_header="nginx")


@pytest.mark.parametrize(
    "hdr", ["cloudflare", "AkamaiGHost", "Fastly", "cloudfront", "ECAcc (nyd/D14A)"]
)
def test_evaluate_rejects_cdn(hdr):
    with pytest.raises(DestError):
        evaluate(tls_version="TLSv1.3", alpn="h2", server_header=hdr)


def test_check_dest_empty_no_network():
    # an empty dest is rejected before any network access
    with pytest.raises(DestError):
        check_dest("")
