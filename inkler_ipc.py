"""Private, atomic local IPC. No sockets, downloads, or third-party imports."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

MAX_BYTES = 8 * 1024 * 1024


def state_dir():
    return Path(os.environ.get("INKLER_STATE_DIR", Path.home() / ".cache/inkler"))


def private_dir(path):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("Inkler directory must be a real directory, not a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Inkler directory must be owned by you and not a symlink")
    path.chmod(0o700)
    return path


def read_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Inkler IPC file must be a regular file owned by you")
        if info.st_mode & 0o077:
            raise ValueError("Inkler IPC file must be private (mode 600)")
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Inkler request/result exceeds 8 MiB")
        return data


def read_json(path):
    return json.loads(read_bytes(path))


def atomic_write(path, data):
    path = Path(path)
    private_dir(path.parent)
    if isinstance(data, str):
        data = data.encode()
    if len(data) > MAX_BYTES:
        raise ValueError("Inkler request/result exceeds 8 MiB")
    tmp = path.parent / (".tmp-" + uuid.uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, allow_nan=False))


@contextmanager
def locked(path):
    private_dir(Path(path).parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe Inkler lock file")
        # Fail promptly instead of blocking a second agent indefinitely.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
