"""Подбор файлов эксперимента по идентификатору, из разных папок."""

from __future__ import annotations

import unittest

from tests.support import Sandbox  # noqa: F401  (кладёт корень проекта в sys.path)

from batch_runner.config import ConfigError
from batch_runner.pairing import (
    base_dir_for,
    natural_key,
    number_of,
    parse_selection,
    pattern_to_regex,
    scan,
    select,
    split_pattern,
)

PATTERNS = {
    "config": "configs/config_{n}.yml",
    "enable": "affects/affect_enable_{n}.yml",
    "disable": "affects/affect_disable_{n}.yml",
}


class PatternTest(unittest.TestCase):
    def test_номер_с_кластером_и_без(self):
        regex = pattern_to_regex("configs/config_{n}.yml")
        self.assertEqual(regex.match("config_103.yml").group(1), "103")
        self.assertEqual(regex.match("config_103m5.yml").group(1), "103m5")
        self.assertEqual(regex.match("config_20m4.yml").group(1), "20m4")

    def test_идентификатор_начинается_с_цифры(self):
        regex = pattern_to_regex("configs/config_{n}.yml")
        self.assertIsNone(regex.match("config_m5.yml"))
        self.assertIsNone(regex.match("config_.yml"))

    def test_точка_не_является_любым_символом(self):
        regex = pattern_to_regex("configs/config_{n}.yml")
        self.assertIsNone(regex.match("config_103Xyml"))

    def test_совпадение_только_целиком(self):
        regex = pattern_to_regex("configs/config_{n}.yml")
        self.assertIsNone(regex.match("my_config_103m5.yml"))
        self.assertIsNone(regex.match("config_103m5.yml.bak"))

    def test_подчёркивание_в_хвост_не_попадает(self):
        # Иначе config_103m5_старый.yml тихо стал бы отдельным экспериментом.
        regex = pattern_to_regex("configs/config_{n}.yml")
        self.assertIsNone(regex.match("config_103m5_старый.yml"))

    def test_enable_и_disable_не_путаются(self):
        enable = pattern_to_regex(PATTERNS["enable"])
        disable = pattern_to_regex(PATTERNS["disable"])
        self.assertIsNotNone(enable.match("affect_enable_103m5.yml"))
        self.assertIsNone(enable.match("affect_disable_103m5.yml"))
        self.assertIsNotNone(disable.match("affect_disable_103m5.yml"))
        self.assertIsNone(disable.match("affect_enable_103m5.yml"))

    def test_паттерн_без_номера_отвергается(self):
        with self.assertRaises(ConfigError):
            pattern_to_regex("configs/config.yml")

    def test_два_номера_отвергаются(self):
        with self.assertRaises(ConfigError):
            pattern_to_regex("config_{n}_{n}.yml")

    def test_разбор_папки_и_имени(self):
        self.assertEqual(split_pattern("affects/affect_enable_{n}.yml"),
                         ("affects", "affect_enable_{n}.yml"))
        self.assertEqual(split_pattern("config_{n}.yml"), ("", "config_{n}.yml"))

    def test_абсолютный_путь_в_паттерне_уводит_из_корня(self):
        from pathlib import Path

        base = base_dir_for(Path("/данные").resolve(), "/совсем/другое/место/config_{n}.yml")
        self.assertTrue(str(base).endswith("место"))


class NaturalKeyTest(unittest.TestCase):
    def test_сортировка_по_числу_а_не_по_строке(self):
        # Числа сравниваются как числа и в номере, и в названии кластера:
        # 20 раньше 103, m5 раньше m10.
        ids = ["103", "20", "103m5", "103m10", "103m4", "9m4"]
        self.assertEqual(
            sorted(ids, key=natural_key),
            ["9m4", "20", "103", "103m4", "103m5", "103m10"],
        )

    def test_числовая_часть(self):
        self.assertEqual(number_of("103m5"), 103)
        self.assertEqual(number_of("20"), 20)


