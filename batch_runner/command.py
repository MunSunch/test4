"""Сборка командной строки из шаблона с плейсхолдерами.

Подстановка сделана регуляркой, а не ``str.format``: у утилиты в аргументах
вполне могут встретиться собственные фигурные скобки, и ``format`` на них
падает с KeyError. Здесь заменяются только известные ключи, всё остальное
остаётся как было.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

#: Служебные плейсхолдеры. Всё остальное — роли файлов из "patterns",
#: поэтому добавить новый вид yml можно правкой одного конфига.
RESERVED_PLACEHOLDERS = ("n", "dir", "taskdir", "outdir", "root")


def placeholders_in(args: Iterable[str]) -> Set[str]:
    """Множество имён плейсхолдеров, встречающихся в аргументах."""
    found = set()
    for arg in args:
        found.update(PLACEHOLDER_RE.findall(arg))
    return found


def render(args: Iterable[str], values: Dict[str, str]) -> List[str]:
    """Подставить значения в шаблон аргументов.

    Каждый аргумент обрабатывается отдельно и остаётся отдельным элементом
    списка — поэтому пути с пробелами не нуждаются в кавычках и не разъезжаются.
    """

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        return match.group(0)

    return [PLACEHOLDER_RE.sub(replace, arg) for arg in args]


def build_argv(exe: str, args: Iterable[str], values: Dict[str, str]) -> List[str]:
    """Полная команда: исполняемый файл плюс отрендеренные аргументы."""
    return [exe] + render(args, values)


def quote_for_display(argv: Iterable[str]) -> str:
    """Читаемое представление команды для ``--dry-run`` и логов.

    Только для показа человеку — на запуск уходит список argv, а не эта строка.
    """
    parts = []
    for arg in argv:
        if not arg or any(ch.isspace() for ch in arg) or '"' in arg:
            parts.append('"' + arg.replace('"', r"\"") + '"')
        else:
            parts.append(arg)
    return " ".join(parts)
