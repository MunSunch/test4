"""Сквозные прогоны на заглушке вместо настоящей утилиты."""

from __future__ import annotations

import time
import unittest

from tests.support import Sandbox, env

from batch_runner.state import completed_keys, failed_keys, read_records


class BaseE2E(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)


class ParallelismTest(BaseE2E):
    def test_параллельный_прогон_заметно_быстрее_последовательного(self):
        numbers = range(103, 115)
        count, sleep, workers = len(list(numbers)), 0.8, 6
        self.sandbox.make_experiments(numbers)
        self.sandbox.write_config(workers=workers)

        with env(FAKE_STORM_SLEEP=sleep):
            started = time.monotonic()
            code, output = self.sandbox.run("--mode", "create")
            elapsed = time.monotonic() - started

        self.assertEqual(code, 0, output)
        sequential = count * sleep
        self.assertLess(
            elapsed,
            sequential * 0.6,
            "прогон занял {:.1f}с — это близко к последовательным {:.1f}с".format(
                elapsed, sequential
            ),
        )
        records = read_records(self.sandbox.state_path)
        self.assertEqual(len(completed_keys(records)), count)

    def test_логи_параллельных_запусков_не_перетирают_друг_друга(self):
        # Заглушка пишет storm.log по фиксированному имени, как настоящая
        # утилита. Без изоляции рабочих папок остался бы один файл на всех.
        numbers = list(range(103, 109))
        self.sandbox.make_experiments(numbers)
        self.sandbox.write_config(workers=6)

        with env(FAKE_STORM_SLEEP=0.3):
            code, output = self.sandbox.run("--mode", "create")

        self.assertEqual(code, 0, output)
        for number in numbers:
            log = self.sandbox.attempt_dir(number, "create") / "storm.log"
            self.assertTrue(log.is_file(), "нет лога для N={}".format(number))
            text = log.read_text(encoding="utf-8")
            self.assertIn("config_{}.yml".format(number), text)
            self.assertIn("эксперимент {}".format(number), text)


class StepsTest(BaseE2E):
    def test_каждый_шаг_получает_свой_affect_файл(self):
        self.sandbox.make_experiments([103])
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1):
            code, output = self.sandbox.run("--mode", "enable,disable")

        self.assertEqual(code, 0, output)
        enable_log = (self.sandbox.attempt_dir(103, "enable") / "storm.log").read_text(encoding="utf-8")
        disable_log = (self.sandbox.attempt_dir(103, "disable") / "storm.log").read_text(encoding="utf-8")
        self.assertIn("affect_enable_103.yml", enable_log)
        self.assertNotIn("affect_disable_103.yml", enable_log)
        self.assertIn("affect_disable_103.yml", disable_log)
        self.assertNotIn("affect_enable_103.yml", disable_log)

    def test_mode_all_проходит_все_четыре_шага(self):
        self.sandbox.make_experiments([103])
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1):
            code, output = self.sandbox.run("--mode", "all")

        self.assertEqual(code, 0, output)
        records = read_records(self.sandbox.state_path)
        self.assertEqual(
            completed_keys(records),
            {(103, "create"), (103, "enable"), (103, "disable"), (103, "stop")},
        )


class ResumeTest(BaseE2E):
    def test_повторный_запуск_пропускает_сделанное(self):
        self.sandbox.make_experiments([103, 104, 105])
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1):
            first_code, _ = self.sandbox.run("--mode", "create")
            second_code, second_output = self.sandbox.run("--mode", "create")

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertIn("Всё уже сделано", second_output)
        # Второго захода на диск не случилось.
        self.assertTrue(self.sandbox.attempt_dir(103, "create", 1).is_dir())
        self.assertFalse(self.sandbox.attempt_dir(103, "create", 2).exists())

    def test_force_прогоняет_заново(self):
        self.sandbox.make_experiments([103])
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1):
            self.sandbox.run("--mode", "create")
            code, output = self.sandbox.run("--mode", "create", "--force")

        self.assertEqual(code, 0, output)
        records = read_records(self.sandbox.state_path)
        self.assertEqual(len(records), 2)

    def test_незаконченный_шаг_доделывается(self):
        self.sandbox.make_experiments([103, 104])
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1):
            self.sandbox.run("--mode", "create")
            code, output = self.sandbox.run("--mode", "create,enable")

        self.assertEqual(code, 0, output)
        records = read_records(self.sandbox.state_path)
        # create не повторялся, enable выполнен для обоих.
        self.assertEqual(
            completed_keys(records),
            {(103, "create"), (104, "create"), (103, "enable"), (104, "enable")},
        )
        self.assertFalse(self.sandbox.attempt_dir(103, "create", 2).exists())


