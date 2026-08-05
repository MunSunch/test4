"""Поиск yml-файлов и сборка их в эксперименты по номеру N."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import ConfigError


@dataclass
class Experiment:
    """Один эксперимент: номер и абсолютные пути его файлов по ролям."""

    n: int
    files: Dict[str, Path]

    def values(self) -> Dict[str, str]:
        """Значения плейсхолдеров для подстановки в команду."""
        result = {role: str(path) for role, path in self.files.items()}
        result["n"] = str(self.n)
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
    """Превратить ``config_{n}.yml`` в регулярку с числовой группой.

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
    return re.compile("^" + re.escape(left) + r"(\d+)" + re.escape(right) + "$")


def base_dir_for(root: Path, pattern: str) -> Path:
    """Папка, в которой лежат файлы этой роли."""
    head, _ = split_pattern(pattern)
    if not head:
        return root
    candidate = Path(head).expanduser()
    if candidate.is_absolute():
        return candidate
    return root / head


def parse_number_spec(spec: str) -> Set[int]:
    """Разобрать ``1,3,7-12`` в множество номеров."""
    numbers: Set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            start_text, _, end_text = chunk.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                raise ConfigError("не разобрать диапазон {!r}".format(chunk)) from None
            if start > end:
                start, end = end, start
            numbers.update(range(start, end + 1))
        else:
            try:
                numbers.add(int(chunk))
            except ValueError:
                raise ConfigError("не разобрать номер {!r}".format(chunk)) from None
    return numbers


def scan(
    directory: Path,
    patterns: Dict[str, str],
    required_roles: Iterable[str],
    recursive: bool = False,
) -> "Tuple[List[Experiment], List[str]]":
    """Собрать эксперименты из папки.

    Возвращает список экспериментов (отсортированный по числовому N) и список
    предупреждений: непарные файлы, дубликаты, паттерны без единого совпадения.
    Непарные файлы именно предупреждение, а не молчаливый пропуск — опечатку
    в имени одного из десятков yml иначе заметишь только в конце прогона.
    """
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ConfigError("не папка: {}".format(directory))

    required = sorted(set(required_roles))
    for role in required:
        if role not in patterns:
            raise ConfigError('в "patterns" нет записи "{}"'.format(role))

    found: Dict[str, Dict[int, Path]] = {role: {} for role in required}
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
            number = int(match.group(1))
            previous = found[role].get(number)
            if previous is not None:
                warnings.append(
                    "{} №{}: найдено несколько файлов ({} и {}), беру первый".format(
                        role, number, previous.name, path.name
                    )
                )
                continue
            found[role][number] = path.resolve()

        if not found[role] and listings[key]:
            sample = ", ".join(p.name for p in listings[key][:5])
            warnings.append(
                'по паттерну "{}" (роль {}) не найдено ни одного файла в {}. '
                "Что там лежит: {}".format(pattern, role, base, sample)
            )

    all_numbers: Set[int] = set()
    for role in required:
        all_numbers |= set(found.get(role, {}))

    experiments: List[Experiment] = []
    for number in sorted(all_numbers):
        missing = [role for role in required if number not in found.get(role, {})]
        if missing:
            warnings.append(
                "№{}: пропущен, нет файлов для ролей {}".format(number, ", ".join(missing))
            )
            continue
        files_by_role = {role: found[role][number] for role in required}
        experiments.append(Experiment(n=number, files=files_by_role))

    return experiments, warnings


def select(
    experiments: "Sequence[Experiment]",
    only: Optional[Set[int]] = None,
    limit: Optional[int] = None,
) -> "List[Experiment]":
    """Отфильтровать эксперименты по ``--only`` и ``--limit``."""
    chosen = list(experiments)
    if only is not None:
        chosen = [e for e in chosen if e.n in only]
    if limit is not None and limit >= 0:
        chosen = chosen[:limit]
    return chosen
