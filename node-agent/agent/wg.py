"""WireGuard management via `wg`/`ip`. The runner is injected → unit-testable without root."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence

CompletedProcess = subprocess.CompletedProcess

Runner = Callable[[Sequence[str]], CompletedProcess]


def default_runner(argv: Sequence[str]) -> CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=True)


def tool_for_protocol(protocol: str | None) -> str:
    """Peer management CLI tool: awg for AmneziaWG, wg for regular WireGuard.
    amneziawg-tools is a fork of wireguard-tools, the set/show subcommands match exactly."""
    return "awg" if protocol == "amneziawg" else "wg"


class WireGuard:
    def __init__(self, interface: str, *, tool: str = "wg", runner: Runner = default_runner):
        self.interface = interface
        self.tool = tool  # "wg" or "awg" (see tool_for_protocol)
        self.runner = runner

    def add_peer(
        self, public_key: str, allowed_ips: Sequence[str], preshared_key: str | None = None
    ) -> CompletedProcess:
        argv = [
            self.tool, "set", self.interface,
            "peer", public_key,
            "allowed-ips", ",".join(allowed_ips),
        ]
        if preshared_key:
            # PSK via a 0600 file, so as not to expose it in argv/ps
            fd, path = tempfile.mkstemp()
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(preshared_key)
                os.chmod(path, 0o600)
                return self.runner(argv + ["preshared-key", path])
            finally:
                os.unlink(path)
        return self.runner(argv)

    def remove_peer(self, public_key: str) -> CompletedProcess:
        return self.runner([self.tool, "set", self.interface, "peer", public_key, "remove"])

    def dump(self) -> CompletedProcess:
        return self.runner([self.tool, "show", self.interface, "dump"])

    def peer_count(self) -> int:
        lines = self.dump().stdout.strip().splitlines()
        return max(len(lines) - 1, 0)  # the first line is the interface itself

    def transfer_totals(self) -> tuple[int, int]:
        """Aggregate rx/tx for the interface (NOT per-user). Sum only."""
        rx = tx = 0
        for line in self.dump().stdout.strip().splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) >= 7:
                rx += int(cols[5])
                tx += int(cols[6])
        return rx, tx
