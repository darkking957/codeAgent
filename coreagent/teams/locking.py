"""T2｜文件锁原语（认领防双取 / 超时重试 / 过期夺取）。

基于**锁文件独占创建**（``O_CREAT | O_EXCL``）的进程间互斥：抢到锁文件者持锁，释放即删。供 T4
任务认领、T5 邮箱写入复用，保证并发下对同一目标文件的读改写串行化（spec 非功能「无竞态」）。

三条语义（阈值钉 constants / checklist）：
- **重试**：拿不到锁按固定间隔（50ms）重试，到总等待上限（2s）仍失败 → 抛 ``LockTimeout``。
- **过期夺取**：锁文件 mtime 超过陈旧阈值（30s）视为持有者已死，直接删旧锁重抢（防死锁）。
- **原子写配套**：``write_json_atomic`` 用同目录临时文件 + ``os.replace``（仿 #0002 / memory 落盘范式），
  与锁配合即「持锁 → 读 → 改 → 原子写 → 释放锁」。

锁文件命名 ``{target}.lock``，与目标文件同目录（同一文件系统，保证 replace 原子）。
"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from coreagent.teams import constants

logger = logging.getLogger(__name__)


class LockTimeout(TimeoutError):
    """在总等待上限内未能取得锁（持有者既未释放、锁也未过期）。"""


def _lock_path(target: Path) -> Path:
    return target.with_name(target.name + constants.LOCK_SUFFIX)


def _try_create(lock: Path) -> bool:
    """尝试独占创建锁文件；成功 True，已存在 False。写入持有者 pid 便于诊断。"""
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _is_stale(lock: Path, stale_seconds: float) -> bool:
    """锁文件 mtime 超过陈旧阈值 → 视为过期（持有者已死）。读 mtime 失败按未过期处理（不误删）。"""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    return age > stale_seconds


@contextmanager
def file_lock(
    target: Path | str,
    *,
    timeout: float = constants.LOCK_TIMEOUT,
    retry_interval: float = constants.LOCK_RETRY_INTERVAL,
    stale_seconds: float = constants.LOCK_STALE_SECONDS,
):
    """对 ``target`` 加文件锁的上下文管理器；``with file_lock(path): ...`` 期间独占。

    拿不到锁时按 ``retry_interval`` 重试至 ``timeout`` 上限；其间若锁文件陈旧（mtime 超
    ``stale_seconds``）则夺取。超上限仍失败抛 ``LockTimeout``。退出时删自己的锁文件（幂等）。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(target)
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        if _try_create(lock):
            acquired = True
            break
        # 锁被占：陈旧则夺取，否则到点重试 / 超时放弃。
        if _is_stale(lock, stale_seconds):
            try:
                lock.unlink()
            except OSError:
                pass            # 竞争者已删 / 已被别人夺取 → 下一轮重抢
            continue
        if time.monotonic() >= deadline:
            raise LockTimeout(f"取锁超时（>{timeout}s）：{lock}")
        time.sleep(retry_interval)
    try:
        yield
    finally:
        if acquired:
            try:
                lock.unlink()
            except OSError:
                pass


def write_json_atomic(path: Path | str, data) -> None:
    """原子写 JSON（同目录临时文件 + os.replace；仿 #0002 / memory 落盘范式）。

    与 ``file_lock`` 配合即「持锁 → 改 → 原子写」；单独使用也保证读者永不见半截文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path | str, default=None):
    """读 JSON；文件缺失 / 解析失败 → 返回 ``default``（不抛，容忍并发半截写已被 replace 规避）。"""
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default
