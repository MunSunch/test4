"""Параллельный запуск внешней CLI-утилиты по парам yml-файлов.

Пакет не тянет ни одной внешней зависимости и рассчитан на Python 3.9+,
чтобы работать на системном python3 в macOS без установки чего-либо.
"""

from __future__ import annotations

__all__ = ["config", "pairing", "command", "procs", "executor", "state", "pool"]
