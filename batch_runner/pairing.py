"""Поиск файлов и сборка их в эксперименты.

``{n}`` — это номер эксперимента вместе с названием кластера, если оно есть
в имени: и ``config_103.yml``, и ``config_103m5.yml`` подходят под один и тот же
паттерн ``config_{n}.yml``. Идентификатором служит всё совпадение целиком,
поэтому 103m4 и 103m5 — два разных эксперимента.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import ConfigError

#: Номер и необязательный кластер после него: 103, 103m5, 20m4.
NUMBER_GROUP = r"(\d+[A-Za-z0-9]*)"

_LEADING_NUMBER_RE = re.compile(r"^(\d+)")
_DIGITS_RE = re.compile(r"(\d+)")
_ID_RE = re.compile(r"^\d+[A-Za-z0-9]*$")
_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


def natural_key(experiment_id: str) -> tuple:
    """Ключ сортировки, в котором числа сравниваются как числа.

    Строковая сортировка поставила бы 103 раньше 20, а 103m10 — раньше
    103m5. Здесь идентификатор режется на куски цифр и не-цифр, и каждый
    кусок сравнивается по своему типу.
    """
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in _DIGITS_RE.split(experiment_id)
        if part
    )


def number_of(experiment_id: str) -> int:
    """Числовая часть идентификатора: 103 из "103m5"."""
    match = _LEADING_NUMBER_RE.match(experiment_id)
    return int(match.group(1)) if match is not None else 0


@dataclass
class Experiment:
    """Один эксперимент: идентификатор и абсолютные пути его файлов."""

    n: str
    files: Dict[str, Path] = field(default_factory=dict)

    @property
    def sort_key(self) -> "Tuple[int, str]":
        return natural_key(self.n)

    def values(self) -> Dict[str, str]:
        """Значения плейсхолдеров для подстановки в команду."""
        result = {role: str(path) for role, path in self.files.items()}
        result["n"] = self.n
        return result


def split_pattern(pattern: str) -> "Tuple[str, str]":
    """Разделить паттерн на папку и имя файла.

    Файлы разных ролей обычно лежат в разных папках (``configs/`` и
    ``affects/``), поэтому подпапка указывается прямо в паттерне —
    отдельных аргументов командной строки под каждую роль не требуется.
    """
    normalized = pattern.replace("\\", "/")
    head, _, tail = normalized.rpartition("/")
    return head, tail


def pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """Превратить ``config_{n}.yml`` в регулярку.

    Всё, кроме ``{n}``, экранируется — точки, скобки и прочие спецсимволы
    в именах файлов должны совпадать буквально. Папка из паттерна
    отбрасывается: сопоставляется только имя файла.
    """
    _, name = split_pattern(pattern)
    if name.count("{n}") != 1:
        raise ConfigError(
            'в паттерне "{}" должен быть ровно один "{{n}}" в имени файла'.format(pattern)
        )
    left, right = name.split("{n}")
    return re.compile("^" + re.escape(left) + NUMBER_GROUP + re.escape(right) + "$")


def base_dir_for(root: Path, pattern: str) -> Path:
    """Папка, в которой лежат файлы этой роли."""
    head, _ = split_pattern(pattern)
    if not head:
        return root
    candidate = Path(head).expanduser()
    if candidate.is_absolute():
        return candidate
    return root / head


@dataclass
class Selection:
    """Что выбрано аргументом ``--only``."""

    ids: Set[str] = field(default_factory=set)
    numbers: Set[int] = field(default_factory=set)

    def matches(self, experiment: Experiment) -> bool:
        # Голое число выбирает эксперимент на всех кластерах,
        # полный идентификатор — ровно один.
        return experiment.n in self.ids or number_of(experiment.n) in self.numbers


def parse_selection(spec: str) -> Selection:
    """Разобрать ``103,105m4,107-109`` в набор условий."""
    selection = Selection()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue

        range_match = _RANGE_RE.match(chunk)
        if range_match is not None:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start > end:
                start, end = end, start
            selection.numbers.update(range(start, end + 1))
            continue

        if _ID_RE.match(chunk) is None:
            raise ConfigError(
                "не разобрать {!r}; ожидается 103, 103m5, 103-109 или их "
                "перечисление через запятую".format(chunk)
            )
        if chunk.isdigit():
            selection.numbers.add(int(chunk))
        else:
            selection.ids.add(chunk)
    return selection


def scan(
    directory: Path,
    patterns: Dict[str, str],
    required_roles: Iterable[str],
    recursive: bool = False,
) -> "Tuple[List[Experiment], List[str]]":
    """Собрать эксперименты из папки.

    Возвращает список экспериментов (отсортированный по номеру и кластеру)
    и список предупреждений: неполные наборы, дубликаты, паттерны без единого
    совпадения. Неполный набор именно предупреждение, а не молчаливый пропуск —
    опечатку в имени одного из десятков yml иначе заметишь только в конце прогона.
    """
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ConfigError("не папка: {}".format(directory))

    required = sorted(set(required_roles))
    for role in required:
        if role not in patterns:
            raise ConfigError('в "patterns" нет записи "{}"'.format(role))

    found: Dict[str, Dict[str, Path]] = {role: {} for role in required}
    warnings: List[str] = []
    listings: Dict[str, List[Path]] = {}

    for role in required:
        pattern = patterns[role]
        base = base_dir_for(directory, pattern)
        regex = pattern_to_regex(pattern)

        key = str(base)
        if key not in listings:
            if not base.is_dir():
                warnings.append(
                    'папка для роли "{}" не найдена: {} (паттерн "{}")'.format(
                        role, base, pattern
                    )
                )
                listings[key] = []
            else:
                entries = sorted(base.rglob("*") if recursive else base.iterdir())
                listings[key] = [p for p in entries if p.is_file()]

        for path in listings[key]:
            match = regex.match(path.name)
            if match is None:
                continue
            experiment_id = match.group(1)
            previous = found[role].get(experiment_id)
            if previous is not None:
                warnings.append(
                    "{} {}: найдено несколько файлов ({} и {}), беру первый".format(
                        role, experiment_id, previous.name, path.name
                    )
                )
                continue
            found[role][experiment_id] = path.resolve()

        if not found[role] and listings[key]:
            sample = ", ".join(p.name for p in listings[key][:5])
            warnings.append(
                'по паттерну "{}" (роль {}) не найдено ни одного файла в {}. '
                "Что там лежит: {}".format(pattern, role, base, sample)
            )

    all_ids: Set[str] = set()
    for role in required:
        all_ids |= set(found.get(role, {}))

    experiments: List[Experiment] = []
    for experiment_id in sorted(all_ids, key=natural_key):
        missing = [role for role in required if experiment_id not in found.get(role, {})]
        if missing:
            warnings.append(
                "{}: пропущен, нет файлов для ролей {}".format(
                    experiment_id, ", ".join(missing)
                )
            )
            continue
        experiments.append(
            Experiment(
                n=experiment_id,
                files={role: found[role][experiment_id] for role in required},
            )
        )

    return experiments, warnings


def select(
    experiments: "Sequence[Experiment]",
    only: Optional[Selection] = None,
    limit: Optional[int] = None,
) -> "List[Experiment]":
    """Отфильтровать эксперименты по ``--only`` и ``--limit``."""
    chosen = list(experiments)
    if only is not None:
        chosen = [e for e in chosen if only.matches(e)]
    if limit is not None and limit >= 0:
        chosen = chosen[:limit]
    return chosen
