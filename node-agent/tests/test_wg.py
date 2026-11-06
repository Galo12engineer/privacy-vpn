import subprocess

from agent.wg import WireGuard


class RecordingRunner:
    def __init__(self, stdout: str = ""):
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, argv):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr="")


def test_add_peer_builds_wg_set():
    r = RecordingRunner()
    WireGuard("wg0", runner=r).add_peer("PUBKEY1", ["10.7.0.5/32", "fd00:7::5/128"])
    assert r.calls == [
        ["wg", "set", "wg0", "peer", "PUBKEY1", "allowed-ips", "10.7.0.5/32,fd00:7::5/128"]
    ]


def test_remove_peer_builds_remove():
    r = RecordingRunner()
    WireGuard("wg0", runner=r).remove_peer("PUBKEY1")
    assert r.calls == [["wg", "set", "wg0", "peer", "PUBKEY1", "remove"]]


def test_add_peer_with_psk_uses_file_not_argv():
    r = RecordingRunner()
    WireGuard("wg0", runner=r).add_peer("PUB", ["10.7.0.5/32"], preshared_key="SECRETPSK")
    argv = r.calls[0]
    assert "preshared-key" in argv
    # the PSK itself must not end up in argv (only the path to the file)
    assert "SECRETPSK" not in argv


def test_peer_count_and_transfer_totals():
    dump = (
        "PRIVKEY\tPUBKEY\t51820\toff\n"
        "PEER1\t(none)\t1.2.3.4:1\t10.7.0.2/32\t0\t100\t200\toff\n"
        "PEER2\t(none)\t1.2.3.5:1\t10.7.0.3/32\t0\t300\t400\toff\n"
    )
    wg = WireGuard("wg0", runner=RecordingRunner(stdout=dump))
    assert wg.peer_count() == 2
    assert wg.transfer_totals() == (400, 600)  # sum of rx/tx, aggregate