class SelectionTest(unittest.TestCase):
    def test_голое_число_берёт_все_кластеры(self):
        selection = parse_selection("103")
        from batch_runner.pairing import Experiment

        self.assertTrue(selection.matches(Experiment(n="103")))
        self.assertTrue(selection.matches(Experiment(n="103m4")))
        self.assertTrue(selection.matches(Experiment(n="103m5")))
        self.assertFalse(selection.matches(Experiment(n="104m5")))

    def test_полный_идентификатор_берёт_один_кластер(self):
        from batch_runner.pairing import Experiment

        selection = parse_selection("103m5")
        self.assertTrue(selection.matches(Experiment(n="103m5")))
        self.assertFalse(selection.matches(Experiment(n="103m4")))
        self.assertFalse(selection.matches(Experiment(n="103")))

    def test_диапазон_и_перечисление(self):
        from batch_runner.pairing import Experiment

        selection = parse_selection("103m4,105-107")
        self.assertTrue(selection.matches(Experiment(n="103m4")))
        self.assertFalse(selection.matches(Experiment(n="103m5")))
        self.assertTrue(selection.matches(Experiment(n="106m9")))
        self.assertFalse(selection.matches(Experiment(n="108")))

    def test_мусор_даёт_понятную_ошибку(self):
        with self.assertRaises(ConfigError):
            parse_selection("abc")


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def test_собирает_файлы_из_разных_папок(self):
        self.sandbox.make_experiments(["103m5"])
        experiments, warnings = scan(
            self.sandbox.inputs, PATTERNS, ["config", "enable", "disable"]
        )
        self.assertEqual(warnings, [])
        files = experiments[0].files
        self.assertEqual(experiments[0].n, "103m5")
        self.assertEqual(files["config"].parent.name, "configs")
        self.assertEqual(files["enable"].name, "affect_enable_103m5.yml")
        self.assertEqual(files["disable"].name, "affect_disable_103m5.yml")

    def test_один_номер_на_разных_кластерах_это_разные_эксперименты(self):
        self.sandbox.make_experiments(["103m4", "103m5"])
        experiments, warnings = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        self.assertEqual([e.n for e in experiments], ["103m4", "103m5"])
        # И файлы у них разные, а не общие.
        self.assertNotEqual(
            experiments[0].files["enable"], experiments[1].files["enable"]
        )

    def test_с_кластером_и_без_живут_рядом(self):
        self.sandbox.make_experiments(["103", "103m5"])
        experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        self.assertEqual([e.n for e in experiments], ["103", "103m5"])

    def test_сортировка_числовая_а_не_строковая(self):
        self.sandbox.make_experiments(["103m5", "20m4", "2", "103m4"])
        experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        self.assertEqual([e.n for e in experiments], ["2", "20m4", "103m4", "103m5"])

    def test_абсолютные_пути(self):
        self.sandbox.make_experiments(["103m5"])
        experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        for path in experiments[0].files.values():
            self.assertTrue(path.is_absolute(), path)

    def test_неполный_набор_даёт_предупреждение_а_не_тишину(self):
        self.sandbox.make_experiments(["103m4", "103m5"])
        (self.sandbox.inputs / "affects" / "affect_enable_103m5.yml").unlink()
        experiments, warnings = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        self.assertEqual([e.n for e in experiments], ["103m4"])
        self.assertTrue(any("103m5" in w and "enable" in w for w in warnings), warnings)

    def test_лишние_роли_не_требуются(self):
        # Шагу stop нужен только config — affect-файлы не должны мешать.
        self.sandbox.make_experiments(["103m5"], roles=("config",))
        experiments, warnings = scan(self.sandbox.inputs, PATTERNS, ["config"])
        self.assertEqual([e.n for e in experiments], ["103m5"])
        self.assertEqual(warnings, [])

    def test_отсутствующая_папка_роли_названа_явно(self):
        self.sandbox.make_experiments(["103m5"], roles=("config",))
        experiments, warnings = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        self.assertEqual(experiments, [])
        self.assertTrue(any("affects" in w for w in warnings), warnings)

    def test_непопадание_подсказывает_что_лежит_в_папке(self):
        configs = self.sandbox.inputs / "configs"
        configs.mkdir(parents=True)
        (configs / "конфиг103.yaml").write_text("x", encoding="utf-8")
        experiments, warnings = scan(self.sandbox.inputs, PATTERNS, ["config"])
        self.assertEqual(experiments, [])
        self.assertTrue(any("конфиг103.yaml" in w for w in warnings), warnings)

    def test_values_содержит_идентификатор_и_пути(self):
        self.sandbox.make_experiments(["103m5"])
        experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        values = experiments[0].values()
        self.assertEqual(values["n"], "103m5")
        self.assertTrue(values["config"].endswith("config_103m5.yml"))

    def test_рекурсивный_поиск(self):
        nested = self.sandbox.inputs / "configs" / "старое"
        nested.mkdir(parents=True)
        (nested / "config_105m4.yml").write_text("x", encoding="utf-8")
        плоско, _ = scan(self.sandbox.inputs, PATTERNS, ["config"])
        глубоко, _ = scan(self.sandbox.inputs, PATTERNS, ["config"], recursive=True)
        self.assertEqual(плоско, [])
        self.assertEqual([e.n for e in глубоко], ["105m4"])

    def test_роль_без_паттерна_это_ошибка(self):
        self.sandbox.make_experiments(["103m5"])
        with self.assertRaises(ConfigError):
            scan(self.sandbox.inputs, PATTERNS, ["config", "такойроли-нет"])


class SelectFilterTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.make_experiments(["103m4", "103m5", "104m4", "104m5", "105m5"])
        self.experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])

    def test_only_по_номеру_берёт_оба_кластера(self):
        chosen = select(self.experiments, only=parse_selection("103"))
        self.assertEqual([e.n for e in chosen], ["103m4", "103m5"])

    def test_only_по_кластеру_берёт_один(self):
        chosen = select(self.experiments, only=parse_selection("103m5,105m5"))
        self.assertEqual([e.n for e in chosen], ["103m5", "105m5"])

    def test_limit(self):
        chosen = select(self.experiments, limit=2)
        self.assertEqual([e.n for e in chosen], ["103m4", "103m5"])


if __name__ == "__main__":
    unittest.main()
