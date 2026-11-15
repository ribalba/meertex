"""The box a compile job runs in (compile/sandbox.py, docs/DESIGN.md §14), on
its own and without TeX. What is open to a command inside it, and what is not."""

import os
import socket
import subprocess
import sys

import pytest
from compile_helpers import SANDBOX, landlock, needs_sandbox

# Where sh, cat and the like are, with their libraries, and Python itself.
SYSTEM = [
    path
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", sys.prefix, sys.base_prefix)
    if os.path.exists(path)
]


def _boxed(command: list[str], read=(), write=(), execute=(), **kwargs) -> subprocess.CompletedProcess:
    flags: list[str] = []
    for flag, paths in (("-r", [*SYSTEM, *read]), ("-w", write), ("-x", execute)):
        for path in paths:
            flags += [flag, str(path)]
    cmd = [sys.executable, "-I", str(SANDBOX), *flags, "--", *command]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False, **kwargs)


def _sh(script: str, **box) -> subprocess.CompletedProcess:
    return _boxed(["sh", "-c", script], **box)


def test_says_which_landlock_the_kernel_has():
    out = subprocess.run([sys.executable, "-I", str(SANDBOX), "--abi"], capture_output=True, text=True, timeout=30, check=True)
    assert int(out.stdout) == landlock() >= 0
    assert out.stderr == ""


@needs_sandbox
def test_only_what_was_given_is_open(tmp_path):
    inside, shown, outside = tmp_path / "inside", tmp_path / "shown", tmp_path / "outside"
    for folder in (inside, shown, outside):
        folder.mkdir()
        (folder / "file.txt").write_text(f"{folder.name}-text")

    out = _sh(f"cat {inside}/file.txt {shown}/file.txt && echo new > {inside}/new.txt", read=[shown], write=[inside])
    assert (out.returncode, out.stdout, out.stderr) == (0, "inside-textshown-text", "")
    assert (inside / "new.txt").read_text() == "new\n"

    # Reading, listing, writing and removing what was not given. A folder
    # that may be read stays as it is.
    for script in (
        f"cat {outside}/file.txt",
        f"ls {outside}",
        f"echo x > {outside}/planted.txt",
        f"echo x >> {outside}/file.txt",
        f"rm {outside}/file.txt",
        f"echo x > {shown}/planted.txt",
        f"rm {shown}/file.txt",
        f"mv {inside}/file.txt {outside}/moved.txt",
    ):
        out = _sh(script, read=[shown], write=[inside])
        assert out.returncode != 0, script
        assert "Permission denied" in out.stderr or "not permitted" in out.stderr, (script, out.stderr)
        assert "outside-text" not in out.stdout
    assert sorted(p.name for p in outside.iterdir()) == ["file.txt"]
    assert (outside / "file.txt").read_text() == "outside-text"
    assert sorted(p.name for p in shown.iterdir()) == ["file.txt"]

    # Neither another working directory, nor "..", nor a link leads out. The
    # link is one the box could not have made itself.
    (inside / "link").symlink_to(outside / "file.txt")
    for script in (f"cd {outside} && cat file.txt", f"cat {inside}/../outside/file.txt", f"cat {inside}/link"):
        out = _sh(script, write=[inside])
        assert out.returncode != 0 and "outside-text" not in out.stdout, script
    assert _sh(f"ln -s {outside}/file.txt {inside}/made", write=[inside]).returncode != 0
    assert not (inside / "made").is_symlink()


@needs_sandbox
def test_the_environment_of_other_processes_is_closed():
    """/proc is not given to anybody. That is where the service's and the
    container's environment can be read."""
    env = {**os.environ, "MEERTEX_SECRET": "in-the-environment"}
    for path in ("/proc/self/environ", f"/proc/{os.getpid()}/environ", "/proc/1/environ"):
        out = _sh(f"cat {path}", env=env)
        assert out.returncode != 0, path
        assert "in-the-environment" not in out.stdout


@needs_sandbox
def test_programs_run_only_from_where_they_may(tmp_path):
    plain, home = tmp_path / "plain", tmp_path / "home"
    for folder in (plain, home):
        folder.mkdir()
        (folder / "program").write_text("#!/bin/sh\necho ran\n")
        (folder / "program").chmod(0o755)
    box = {"write": [plain], "execute": [home]}
    assert _boxed([str(home / "program")], **box).stdout == "ran\n"
    out = _boxed([str(plain / "program")], **box)
    assert out.returncode == 127
    assert out.stderr.startswith("meertex-compile: ") and "could not be started" in out.stderr


@needs_sandbox
@pytest.mark.skipif(landlock() < 4, reason="Landlock closes TCP since its version 4 (Linux 6.7)")
def test_no_tcp():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        connect = f"import socket; socket.create_connection(('127.0.0.1', {server.getsockname()[1]}), 5); print('connected')"
        free = subprocess.run([sys.executable, "-c", connect], capture_output=True, text=True, timeout=30, check=False)
        assert free.stdout == "connected\n"
        out = _boxed([sys.executable, "-c", connect])
        assert out.returncode != 0
        assert "PermissionError" in out.stderr


def test_refuses_instead_of_running_unboxed(tmp_path):
    marker = tmp_path / "ran"
    # A folder to write to that is not there, or a kernel without Landlock.
    out = _boxed(["touch", str(marker)], write=[tmp_path / "missing"])
    assert out.returncode == 126
    assert out.stderr.startswith("meertex-compile: the sandbox could not be set up")
    assert not marker.exists()

    for args in (["-r"], ["-q", "/usr", "--", "true"], ["-r", "/usr"], ["-r", "/usr", "--"]):
        out = subprocess.run([sys.executable, "-I", str(SANDBOX), *args], capture_output=True, text=True, timeout=30, check=False)
        assert out.returncode == 2, args
        assert out.stderr.startswith("meertex-compile: ")


@needs_sandbox
def test_a_missing_program_and_its_exit_code(tmp_path):
    assert _boxed(["no-such-program-anywhere"]).returncode == 127
    assert _sh("exit 7").returncode == 7
    # A folder to read from that this machine does not have is skipped.
    assert _sh("echo fine", read=[tmp_path / "missing"]).stdout == "fine\n"
