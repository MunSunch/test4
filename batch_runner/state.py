"""Журнал прогресса в JSONL — он же основа resume.

Ключ записи — пара (номер эксперимента, шаг), а не эксперимент целиком:
если create прошёл, а apply нет, повторный запуск не должен тратить
десять минут на пересоздание.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .executor import STATUS_OK, StepResult

Key = Tuple[int, str]


def read_records(path: Path) -> List[Dict[str, object]]:
    """Прочитать журнал, пропуская испорченные строки.

    Последняя строка вполне может оказаться обрезанной, если прошлый прогон
    убили ровно в момент записи. Это не повод терять весь журнал.
    """
    if not path.exists():
        return []
    records: List[Dict[str, object]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "n" in record and "step" in record:
                records.append(record)
    return records


def _key(record: Dict[str, object]) -> Optional[Key]:
    try:
        return int(record["n"]), str(record["step"])
    except (KeyError, TypeError, ValueError):
        return None


def completed_keys(records: List[Dict[str, object]]) -> Set[Key]:
    """Шаги, которые хоть раз завершились успешно."""
    done: Set[Key] = set()
    for record in records:
        if record.get("status") != STATUS_OK:
            continue
        key = _key(record)
        if key is not None:
            done.add(key)
    return done


def failed_keys(records: List[Dict[str, object]]) -> Set[Key]:
    """Шаги, которые пробовали и ни разу не довели до успеха."""
    attempted: Set[Key] = set()
    for record in records:
        key = _key(record)
        if key is not None:
            attempted.add(key)
    return attempted - completed_keys(records)


class Journal:
    """Дописывающий журнал, безопасный для записи из нескольких потоков."""

    def __init__(self, path: Path, out_root: Path) -> None:
        self.path = path
        self.out_root = out_root
        self._lock = threading.Lock()
        self._handle = None  # type: Optional[object]

    def __enter__(self) -> "Journal":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def append(self, result: StepResult) -> None:
        """Записать итог попытки и сразу сбросить на диск.

        fsync на каждую запись выглядит расточительно, но при десяти минутах
        на шаг это происходит раз в вечность, зато журнал переживает
        и Ctrl+C, и внезапную перезагрузку.
        """
        record = result.as_record(self.out_root)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            if self._handle is None:
                raise RuntimeError("журнал не открыт")
            self._handle.write(line + "\n")
            self._handle.flush()
            try:
                os.fsync(self._handle.fileno())
            except OSError:
                pass
