"""meertex-compile: the box a job runs in (docs/DESIGN.md §14).

    python3 -I sandbox.py [-r PATH]... [-w PATH]... [-x PATH]... -- COMMAND [ARGUMENT]...

runs COMMAND, and everything it starts, where it reaches only the files it
was given. ``-r`` allows reading and running what is below PATH, ``-w``
reading and writing, ``-x`` all three. Everything else on the machine is
closed. Opening it fails with "Permission denied". The command cannot open a
TCP connection either. ``python3 -I sandbox.py --abi`` prints the version of
the kernel's Landlock, 0 when it has none.

Why this exists. kpathsea's paranoid mode keeps TeX below its folder, but the
Lua of LuaTeX gets around it. ``io.input`` and ``io.output`` open any path,
and ``kpse.lookup`` finds files anywhere. A lualatex document could read the
container's environment in /proc and every other paper's files. So the rule
is not left to the programs. Landlock is the kernel's. A process gives up
access for itself and for everything it starts, for good, and needs no
privilege to do so. The rules are on the files themselves, so a link, a
``..`` or another working directory lead nowhere new.

This is a file of its own because it runs before every job. It needs only
ctypes, and starts in a fraction of the time compile/service.py would.
"""

import ctypes
import os
import stat
import sys

# What the service's own guards print. The server lists such lines as errors.
PREFIX = "meertex-compile: "

# linux/landlock.h and the system call numbers, which are the same on every
# architecture.
SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # since version 2: rename or link into another folder
FS_TRUNCATE = 1 << 14  # since version 3
FS_IOCTL_DEV = 1 << 15  # since version 5
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1  # since version 4
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1  # since version 6

# Everything version 1 knows. What a rule does not allow is refused.
FS_VERSION_1 = (1 << 13) - 1
# The only rights a rule on a single file may hold.
FS_OF_A_FILE = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV

READ = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
# No links (FS_MAKE_SYM), so nothing the service reads after a job can point
# out of the job's folders, and no sockets, pipes or device files.
WRITE = (
    FS_READ_FILE | FS_READ_DIR | FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE
    | FS_MAKE_DIR | FS_MAKE_REG | FS_REFER | FS_TRUNCATE
)  # fmt: skip
# Shell scripts write to /dev/null all the time, Perl seeds itself from
# /dev/urandom.
DEVICES = {
    "/dev/null": FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE | FS_IOCTL_DEV,
    "/dev/zero": FS_READ_FILE,
    "/dev/random": FS_READ_FILE,
    "/dev/urandom": FS_READ_FILE,
}


class RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class PathBeneathAttr(ctypes.Structure):
    # The kernel's struct is packed and ends after parent_fd, at 12 bytes.
    # This one has four bytes of padding behind it, which the kernel never
    # reads. Both fields are where it expects them.
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _syscall(number: int, *args: object) -> int:
    result = _libc().syscall(ctypes.c_long(number), *args)
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def abi() -> int:
    """The version of the kernel's Landlock. 0 when the kernel has none, it is
    switched off, or this is not Linux."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        return max(0, _syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION)))
    except (OSError, AttributeError):
        return 0


def _allow(ruleset: int, path: str, rights: int, required: bool) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        if required:
            raise
        return  # a place this machine does not have
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            rights &= FS_OF_A_FILE
        if rights:
            attr = PathBeneathAttr(allowed_access=rights, parent_fd=fd)
            _syscall(SYS_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(RULE_PATH_BENEATH), ctypes.byref(attr), ctypes.c_uint32(0))
    finally:
        os.close(fd)


def restrict(read: list[str], write: list[str], execute: list[str]) -> None:
    """Close everything to this process and what it starts, but what is below
    the given paths. Raises OSError when the kernel cannot do it. A folder to
    write to has to be there, one to read from may be missing."""
    version = abi()
    if version < 1:
        raise OSError("this kernel has no Landlock")
    handled = FS_VERSION_1
    if version >= 2:
        handled |= FS_REFER
    if version >= 3:
        handled |= FS_TRUNCATE
    if version >= 5:
        handled |= FS_IOCTL_DEV
    attr = RulesetAttr(handled_access_fs=handled)
    size = ctypes.sizeof(ctypes.c_uint64)
    if version >= 4:
        # No rule allows a port, so no TCP at all.
        attr.handled_access_net = NET_BIND_TCP | NET_CONNECT_TCP
        size = 2 * ctypes.sizeof(ctypes.c_uint64)
    if version >= 6:
        # No signals to, and no abstract sockets of, processes outside.
        attr.scoped = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL
        size = ctypes.sizeof(RulesetAttr)
    ruleset = _syscall(SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0))
    try:
        for path, rights in DEVICES.items():
            _allow(ruleset, path, rights & handled, required=False)
        for path in read:
            _allow(ruleset, path, READ & handled, required=False)
        for path in write:
            _allow(ruleset, path, WRITE & handled, required=True)
        for path in execute:
            _allow(ruleset, path, (WRITE | FS_EXECUTE) & handled, required=True)
        if _libc().prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        _syscall(SYS_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0))
    finally:
        os.close(ruleset)


def main(argv: list[str]) -> int:
    if argv == ["--abi"]:
        print(abi())
        return 0
    paths: dict[str, list[str]] = {"-r": [], "-w": [], "-x": []}
    rest = list(argv)
    while rest and rest[0] != "--":
        flag = rest.pop(0)
        if flag not in paths or not rest or rest[0] == "--":
            print(f"{PREFIX}sandbox.py takes -r, -w and -x with a path each, then -- and a command.", file=sys.stderr)
            return 2
        paths[flag].append(rest.pop(0))
    command = rest[1:]
    if not command:
        print(f"{PREFIX}sandbox.py was given no command.", file=sys.stderr)
        return 2
    try:
        restrict(paths["-r"], paths["-w"], paths["-x"])
    except OSError as e:
        # Never run a job outside the box it was meant for.
        print(f"{PREFIX}the sandbox could not be set up ({e}), so nothing ran.", file=sys.stderr)
        return 126
    try:
        os.execvp(command[0], command)
    except OSError as e:
        print(f"{PREFIX}{command[0]} could not be started ({e}).", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
