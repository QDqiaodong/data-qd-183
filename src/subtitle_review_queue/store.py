"""账本持久化：排他/共享文件锁、完整快照读取与原子提交。

账本就是一个 JSON 文件。所有变更都在同一进程内构造完整的新状态，先写
同目录临时文件并 fsync，再用 ``os.replace`` 原子替换。进程在替换前退出
只会留下无人引用的临时文件，账本仍是旧快照；替换成功后变更即已提交，
与后续报告写入无关。跨进程互斥由账本旁的 ``<state>.lock`` 哨兵上的
``flock`` 提供，读取只拿共享锁。
"""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

MODE = "subtitle-review-queue"
SCHEMA_VERSION = 1


class MissingLedger(ValueError):
    """账本文件尚不存在。"""


def canonical(value):
    """键序无关的规范 JSON，用于比较请求身份。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def lock_path(path):
    target = Path(path)
    return target.with_name(target.name + ".lock")


@contextmanager
def locked(path, exclusive=True):
    """在账本旁哨兵文件上加锁；不读取、不修改账本内容。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path(target), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def read(path):
    """读取一份完整账本快照；调用方负责持有共享或排他锁。"""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        raise MissingLedger(f"state ledger does not exist: {target}")
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"state ledger is not valid JSON: {exc}")
    if not isinstance(state, dict):
        raise ValueError("state ledger root must be an object")
    if state.get("schema_version") != SCHEMA_VERSION \
            or state.get("mode") != MODE:
        raise ValueError("state ledger has an unsupported format")
    return state


def write(path, state):
    """原子提交完整状态；返回时替换已完成。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(state, ensure_ascii=False, allow_nan=False,
                          indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    # 目录项也要落盘，提交后的快照在进程退出后仍可被其他进程看到。
    try:
        directory = os.open(target.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)
