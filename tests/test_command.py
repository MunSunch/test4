"""Рендер шаблона команды."""

from __future__ import annotations

import unittest

from tests.support import ROOT  # noqa: F401  (кладёт корень проекта в sys.path)

from batch_runner.command import build_argv, placeholders_in, quote_for_display, render


class RenderTest(unittest.TestCase):
    def test_подставляет_известные_плейсхолдеры(self):
        args = ["-c", "{config}", "-f", "{affect}", "-e"]
        values = {"config": "/tmp/config-7.yml", "affect": "/tmp/affect_7.yml"}
        self.assertEqual(
            render(args, values),
            ["-c", "/tmp/config-7.yml", "-f", "/tmp/affect_7.yml", "-e"],
        )

    def test_незнакомые_скобки_остаются_нетронутыми(self):
        # У утилиты может быть собственный синтаксис со скобками —
        # str.format здесь упал бы с KeyError.
        args = ["--filter", "{{value}}", "--tpl", "{unknown}", "-c", "{config}"]
        result = render(args, {"config": "/tmp/c.yml"})
        self.assertEqual(result, ["--filter", "{{value}}", "--tpl", "{unknown}", "-c", "/tmp/c.yml"])

    def test_путь_с_пробелами_остаётся_одним_аргументом(self):
        result = render(["-c", "{config}"], {"config": "/Users/имя/мои файлы/c.yml"})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1], "/Users/имя/мои файлы/c.yml")

    def test_плейсхолдер_несколько_раз_в_одном_аргументе(self):
        result = render(["--out={outdir}/{n}/{n}.txt"], {"outdir": "/o", "n": "7"})
        self.assertEqual(result, ["--out=/o/7/7.txt"])

    def test_build_argv_ставит_утилиту_первой(self):
        argv = build_argv("storm", ["-c", "{config}"], {"config": "/tmp/c.yml"})
        self.assertEqual(argv, ["storm", "-c", "/tmp/c.yml"])


class PlaceholdersTest(unittest.TestCase):
    def test_находит_все_имена(self):
        found = placeholders_in(["-c", "{config}", "-o", "{outdir}/{n}.log"])
        self.assertEqual(found, {"config", "outdir", "n"})

    def test_пустой_список(self):
        self.assertEqual(placeholders_in([]), set())


class DisplayTest(unittest.TestCase):
    def test_кавычит_только_то_что_нужно(self):
        text = quote_for_display(["storm", "-c", "/a b/c.yml", "-e"])
        self.assertEqual(text, 'storm -c "/a b/c.yml" -e')


if __name__ == "__main__":
    unittest.main()
