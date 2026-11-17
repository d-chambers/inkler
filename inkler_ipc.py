"""Private, atomic local IPC. No sockets, downloads, or third-party imports."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import time
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


def too_large(limit):
    return ValueError(f"Inkler file exceeds {limit // (1024 * 1024)} MiB")


def read_bytes(path, limit=MAX_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Inkler IPC file must be a regular file owned by you")
        if info.st_mode & 0o077:
            raise ValueError("Inkler IPC file must be private (mode 600)")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise too_large(limit)
        return data


def read_json(path):
    return json.loads(read_bytes(path))


def atomic_write(path, data, limit=MAX_BYTES):
    """Write privately and atomically. limit=None is for local render inputs, not messages."""
    path = Path(path)
    private_dir(path.parent)
    if isinstance(data, str):
        data = data.encode()
    if limit is not None and len(data) > limit:
        raise too_large(limit)
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
def locked(path, wait=0):
    """Hold an exclusive cross-process lock, waiting up to `wait` seconds for it."""
    private_dir(Path(path).parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe Inkler lock file")
        # Poll with a deadline instead of blocking a second agent indefinitely.
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Another Inkler operation is in progress; try again when it finishes") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)
