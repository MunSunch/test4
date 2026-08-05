"""Подбор файлов эксперимента по номеру, из разных папок."""

from __future__ import annotations

import unittest

from tests.support import Sandbox  # noqa: F401  (кладёт корень проекта в sys.path)

from batch_runner.config import ConfigError
from batch_runner.pairing import (
    base_dir_for,
    parse_number_spec,
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
    def test_точка_не_является_любым_символом(self):
        regex = pattern_to_regex("configs/config_{n}.yml")
        self.assertIsNotNone(regex.match("config_103.yml"))
        self.assertIsNone(regex.match("config_103Xyml"))

    def test_совпадение_только_целиком(self):
        regex = pattern_to_regex("configs/config_{n}.yml")
        self.assertIsNone(regex.match("my_config_103.yml"))
        self.assertIsNone(regex.match("config_103.yml.bak"))

    def test_enable_и_disable_не_путаются(self):
        # Имена отличаются одним словом в середине — регулярка должна
        # различать их, иначе аффект применится не тот.
        enable = pattern_to_regex(PATTERNS["enable"])
        disable = pattern_to_regex(PATTERNS["disable"])
        self.assertIsNotNone(enable.match("affect_enable_103.yml"))
        self.assertIsNone(enable.match("affect_disable_103.yml"))
        self.assertIsNotNone(disable.match("affect_disable_103.yml"))
        self.assertIsNone(disable.match("affect_enable_103.yml"))

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

        root = Path("/данные").resolve()
        absolute = "/совсем/другое/место/config_{n}.yml"
        base = base_dir_for(root, absolute)
        self.assertNotEqual(base, root)
        self.assertTrue(str(base).endswith("место"))


class NumberSpecTest(unittest.TestCase):
    def test_список_и_диапазон(self):
        self.assertEqual(parse_number_spec("103,105,107-109"), {103, 105, 107, 108, 109})

    def test_перевёрнутый_диапазон(self):
        self.assertEqual(parse_number_spec("105-103"), {103, 104, 105})

    def test_мусор_даёт_понятную_ошибку(self):
        with self.assertRaises(ConfigError):
            parse_number_spec("abc")


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def test_собирает_файлы_из_разных_папок(self):
        self.sandbox.make_experiments([103])
        experiments, warnings = scan(
            self.sandbox.inputs, PATTERNS, ["config", "enable", "disable"]
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(experiments), 1)
        files = experiments[0].files
        self.assertEqual(files["config"].parent.name, "configs")
        self.assertEqual(files["enable"].parent.name, "affects")
        self.assertEqual(files["enable"].name, "affect_enable_103.yml")
        self.assertEqual(files["disable"].name, "affect_disable_103.yml")

    def test_сортировка_числовая_а_не_строковая(self):
        self.sandbox.make_experiments([103, 104, 2, 20])
        experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        self.assertEqual([e.n for e in experiments], [2, 20, 103, 104])

    def test_абсолютные_пути(self):
        self.sandbox.make_experiments([103])
        experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        for path in experiments[0].files.values():
            self.assertTrue(path.is_absolute(), path)

    def test_непарный_файл_даёт_предупреждение_а_не_тишину(self):
        self.sandbox.make_experiments([103, 104])
        (self.sandbox.inputs / "affects" / "affect_enable_104.yml").unlink()
        experiments, warnings = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        self.assertEqual([e.n for e in experiments], [103])
        self.assertTrue(any("№104" in w and "enable" in w for w in warnings), warnings)

    def test_лишние_роли_не_требуются(self):
        # Шагу stop нужен только config — affect-файлы не должны мешать.
        self.sandbox.make_experiments([103], roles=("config",))
        experiments, warnings = scan(self.sandbox.inputs, PATTERNS, ["config"])
        self.assertEqual([e.n for e in experiments], [103])
        self.assertEqual(set(experiments[0].files), {"config"})
        self.assertEqual(warnings, [])

    def test_отсутствующая_папка_роли_названа_явно(self):
        self.sandbox.make_experiments([103], roles=("config",))
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

    def test_values_содержит_номер_и_пути(self):
        self.sandbox.make_experiments([103])
        experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])
        values = experiments[0].values()
        self.assertEqual(values["n"], "103")
        self.assertTrue(values["config"].endswith("config_103.yml"))
        self.assertTrue(values["enable"].endswith("affect_enable_103.yml"))

    def test_рекурсивный_поиск(self):
        nested = self.sandbox.inputs / "configs" / "старое"
        nested.mkdir(parents=True)
        (nested / "config_105.yml").write_text("x", encoding="utf-8")
        плоско, _ = scan(self.sandbox.inputs, PATTERNS, ["config"])
        глубоко, _ = scan(self.sandbox.inputs, PATTERNS, ["config"], recursive=True)
        self.assertEqual(плоско, [])
        self.assertEqual([e.n for e in глубоко], [105])

    def test_роль_без_паттерна_это_ошибка(self):
        self.sandbox.make_experiments([103])
        with self.assertRaises(ConfigError):
            scan(self.sandbox.inputs, PATTERNS, ["config", "такойроли-нет"])


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.sandbox.make_experiments(range(103, 108))
        self.experiments, _ = scan(self.sandbox.inputs, PATTERNS, ["config", "enable"])

    def test_only(self):
        chosen = select(self.experiments, only={104, 106})
        self.assertEqual([e.n for e in chosen], [104, 106])

    def test_limit(self):
        chosen = select(self.experiments, limit=2)
        self.assertEqual([e.n for e in chosen], [103, 104])


if __name__ == "__main__":
    unittest.main()
