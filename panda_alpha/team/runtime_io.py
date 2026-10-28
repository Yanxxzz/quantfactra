"""宿主共用的原子文件、租约和进程出生身份工具。

PID 可复用，因此恢复和终止都核验出生身份；不按裸 PID 猜测归属。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid

import psutil

from .contracts import canonical


def read(path, default=None):
    path = Path(path)
    for attempt in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(.02)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers briefly hold a handle that denies delete/replace sharing.
    # Keep the old complete JSON visible and retry the atomic rename only.
    for attempt in range(50):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(.02)


@contextmanager
def lease(path):
    """OS-released exclusive lease; never infer ownership from a stale PID file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Runtime lease already held: {path.name}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def identity(pid=None):
    process = psutil.Process(pid)
    return {"pid": process.pid, "created": process.create_time()}


def owned(receipt):
    """Birth time protects unrelated processes after PID reuse."""
    if not receipt:
        return None
    try:
        process = psutil.Process(receipt["pid"])
        if process.create_time() == receipt["created"] and process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except (psutil.NoSuchProcess, KeyError):
        pass
    return None


def terminate(receipt):
    process = owned(receipt)
    if process is None:
        return
    try:
        children = process.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        process.terminate()
        _, alive = psutil.wait_procs(children + [process], timeout=2)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass


def active(root, manifest):
    control = read(Path(root) / "control.json", {})
    if control.get("run") is not True or time.time() >= manifest["deadline_epoch"]:
        return False
    if time.time() - control.get("heartbeat", 0) < manifest["workers"]["orphan_timeout"]:
        return True
    # A machine/VM scheduling pause ages every heartbeat together. Do not kill
    # healthy workers and exhaust restart budgets while their original owner
    # is still alive. PID birth identity, the stop flag and deadline still bind.
    supervisor = read(Path(root) / "supervisor.json", {})
    return supervisor.get("status") == "running" and owned(supervisor.get("process")) is not None
