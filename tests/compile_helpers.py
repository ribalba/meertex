"""The compile service (compile/service.py) for the tests: started natively as
a subprocess on a free port, with this machine's TeX Live."""

import gzip
import io
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "compile" / "service.py"
SANDBOX = ROOT / "compile" / "sandbox.py"
TOKEN = "test-compile-token"


def landlock() -> int:
    """The version of this kernel's Landlock, 0 when it has none. The compile
    service boxes its jobs in with it (compile/sandbox.py)."""
    try:
        cmd = [sys.executable, "-I", str(SANDBOX), "--abi"]
        return int(subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


needs_sandbox = pytest.mark.skipif(landlock() < 1, reason="this kernel has no Landlock, so jobs run without the sandbox")

needs_tex = pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("synctex") is None,
    reason="latexmk (TeX Live) is not installed",
)
# Markdown main files. The compile image pins pandoc's version
# (compile/Dockerfile), and so does the CI.
needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc is not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    def __init__(self, url: str, jobs: Path, pid: int) -> None:
        self.url = url
        self.jobs = jobs
        self.pid = pid

    def log(self) -> str:
        """What the service has printed so far."""
        return (self.jobs.parent / "service.log").read_text()

    def job_count(self) -> int:
        """Jobs the service has made and still keeps."""
        return sum(1 for p in self.jobs.iterdir() if (p / "job.json").is_file()) if self.jobs.is_dir() else 0


@contextmanager
def compile_service(folder: Path, **env: object) -> Iterator[Service]:
    """Run the service until the block ends. ``env`` adds COMPILE_* settings."""
    folder.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    environ = {
        **os.environ,
        "COMPILE_TOKEN": TOKEN,
        "COMPILE_HOST": "127.0.0.1",
        "COMPILE_PORT": str(port),
        "COMPILE_JOBS_DIR": str(folder / "jobs"),
        **{k: str(v) for k, v in env.items()},
    }
    log_path = folder / "service.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen([sys.executable, "-I", str(SERVICE)], env=environ, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=1):
                    break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"The compile service did not start:\n{log_path.read_text()}") from None
                time.sleep(0.1)
        yield Service(url, folder / "jobs", proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def tgz(files: dict[str, bytes], extra: list[tarfile.TarInfo] | None = None) -> bytes:
    """A tar.gz of ``files``, plus hand-made members (links, devices) in ``extra``."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


HELLO = rb"""\documentclass{article}
\begin{document}
Hello world.

\input{sections/intro}

The end.
\end{document}
"""
INTRO = b"""Intro line one.

Intro line three.
"""
