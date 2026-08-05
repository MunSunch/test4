"""Журнал прогресса и логика возобновления."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT  # noqa: F401  (кладёт корень проекта в sys.path)

from batch_runner.executor import STATUS_FAIL, STATUS_OK, STATUS_TIMEOUT, StepResult
from batch_runner.state import Journal, completed_keys, failed_keys, read_records


def result(n: int, step: str, status: str, out_root: Path, attempt: int = 1) -> StepResult:
    return StepResult(
        n=n,
        step=step,
        status=status,
        rc=0 if status == STATUS_OK else 3,
        attempt=attempt,
        seconds=1.5,
        workdir=out_root / "runs" / str(n) / step / "attempt{}".format(attempt),
        started="2026-08-05T10:00:00",
    )


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="storm_state_"))
        self.path = self.tmp / "state.jsonl"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_запись_и_чтение(self):
        with Journal(self.path, self.tmp) as journal:
            journal.append(result(7, "create", STATUS_OK, self.tmp))
            journal.append(result(8, "create", STATUS_FAIL, self.tmp))
        records = read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["n"], 7)
        self.assertEqual(records[0]["status"], STATUS_OK)

    def test_путь_в_журнале_относительный(self):
        # Журнал должен переживать переезд папки с результатами.
        with Journal(self.path, self.tmp) as journal:
            journal.append(result(7, "create", STATUS_OK, self.tmp))
        record = read_records(self.path)[0]
        self.assertEqual(record["dir"], str(Path("runs") / "7" / "create" / "attempt1"))

    def test_ключ_различает_шаги(self):
        with Journal(self.path, self.tmp) as journal:
            journal.append(result(7, "create", STATUS_OK, self.tmp))
            journal.append(result(7, "apply", STATUS_FAIL, self.tmp))
        records = read_records(self.path)
        self.assertEqual(completed_keys(records), {(7, "create")})
        self.assertEqual(failed_keys(records), {(7, "apply")})

    def test_успех_после_провала_считается_выполненным(self):
        with Journal(self.path, self.tmp) as journal:
            journal.append(result(7, "create", STATUS_FAIL, self.tmp, attempt=1))
            journal.append(result(7, "create", STATUS_OK, self.tmp, attempt=2))
        records = read_records(self.path)
        self.assertEqual(completed_keys(records), {(7, "create")})
        self.assertEqual(failed_keys(records), set())

    def test_таймаут_считается_провалом(self):
        with Journal(self.path, self.tmp) as journal:
            journal.append(result(9, "create", STATUS_TIMEOUT, self.tmp))
        records = read_records(self.path)
        self.assertEqual(failed_keys(records), {(9, "create")})

    def test_битая_последняя_строка_не_роняет_чтение(self):
        # Прошлый прогон убили ровно в момент записи — это не повод
        # потерять весь журнал.
        with Journal(self.path, self.tmp) as journal:
            journal.append(result(1, "create", STATUS_OK, self.tmp))
            journal.append(result(2, "create", STATUS_OK, self.tmp))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"n": 3, "step": "cre')
        records = read_records(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(completed_keys(records), {(1, "create"), (2, "create")})

    def test_отсутствующий_файл_это_пустой_журнал(self):
        self.assertEqual(read_records(self.tmp / "нет.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