class FailureTest(BaseE2E):
    def test_ретраи_и_отчёт(self):
        self.sandbox.make_experiments([103, 104, 105])
        self.sandbox.write_config(attempts=2, retry_delay_sec=0)

        with env(FAKE_STORM_SLEEP=0.1, FAKE_STORM_FAIL_FOR="105"):
            code, output = self.sandbox.run("--mode", "create")

        self.assertEqual(code, 1, output)
        self.assertTrue(self.sandbox.attempt_dir(105, "create", 1).is_dir())
        self.assertTrue(self.sandbox.attempt_dir(105, "create", 2).is_dir())
        self.assertFalse(self.sandbox.attempt_dir(103, "create", 2).exists())

        records = read_records(self.sandbox.state_path)
        self.assertEqual(failed_keys(records), {(105, "create")})

        report = (self.sandbox.out / "report.txt").read_text(encoding="utf-8")
        self.assertIn("N=105", report)
        self.assertIn("create", report)

    def test_провал_шага_отменяет_следующие(self):
        # Нет смысла применять аффект к эксперименту, который не создался.
        self.sandbox.make_experiments([103, 104])
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1, FAKE_STORM_FAIL_FOR="104"):
            code, output = self.sandbox.run("--mode", "create,enable")

        self.assertEqual(code, 1, output)
        self.assertTrue(self.sandbox.attempt_dir(103, "enable", 1).is_dir())
        self.assertFalse(self.sandbox.attempt_dir(104, "enable", 1).exists())

    def test_fail_regex_ловит_ошибку_при_нулевом_коде_возврата(self):
        self.sandbox.make_experiments([103])
        self.sandbox.write_config(run={"fail_regex": "ОШИБКА"})

        with env(FAKE_STORM_SLEEP=0.1, FAKE_STORM_FAIL_TEXT="ОШИБКА: не сошлось"):
            code, output = self.sandbox.run("--mode", "create")

        self.assertEqual(code, 1, output)
        records = read_records(self.sandbox.state_path)
        self.assertEqual(records[0]["rc"], 0)
        self.assertIn("ОШИБКА", str(records[0]["reason"]))


class TimeoutTest(BaseE2E):
    def test_таймаут_убивает_и_порождённых_потомков(self):
        child_sleep = 3.0
        self.sandbox.make_experiments([103])
        self.sandbox.write_config(steps={"create": {"timeout_sec": 1}})

        with env(
            FAKE_STORM_SLEEP=30,
            FAKE_STORM_SPAWN_CHILD="1",
            FAKE_STORM_CHILD_SLEEP=child_sleep,
        ):
            started = time.monotonic()
            code, output = self.sandbox.run("--mode", "create")
            elapsed = time.monotonic() - started

        self.assertEqual(code, 1, output)
        self.assertLess(elapsed, 20, "таймаут не сработал, ждали всю заглушку")

        records = read_records(self.sandbox.state_path)
        self.assertEqual(records[0]["status"], "timeout")

        # Потомок должен был умереть вместе с родителем: если он выжил,
        # то через child_sleep секунд оставит после себя файл.
        marker = self.sandbox.attempt_dir(103, "create") / "child_survived.txt"
        time.sleep(child_sleep + 1.5)
        self.assertFalse(marker.exists(), "потомок пережил убийство дерева процессов")


