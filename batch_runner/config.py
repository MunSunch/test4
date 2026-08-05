"""Чтение config.json, слияние с аргументами CLI и валидация.

Конфиг в JSON, а не в TOML, сознательно: ``tomllib`` появился только
в Python 3.11, а системный python3 в macOS нередко 3.9. JSON читается
любым Python и не требует ставить зависимости.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from .command import RESERVED_PLACEHOLDERS, placeholders_in

DEFAULTS: Dict[str, Any] = {
    "exe": "storm",
    "workers": 16,
    "attempts": 3,
    "retry_delay_sec": 30,
    "success_exit_codes": [0],
    "patterns": {
        "config": "configs/config_{n}.yml",
        "enable": "affects/affect_enable_{n}.yml",
        "disable": "affects/affect_disable_{n}.yml",
    },
    "steps": {
        "create": {
            "args": ["-c", "{config}", "-f", "{enable}", "-e"],
            "timeout_sec": 2400,
        },
        "enable": {
            "args": ["-c", "{config}", "-u", "-f", "{enable}", "-e"],
            "timeout_sec": 300,
        },
        "disable": {
            "args": ["-c", "{config}", "-u", "-f", "{disable}", "-e"],
            "timeout_sec": 300,
        },
        "stop": {
            "args": ["-c", "{config}"],
            "timeout_sec": 2400,
        },
    },
    "run": {
        "isolate_cwd": True,
        "collect": ["*.log", "**/*.log"],
        "fail_regex": "",
        "success_regex": "",
    },
}


class ConfigError(Exception):
    """Ошибка конфигурации: показывается пользователю без трейсбека."""


@dataclass
class Step:
    """Одна команда утилиты (create / enable / disable / stop / что угодно)."""

    name: str
    args: List[str]
    timeout_sec: int

    @property
    def placeholders(self) -> Set[str]:
        return placeholders_in(self.args)


@dataclass
class RunOptions:
    """Как запускать шаг и как разбирать его результат."""

    isolate_cwd: bool = True
    collect: List[str] = field(default_factory=lambda: ["*.log", "**/*.log"])
    fail_regex: str = ""
    success_regex: str = ""


@dataclass
class Config:
    exe: str
    workers: int
    attempts: int
    retry_delay_sec: float
    success_exit_codes: List[int]
    patterns: Dict[str, str]
    steps: "Dict[str, Step]"
    run: RunOptions
    path: Optional[Path] = None

    def step(self, name: str) -> Step:
        if name not in self.steps:
            known = ", ".join(self.steps) or "(пусто)"
            raise ConfigError(
                "неизвестный шаг {!r}; в конфиге есть: {}".format(name, known)
            )
        return self.steps[name]

    @property
    def roles(self) -> Set[str]:
        """Все известные роли файлов — это просто ключи "patterns"."""
        return set(self.patterns)

    def roles_for(self, step_name: str) -> Set[str]:
        """Какие файлы нужны шагу — выводится из его же шаблона.

        Шагу ``stop`` с аргументами ``["-c", "{config}"]`` affect-файлы не нужны,
        и требовать их наличия при подборе пар незачем.
        """
        return self.step(step_name).placeholders & self.roles

    def required_roles(self, step_names: Sequence[str]) -> Set[str]:
        """Объединение требований всех выбранных шагов."""
        roles: Set[str] = set()
        for name in step_names:
            roles |= self.roles_for(name)
        return roles


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Рекурсивно наложить override на base, не трогая исходники."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load(path: Optional[Path]) -> Config:
    """Прочитать конфиг с диска и наложить его на встроенные значения."""
    raw: Dict[str, Any] = {}
    if path is not None:
        if not path.exists():
            raise ConfigError("конфиг не найден: {}".format(path))
        try:
            # utf-8-sig, а не utf-8: конфиг правят руками, и редакторы
            # (особенно на Windows) охотно дописывают в начало BOM.
            with path.open("r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError("не разобрать {}: {}".format(path, exc)) from exc
        if not isinstance(raw, dict):
            raise ConfigError("в {} ожидался JSON-объект".format(path))

    merged = _deep_merge(DEFAULTS, raw)

    steps: Dict[str, Step] = {}
    raw_steps = merged.get("steps") or {}
    if not isinstance(raw_steps, dict):
        raise ConfigError('поле "steps" должно быть объектом')
    for name, body in raw_steps.items():
        if not isinstance(body, dict) or "args" not in body:
            raise ConfigError('шаг "{}": ожидался объект с полем "args"'.format(name))
        args = body["args"]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError('шаг "{}": "args" должен быть списком строк'.format(name))
        steps[name] = Step(
            name=name,
            args=list(args),
            timeout_sec=int(body.get("timeout_sec", 0)),
        )

    run_raw = merged.get("run") or {}
    run = RunOptions(
        isolate_cwd=bool(run_raw.get("isolate_cwd", True)),
        collect=list(run_raw.get("collect", [])),
        fail_regex=str(run_raw.get("fail_regex", "")),
        success_regex=str(run_raw.get("success_regex", "")),
    )

    return Config(
        exe=str(merged["exe"]),
        workers=int(merged["workers"]),
        attempts=int(merged["attempts"]),
        retry_delay_sec=float(merged["retry_delay_sec"]),
        success_exit_codes=[int(c) for c in merged["success_exit_codes"]],
        patterns=dict(merged["patterns"]),
        steps=steps,
        run=run,
        path=path,
    )


def resolve_exe(exe: str) -> Optional[str]:
    """Найти исполняемый файл утилиты. None, если не нашёлся.

    Если в ``exe`` есть разделитель пути — это путь, ищем файл напрямую.
    Иначе ищем в PATH. Учтите: алиасы и функции шелла сюда не попадают,
    они существуют только внутри интерактивного шелла.
    """
    if os.sep in exe or (os.altsep and os.altsep in exe):
        candidate = Path(exe).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        return None
    return shutil.which(exe)


def validate(cfg: Config, step_names: Sequence[str], require_exe: bool = True) -> List[str]:
    """Проверить конфиг до первого запуска. Возвращает список предупреждений.

    При цене ошибки в 10 минут на эксперимент всё, что можно проверить заранее,
    проверяется заранее: неправильный флаг должен всплыть на первой секунде,
    а не через час прогона.
    """
    warnings: List[str] = []

    if cfg.workers < 1:
        raise ConfigError("workers должен быть не меньше 1")
    if cfg.attempts < 1:
        raise ConfigError("attempts должен быть не меньше 1")
    if not cfg.steps:
        raise ConfigError('в конфиге не задано ни одного шага ("steps")')

    reserved = set(RESERVED_PLACEHOLDERS)
    clash = reserved & cfg.roles
    if clash:
        raise ConfigError(
            '"patterns" не может использовать служебные имена: {}'.format(
                ", ".join(sorted(clash))
            )
        )

    for name in step_names:
        step = cfg.step(name)
        if not step.args:
            raise ConfigError('шаг "{}": пустой список args'.format(name))

        # Опечатка вроде {enabel} иначе уехала бы в команду как есть,
        # и утилита получила бы литеральную строку вместо пути.
        unknown = step.placeholders - reserved - cfg.roles
        if unknown:
            raise ConfigError(
                'шаг "{}": неизвестные плейсхолдеры {}. Доступны: {}'.format(
                    name,
                    ", ".join("{" + p + "}" for p in sorted(unknown)),
                    ", ".join("{" + p + "}" for p in sorted(cfg.roles | reserved)),
                )
            )
        if not cfg.roles_for(name):
            warnings.append(
                'шаг "{}" не использует ни одного файла из "patterns" — '
                "команда будет одинаковой для всех экспериментов".format(name)
            )

    for role in sorted(cfg.required_roles(step_names)):
        pattern = cfg.patterns[role]
        if pattern.count("{n}") != 1:
            raise ConfigError(
                'patterns.{}: ожидается ровно один "{{n}}", получено {} в "{}"'.format(
                    role, pattern.count("{n}"), pattern
                )
            )

    resolved = resolve_exe(cfg.exe)
    if resolved is None:
        message = (
            "утилита {!r} не найдена ни в PATH, ни как файл. "
            "Если это алиас или функция шелла — впишите в exe полный путь "
            "к настоящему бинарю (посмотрите вывод `type {}`)".format(cfg.exe, cfg.exe)
        )
        if require_exe:
            raise ConfigError(message)
        warnings.append(message)
    elif os.name != "nt" and not os.access(resolved, os.X_OK):
        warnings.append("файл {} есть, но не помечен исполняемым".format(resolved))

    return warnings
