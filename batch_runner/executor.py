"""Выполнение одного шага для одного эксперимента."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import procs
from .command import build_argv
from .config import Config, Step
from .pairing import Experiment

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

#: Сколько байт хвоста каждого лога читать при проверке regex-ом.
LOG_TAIL_BYTES = 1024 * 1024

#: Папка с runner.py — доступна в командах как {root}, чтобы можно было
#: сослаться на вспомогательный скрипт рядом с программой, не прописывая
#: абсолютный путь, свой на каждой машине.
PROGRAM_ROOT = Path(__file__).resolve().parent.parent

ProcessHook = Callable[["object"], None]


@dataclass
class StepResult:
    """Итог одной попытки одного шага."""

    n: int
    step: str
    status: str
    rc: Optional[int]
    attempt: int
    seconds: float
    workdir: Path
    started: str
    reason: str = ""
    argv: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_record(self, out_root: Path) -> Dict[str, object]:
        """Строка для журнала. Путь относительный — журнал переживает переезд папки."""
        try:
            workdir = str(self.workdir.relative_to(out_root))
        except ValueError:
            workdir = str(self.workdir)
        return {
            "n": self.n,
            "step": self.step,
            "status": self.status,
            "rc": self.rc,
            "attempt": self.attempt,
            "sec": round(self.seconds, 1),
            "started": self.started,
            "dir": workdir,
            "reason": self.reason,
        }


def workdir_for(out_root: Path, n: int, step_name: str, attempt: int) -> Path:
    """Своя папка на каждую попытку каждого шага.

    Изоляция нужна не для красоты: утилита пишет логи сама, и если она делает
    это по фиксированному имени, то параллельные запуски в общей папке
    затрут результаты друг друга.
    """
    return out_root / "runs" / str(n) / step_name / "attempt{}".format(attempt)


def next_attempt_number(out_root: Path, n: int, step_name: str) -> int:
    """Первый свободный номер попытки для этого шага.

    Нумерация сквозная по всем прогонам: если шаг уже падал вчера, сегодняшний
    перезапуск не должен затирать вчерашние логи — именно по ним и разбираются,
    почему не получилось.
    """
    parent = out_root / "runs" / str(n) / step_name
    if not parent.is_dir():
        return 1
    highest = 0
    try:
        for child in parent.iterdir():
            if not child.is_dir() or not child.name.startswith("attempt"):
                continue
            suffix = child.name[len("attempt"):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    except OSError:
        return 1
    return highest + 1


def _collect_outputs(source: Path, workdir: Path, globs: Sequence[str], since: float) -> None:
    """Забрать в папку попытки то, что утилита написала мимо неё.

    Нужно только когда isolate_cwd выключен: при включённом рабочая папка
    и есть папка попытки, копировать нечего.
    """
    if source.resolve() == workdir.resolve():
        return
    for pattern in globs:
        for path in source.glob(pattern):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < since - 1:
                    continue
                shutil.copy2(path, workdir / path.name)
            except OSError:
                continue


def _log_text(workdir: Path, globs: Sequence[str]) -> str:
    """Хвосты всех логов попытки одной строкой — для проверки regex-ом."""
    paths: List[Path] = [workdir / "stdout.log"]
    for pattern in globs:
        paths.extend(sorted(workdir.glob(pattern)))

    seen = set()
    chunks: List[str] = []
    for path in paths:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            with path.open("rb") as handle:
                size = path.stat().st_size
                if size > LOG_TAIL_BYTES:
                    handle.seek(size - LOG_TAIL_BYTES)
                chunks.append(handle.read().decode("utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _verdict(
    rc: Optional[int], cfg: Config, workdir: Path
) -> "tuple[str, str]":
    """Успех или провал: код возврата плюс необязательная проверка по логам."""
    if rc not in cfg.success_exit_codes:
        return STATUS_FAIL, "код возврата {}".format(rc)

    fail_regex = cfg.run.fail_regex
    success_regex = cfg.run.success_regex
    if not fail_regex and not success_regex:
        return STATUS_OK, ""

    text = _log_text(workdir, cfg.run.collect)
    if fail_regex:
        match = re.search(fail_regex, text, re.MULTILINE)
        if match:
            return STATUS_FAIL, "в логе найдено {!r}".format(match.group(0)[:80])
    if success_regex and not re.search(success_regex, text, re.MULTILINE):
        return STATUS_FAIL, "в логе нет признака успеха {!r}".format(success_regex)
    return STATUS_OK, ""


def build_step_argv(
    cfg: Config,
    step: Step,
    experiment: Experiment,
    workdir: Path,
    input_dir: Path,
    out_root: Path,
) -> List[str]:
    """Готовая команда для запуска: exe плюс отрендеренные аргументы."""
    values = experiment.values()
    values.update(
        {
            "dir": str(input_dir),
            "taskdir": str(workdir),
            "outdir": str(out_root),
            "root": str(PROGRAM_ROOT),
        }
    )
    return build_argv(cfg.exe, step.args, values)


def run_step(
    cfg: Config,
    step: Step,
    experiment: Experiment,
    attempt: int,
    out_root: Path,
    input_dir: Path,
    timeout_override: Optional[int] = None,
    on_start: Optional[ProcessHook] = None,
    on_finish: Optional[ProcessHook] = None,
) -> StepResult:
    """Запустить утилиту один раз и разобрать результат.

    ``on_start``/``on_finish`` дают пулу возможность вести реестр живых
    процессов, чтобы прибить их по Ctrl+C.
    """
    workdir = workdir_for(out_root, experiment.n, step.name, attempt)
    workdir.mkdir(parents=True, exist_ok=True)

    argv = build_step_argv(cfg, step, experiment, workdir, input_dir, out_root)
    cwd = workdir if cfg.run.isolate_cwd else Path.cwd()
    timeout = step.timeout_sec if timeout_override is None else timeout_override

    started_at = datetime.now().isoformat(timespec="seconds")
    started_wall = time.time()
    started = time.monotonic()

    log_path = workdir / "stdout.log"
    proc = None
    try:
        with log_path.open("wb") as log_file:
            log_file.write(
                ("$ " + " ".join(argv) + os.linesep + os.linesep).encode(
                    "utf-8", errors="replace"
                )
            )
            log_file.flush()
            proc = procs.spawn(argv, cwd=str(cwd), stdout=log_file)
            if on_start is not None:
                on_start(proc)
            rc = procs.wait_with_timeout(proc, timeout if timeout else None)
            timed_out = rc is None
            if timed_out:
                procs.kill_tree(proc)
                rc = proc.poll()
    except OSError as exc:
        elapsed = time.monotonic() - started
        return StepResult(
            n=experiment.n,
            step=step.name,
            status=STATUS_ERROR,
            rc=None,
            attempt=attempt,
            seconds=elapsed,
            workdir=workdir,
            started=started_at,
            reason="не удалось запустить: {}".format(exc),
            argv=argv,
        )
    finally:
        if proc is not None and on_finish is not None:
            on_finish(proc)

    elapsed = time.monotonic() - started
    _collect_outputs(cwd, workdir, cfg.run.collect, started_wall)

    if timed_out:
        status, reason = STATUS_TIMEOUT, "превышен таймаут {} с".format(timeout)
    else:
        status, reason = _verdict(rc, cfg, workdir)

    return StepResult(
        n=experiment.n,
        step=step.name,
        status=status,
        rc=rc,
        attempt=attempt,
        seconds=elapsed,
        workdir=workdir,
        started=started_at,
        reason=reason,
        argv=argv,
    )