class DryRunTest(BaseE2E):
    def test_ничего_не_запускает_и_показывает_команды(self):
        self.sandbox.make_experiments([103, 104])
        self.sandbox.write_config()

        code, output = self.sandbox.run("--mode", "create,enable,disable", "--dry-run")

        self.assertEqual(code, 0, output)
        self.assertIn("config_103.yml", output)
        self.assertIn("affect_enable_103.yml", output)
        self.assertIn("affect_disable_103.yml", output)
        self.assertIn("Всего запусков утилиты: 6", output)
        self.assertFalse((self.sandbox.out / "runs").exists())
        self.assertFalse(self.sandbox.state_path.exists())


class SelectionTest(BaseE2E):
    def test_only_и_limit(self):
        self.sandbox.make_experiments(range(103, 113))
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1):
            code, output = self.sandbox.run("--mode", "create", "--only", "104,107-108")

        self.assertEqual(code, 0, output)
        records = read_records(self.sandbox.state_path)
        self.assertEqual({r["n"] for r in records}, {104, 107, 108})

    def test_шаг_stop_не_требует_affect_файлов(self):
        self.sandbox.make_experiments([103], roles=("config",))
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1):
            code, output = self.sandbox.run("--mode", "stop")

        self.assertEqual(code, 0, output)
        records = read_records(self.sandbox.state_path)
        self.assertEqual(completed_keys(records), {(103, "stop")})

    def test_only_failed_берёт_только_упавшее(self):
        self.sandbox.make_experiments([103, 104, 105])
        self.sandbox.write_config()

        with env(FAKE_STORM_SLEEP=0.1, FAKE_STORM_FAIL_FOR="104"):
            self.sandbox.run("--mode", "create")
        with env(FAKE_STORM_SLEEP=0.1):
            code, output = self.sandbox.run("--mode", "create", "--only-failed")

        self.assertEqual(code, 0, output)
        self.assertTrue(self.sandbox.attempt_dir(104, "create", 2).is_dir())
        self.assertFalse(self.sandbox.attempt_dir(103, "create", 2).exists())


class ValidationTest(BaseE2E):
    def test_неизвестный_шаг_даёт_понятную_ошибку(self):
        self.sandbox.make_experiments([103])
        self.sandbox.write_config()
        code, output = self.sandbox.run("--mode", "нетакого")
        self.assertEqual(code, 2)
        self.assertIn("нетакого", output)

    def test_опечатка_в_плейсхолдере_не_уезжает_в_команду(self):
        # {enabel} иначе ушёл бы в утилиту литеральной строкой.
        self.sandbox.make_experiments([103])
        self.sandbox.write_config(
            steps={"create": {"args": ["-c", "{config}", "-f", "{enabel}"]}}
        )
        code, output = self.sandbox.run("--mode", "create")
        self.assertEqual(code, 2)
        self.assertIn("{enabel}", output)
        self.assertFalse((self.sandbox.out / "runs").exists())

    def test_ненайденная_утилита_останавливает_до_запуска(self):
        self.sandbox.make_experiments([103])
        self.sandbox.write_config(exe="такой-утилиты-точно-нет-12345")
        code, output = self.sandbox.run("--mode", "create")
        self.assertEqual(code, 2)
        self.assertFalse((self.sandbox.out / "runs").exists())

    def test_пустая_папка_не_запускает_прогон(self):
        self.sandbox.write_config()
        code, output = self.sandbox.run("--mode", "create")
        self.assertEqual(code, 2)
        self.assertIn("Нечего запускать", output)

    def test_опечатка_в_пути_конфига_не_проходит_молча(self):
        # Иначе прогон пошёл бы на встроенных дефолтах — с чужой командой.
        self.sandbox.make_experiments([103])
        self.sandbox.write_config()
        self.sandbox.config_path = self.sandbox.root / "canfig.json"
        code, output = self.sandbox.run("--mode", "create")
        self.assertEqual(code, 2)
        self.assertIn("canfig.json", output)


if __name__ == "__main__":
    unittest.main()
