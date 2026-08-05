"""Запуск и остановка внешних процессов.

Единственный модуль, который знает о разнице между macOS и Windows.
Разница существенная: убить только сам процесс мало — утилита может
породить потомков, и они переживут родителя, продолжая держать ресурсы.

  macOS/Linux: start_new_session=True делает процесс лидером новой группы,
               после чего вся группа убивается одним os.killpg.
  Windows:     CREATE_NEW_PROCESS_GROUP + taskkill /T /F, который сам
               обходит дерево потомков.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import IO, Any, Dict, List, Optional, Sequence

IS_WINDOWS = os.name == "nt"

# Значения на случай, если константы недоступны (не-Windows сборки stdlib).
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def popen_kwargs() -> Dict[str, Any]:
    """Платформенные аргументы Popen для управляемого дочернего процесса."""
    if IS_WINDOWS:
        # CREATE_NO_WINDOW — иначе при 16 параллельных запусках на экран
        # полезут 16 консольных окон.
        return {"creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn(
    argv: Sequence[str],
    cwd: Optional[str] = None,
    stdout: Optional[IO[bytes]] = None,
    env: Optional[Dict[str, str]] = None,
) -> "subprocess.Popen[bytes]":
    """Запустить утилиту.

    Вывод уходит прямо в файловый дескриптор, а не в pipe: за десять минут
    утилита может выдать много, и держать это в памяти незачем. Заодно
    снимается вопрос кодировки — байты пишутся как есть.

    stdin закрыт: если утилита вдруг решит что-то спросить, она получит EOF
    и завершится, а не зависнет насмерть в ожидании ввода, которого не будет.
    """
    return subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=stdout,
        stderr=subprocess.STDOUT if stdout is not None else None,
        stdin=subprocess.DEVNULL,
        env=env,
        close_fds=True,
        **popen_kwargs()
    )


def _kill_tree_posix(pid: int, grace_sec: float) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_tree_windows(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, ValueError):
        return False
    return completed.returncode == 0


def kill_tree(proc: "subprocess.Popen[bytes]", grace_sec: float = 5.0) -> None:
    """Убить процесс вместе со всеми его потомками и дождаться завершения."""
    if proc.poll() is not None:
        return

    if IS_WINDOWS:
        if not _kill_tree_windows(proc.pid):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        # start_new_session=True гарантирует pgid == pid; берём pid напрямую,
        # чтобы не гоняться с getpgid на уже завершившемся процессе.
        _kill_tree_posix(proc.pid, grace_sec)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def wait_with_timeout(
    proc: "subprocess.Popen[bytes]", timeout_sec: Optional[float]
) -> Optional[int]:
    """Дождаться завершения. Возвращает код возврата или None при таймауте."""
    if not timeout_sec:
        return proc.wait()
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None
