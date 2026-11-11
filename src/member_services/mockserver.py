"""Start and stop the supplied mock API from PACK_DIR (used by integration tests and the eval harness).

The mock keeps new grievances in memory, so each eval run starts a fresh process to get a known state.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def run_mock(pack_dir: Path, port: int | None = None, timeout_s: float = 10.0):
    """Yield the base URL of a freshly started mock; always stop it afterwards."""
    server = pack_dir / "mock_services" / "server.py"
    if not server.is_file():
        raise FileNotFoundError(f"mock server not found at {server} (check PACK_DIR)")
    port = port or free_port()
    proc = subprocess.Popen([sys.executable, "-I", str(server), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("mock server did not start")
            time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
