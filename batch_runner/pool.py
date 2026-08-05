"""Оркестрация: пул потоков, цепочка шагов, ретраи, прогресс и Ctrl+C.

Потоки, а не процессы: вся работа происходит внутри внешней утилиты,
питоновский код только ждёт её завершения, и GIL при этом отпущен.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, TextIO

from . import procs
from .config import Config
from .executor import (
    STATUS_OK,
    STATUS_SKIPPED,
    StepResult,
    next_attempt_number,
    run_step,
    workdir_for,
)
from .pairing import Experiment
from .state import Journal, Key


def fmt_duration(seconds: float) -> str:
    """Человеческая длительность: 3с, 10м12с, 2ч10м."""
    seconds = int(round(seconds))
    if seconds < 60:
        return "{}с".format(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return "{}м{:02d}с".format(minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "{}ч{:02d}м".format(hours, minutes)


@dataclass
class TaskOutcome:
    """Итог по одному эксперименту: все выполненные шаги в порядке запуска."""

    n: int
    results: List[StepResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.results:
            return STATUS_SKIPPED
        for result in self.results:
            if result.status not in (STATUS_OK, STATUS_SKIPPED):
                return result.status
        return STATUS_OK

    @property
    def seconds(self) -> float:
        return sum(r.seconds for r in self.results)

    @property
    def failed_step(self) -> Optional[StepResult]:
        for result in self.results:
            if result.status not in (STATUS_OK, STATUS_SKIPPED):
                return result
        return None


class Runner:
    """Гоняет эксперименты через пул потоков и печатает прогресс."""

    def __init__(
        self,
        cfg: Config,
        step_names: Sequence[str],
        experiments: Sequence[Experiment],
        out_root: Path,
        input_dir: Path,
        journal: "Journal",
        done_keys: Optional[Set[Key]] = None,
        timeout_override: Optional[int] = None,
        stream: Optional[TextIO] = None,
    ) -> None:
        self.cfg = cfg
        self.step_names = list(step_names)
        self.experiments = list(experiments)
        self.out_root = out_root
        self.input_dir = input_dir
        self.journal = journal
        self.done_keys = set(done_keys or ())
        self.timeout_override = timeout_override
        self.stream = stream if stream is not None else sys.stdout

        self._print_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active: Dict[int, object] = {}
        self._stop = threading.Event()
        self._stats_lock = threading.Lock()
        self._finished = 0
        self._running = 0
        self._durations: List[float] = []

    # --- вывод ---------------------------------------------------------

    def say(self, text: str) -> None:
        with self._print_lock:
            self.stream.write(text + "\n")
            self.stream.flush()

    def _eta(self) -> str:
        with self._stats_lock:
            if not self._durations:
                return "?"
            average = sum(self._durations) / len(self._durations)
            remaining = len(self.experiments) - self._finished
        if remaining <= 0:
            return "0с"
        workers = max(1, min(self.cfg.workers, len(self.experiments)))
        return fmt_duration(average * remaining / workers)

    # --- реестр живых процессов ----------------------------------------

    def _register(self, proc: object) -> None:
        with self._active_lock:
            self._active[id(proc)] = proc

    def _unregister(self, proc: object) -> None:
        with self._active_lock:
            self._active.pop(id(proc), None)

    def _kill_active(self) -> None:
        with self._active_lock:
            alive = list(self._active.values())
        for proc in alive:
            procs.kill_tree(proc)  # type: ignore[arg-type]

    # --- выполнение ----------------------------------------------------

    def _run_with_retries(self, experiment: Experiment, step_name: str) -> StepResult:
        step = self.cfg.step(step_name)
        first_attempt = next_attempt_number(self.out_root, experiment.n, step_name)
        result = None
        for index in range(self.cfg.attempts):
            result = run_step(
                self.cfg,
                step,
                experiment,
                first_attempt + index,
                self.out_root,
                self.input_dir,
                timeout_override=self.timeout_override,
                on_start=self._register,
                on_finish=self._unregister,
            )
            self.journal.append(result)
            if result.ok:
                return result
            if index + 1 < self.cfg.attempts and not self._stop.is_set():
                self.say(
                    "[retry] N={:<4} {:<7} {:<10} попытка {}/{} через {}".format(
                        experiment.n,
                        step_name,
                        result.reason or result.status,
                        index + 2,
                        self.cfg.attempts,
                        fmt_duration(self.cfg.retry_delay_sec),
                    )
                )
                # Ожидание через событие, чтобы Ctrl+C не пришлось ждать паузу.
                self._stop.wait(self.cfg.retry_delay_sec)
            if self._stop.is_set():
                break
        return result  # type: ignore[return-value]

    def _run_experiment(self, experiment: Experiment) -> TaskOutcome:
        outcome = TaskOutcome(n=experiment.n)
        with self._stats_lock:
            self._running += 1
        started = time.monotonic()
        try:
            for step_name in self.step_names:
                if (experiment.n, step_name) in self.done_keys:
                    outcome.results.append(
                        StepResult(
                            n=experiment.n,
                            step=step_name,
                            status=STATUS_SKIPPED,
                            rc=None,
                            attempt=0,
                            seconds=0.0,
                            workdir=workdir_for(self.out_root, experiment.n, step_name, 1),
                            started="",
                            reason="уже выполнено в прошлый раз",
                        )
                    )
                    continue
                if self._stop.is_set():
                    break

                result = self._run_with_retries(experiment, step_name)
                outcome.results.append(result)
                if result.ok:
                    self.say(
                        "[ok   ] N={:<4} {:<7} {}".format(
                            experiment.n, step_name, fmt_duration(result.seconds)
                        )
                    )
                else:
                    self.say(
                        "[FAIL ] N={:<4} {:<7} {}  ->  {}".format(
                            experiment.n,
                            step_name,
                            result.reason or result.status,
                            result.workdir,
                        )
                    )
                    # Нет смысла применять аффект к эксперименту,
                    # который не создался.
                    break
        finally:
            with self._stats_lock:
                self._running -= 1
                self._finished += 1
                self._durations.append(time.monotonic() - started)
        return outcome

    def _report_task(self, outcome: TaskOutcome) -> None:
        with self._stats_lock:
            finished, running = self._finished, self._running
        self.say(
            "[{:5}] {}/{}  N={:<4} {:<8} | в работе: {} | ETA ~{}".format(
                "done" if outcome.status == STATUS_OK else outcome.status,
                finished,
                len(self.experiments),
                outcome.n,
                fmt_duration(outcome.seconds),
                running,
                self._eta(),
            )
        )

    def run(self) -> List[TaskOutcome]:
        """Прогнать все эксперименты. Возвращает итоги в порядке завершения."""
        outcomes: List[TaskOutcome] = []
        if not self.experiments:
            return outcomes

        workers = max(1, min(self.cfg.workers, len(self.experiments)))
        pool = ThreadPoolExecutor(max_workers=workers)
        futures: List[Future] = [
            pool.submit(self._run_experiment, experiment)
            for experiment in self.experiments
        ]
        pending = set(futures)

        def drain() -> None:
            for future in as_completed(list(pending)):
                pending.discard(future)
                try:
                    outcome = future.result()
                except CancelledError:
                    continue
                outcomes.append(outcome)
                self._report_task(outcome)

        try:
            drain()
        except KeyboardInterrupt:
            self._stop.set()
            self.say(
                "\n[стоп ] Ctrl+C: новые эксперименты не запускаю, "
                "жду уже работающие. Ещё раз Ctrl+C — убить их немедленно."
            )
            pool.shutdown(wait=False, cancel_futures=True)
            try:
                drain()
            except KeyboardInterrupt:
                self.say("[стоп ] убиваю активные процессы")
                self._kill_active()
                try:
                    drain()
                except KeyboardInterrupt:
                    pass
        finally:
            pool.shutdown(wait=False)

        return outcomes


def build_report(
    outcomes: Sequence[TaskOutcome],
    warnings: Sequence[str],
    elapsed: float,
    total_planned: int,
) -> str:
    """Текстовый отчёт: сводка, предупреждения и разбор неуспешных."""
    counts: Dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1

    lines: List[str] = []
    lines.append("Итог: {} из {} экспериментов обработано за {}".format(
        len(outcomes), total_planned, fmt_duration(elapsed)
    ))
    for status in sorted(counts):
        lines.append("  {:8} {}".format(status, counts[status]))

    if warnings:
        lines.append("")
        lines.append("Предупреждения ({}):".format(len(warnings)))
        for warning in warnings:
            lines.append("  - " + warning)

    problems = [o for o in outcomes if o.status != STATUS_OK]
    if problems:
        lines.append("")
        lines.append("Неуспешные ({}):".format(len(problems)))
        for outcome in sorted(problems, key=lambda o: o.n):
            failed = outcome.failed_step
            if failed is None:
                lines.append("  N={}: {}".format(outcome.n, outcome.status))
                continue
            lines.append(
                "  N={} шаг {}: {} (последняя попытка №{})".format(
                    outcome.n,
                    failed.step,
                    failed.reason or failed.status,
                    failed.attempt,
                )
            )
            lines.append("      логи: {}".format(failed.workdir))

    return "\n".join(lines)
