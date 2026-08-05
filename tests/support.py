"""Общая обвязка для тестов: песочница с yml-файлами и конфигом под заглушку."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
FAKE_STORM = ROOT / "tests" / "fake_storm.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runner  # noqa: E402  (после правки sys.path)

ALL_ROLES = ("config", "enable", "disable")

#: Как файлы разложены на диске: своя папка на роль, номер в конце имени.
LAYOUT = {
    "config": "configs/config_{}.yml",
    "enable": "affects/affect_enable_{}.yml",
    "disable": "affects/affect_disable_{}.yml",
}


@contextmanager
def env(**values: Optional[str]) -> Iterator[None]:
    """Временно выставить переменные окружения заглушки."""
    saved: Dict[str, Optional[str]] = {}
    for key, value in values.items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_main(*argv: str) -> "Tuple[int, str]":
    """Запустить runner.main с перехватом всего вывода."""
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        code = runner.main(list(argv))
    return code, buffer.getvalue()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Sandbox:
    """Временная папка с входными yml, конфигом и местом под результаты."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="storm_test_"))
        self.inputs = self.root / "exp"
        self.out = self.root / "out"
        self.config_path = self.root / "config.json"
        self.inputs.mkdir(parents=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def make_experiments(
        self, ids: "Iterable[object]", roles: Sequence[str] = ALL_ROLES
    ) -> None:
        """Разложить файлы экспериментов.

        Идентификатор — номер ("103") или номер с кластером ("103m5"),
        как он выглядит в имени файла.
        """
        for experiment_id in ids:
            for role in roles:
                path = self.inputs / LAYOUT[role].format(experiment_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "{}: эксперимент {}\n".format(role, experiment_id), encoding="utf-8"
                )

    def write_config(self, **overrides: Any) -> Path:
        base: Dict[str, Any] = {
            "exe": sys.executable,
            "workers": 4,
            "attempts": 1,
            "retry_delay_sec": 0,
            "success_exit_codes": [0],
            "patterns": {
                "config": "configs/config_{n}.yml",
                "enable": "affects/affect_enable_{n}.yml",
                "disable": "affects/affect_disable_{n}.yml",
            },
            "steps": {
                "create": {
                    "args": [str(FAKE_STORM), "-c", "{config}", "-f", "{enable}", "-e"],
                    "timeout_sec": 60,
                },
                "enable": {
                    "args": [str(FAKE_STORM), "-c", "{config}", "-u", "-f", "{enable}", "-e"],
                    "timeout_sec": 60,
                },
                "disable": {
                    "args": [str(FAKE_STORM), "-c", "{config}", "-u", "-f", "{disable}", "-e"],
                    "timeout_sec": 60,
                },
                "stop": {
                    "args": [str(FAKE_STORM), "-c", "{config}"],
                    "timeout_sec": 60,
                },
            },
            "run": {
                "isolate_cwd": True,
                "collect": ["*.log"],
                "fail_regex": "",
                "success_regex": "",
            },
        }
        merged = _deep_merge(base, overrides)
        self.config_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.config_path

    def run(self, *extra: str) -> "Tuple[int, str]":
        """Запустить runner.main с базовыми аргументами. Возвращает (код, вывод)."""
        return run_main(
            "--dir", str(self.inputs),
            "--config", str(self.config_path),
            "--out", str(self.out),
            *extra
        )

    @property
    def state_path(self) -> Path:
        return self.out / "state.jsonl"

    def attempt_dir(self, n: object, step: str, attempt: int = 1) -> Path:
        return self.out / "runs" / str(n) / step / "attempt{}".format(attempt)
