"""Пример из папки example/ должен работать как есть.

Пример — первое, что запускают, и первое, что тихо устаревает после правок
в паттернах или шагах. Поэтому он прогоняется теми же тестами.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT, env, run_main

EXAMPLE = ROOT / "example"
IDS = ("103m4", "103m5", "104m5")
STEPS = ("create", "enable", "disable", "stop")


class ExampleLayoutTest(unittest.TestCase):
    def test_файлы_разложены_как_описано(self):
        for experiment_id in IDS:
            path = EXAMPLE / "configs" / "config_{}.yml".format(experiment_id)
            self.assertTrue(path.is_file(), path)
            for action in ("enable", "disable"):
                path = EXAMPLE / "affects" / "affect_{}_{}.yml".format(action, experiment_id)
                self.assertTrue(path.is_file(), path)

    def test_пример_показывает_один_номер_на_двух_кластерах(self):
        self.assertIn("103m4", IDS)
        self.assertIn("103m5", IDS)

    def test_конфиг_примера_разбирается(self):
        from batch_runner import config as config_module

        cfg = config_module.load(EXAMPLE / "config.json")
        self.assertEqual(sorted(cfg.steps), sorted(STEPS))
        self.assertEqual(cfg.roles, {"config", "enable", "disable"})
        config_module.validate(cfg, list(STEPS), require_exe=False)


class ExampleRunTest(unittest.TestCase):
    """Прогон примера целиком, с подменой exe на текущий интерпретатор.

    В самом config.json стоит "python3" — так правильно для macOS, но на
    Windows такой команды может не быть, поэтому тест подставляет sys.executable.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="storm_example_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        raw = json.loads((EXAMPLE / "config.json").read_text(encoding="utf-8"))
        raw["exe"] = sys.executable
        self.config_path = self.tmp / "config.json"
        self.config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    def test_dry_run_показывает_обе_папки(self):
        code, output = run_main(
            "--dir", str(EXAMPLE),
            "--config", str(self.config_path),
            "--out", str(self.tmp / "out"),
            "--mode", "all",
            "--dry-run",
        )
        self.assertEqual(code, 0, output)
        self.assertIn("configs", output)
        self.assertIn("affects", output)
        self.assertIn("config_103m4.yml", output)
        self.assertIn("affect_enable_103m4.yml", output)
        self.assertIn("affect_disable_103m4.yml", output)
        self.assertIn("Всего запусков утилиты: 12", output)

    def test_прогон_примера_проходит_целиком(self):
        from batch_runner.state import completed_keys, read_records

        out = self.tmp / "out"
        with env(FAKE_STORM_SLEEP=0.1):
            code, output = run_main(
                "--dir", str(EXAMPLE),
                "--config", str(self.config_path),
                "--out", str(out),
                "--mode", "all",
            )
        self.assertEqual(code, 0, output)

        records = read_records(out / "state.jsonl")
        expected = {(n, step) for n in IDS for step in STEPS}
        self.assertEqual(completed_keys(records), expected)

    def test_плейсхолдер_root_разворачивается_в_путь_к_программе(self):
        # Без {root} пример не запустился бы ни на одной чужой машине.
        code, output = run_main(
            "--dir", str(EXAMPLE),
            "--config", str(self.config_path),
            "--out", str(self.tmp / "out"),
            "--mode", "create",
            "--dry-run",
        )
        self.assertEqual(code, 0, output)
        self.assertNotIn("{root}", output)
        self.assertIn(str(ROOT), output)
        self.assertIn("fake_storm.py", output)


if __name__ == "__main__":
    unittest.main()
