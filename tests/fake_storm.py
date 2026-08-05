#!/usr/bin/env python3
"""Заглушка вместо настоящей утилиты: тот же интерфейс, но за доли секунды.

Настоящая утилита думает десять минут, поэтому проверять на ней логику
оркестратора невозможно. Заглушка ведёт себя так же в том, что для нас важно:
читает переданные ей файлы, тратит время, **пишет свой лог в текущую рабочую
папку по фиксированному имени** и возвращает код возврата.

Поведение управляется переменными окружения:

    FAKE_STORM_SLEEP        сколько секунд работать (по умолчанию 0.2)
    FAKE_STORM_RC           какой код возврата вернуть (по умолчанию 0)
    FAKE_STORM_FAIL_FOR     номера экспериментов, на которых падать: "3,8"
    FAKE_STORM_FAIL_TEXT    напечатать эту строку в лог, но вернуть 0
    FAKE_STORM_SPAWN_CHILD  породить долгоживущего потомка (проверка kill-tree)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

#: Фиксированное имя — ровно та ловушка, от которой защищает isolate_cwd.
LOG_NAME = "storm.log"
CHILD_MARKER = "child_survived.txt"


def experiment_number(path: Path) -> int:
    match = re.search(r"(\d+)", path.name)
    return int(match.group(1)) if match else -1


def spawn_child(seconds: float) -> None:
    """Долгоживущий потомок, который оставляет след, если его не убили."""
    marker = Path.cwd() / CHILD_MARKER
    code = (
        "import sys,time,pathlib;"
        "time.sleep(float(sys.argv[1]));"
        "pathlib.Path(sys.argv[2]).write_text('alive')"
    )
    subprocess.Popen(
        [sys.executable, "-c", code, str(seconds), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-c", dest="config", required=True)
    parser.add_argument("-f", dest="affect", default=None)
    parser.add_argument("-u", dest="update", action="store_true")
    parser.add_argument("-e", dest="experiment", action="store_true")
    args = parser.parse_args()

    lines = ["fake_storm argv: " + " ".join(sys.argv[1:])]

    config_path = Path(args.config)
    if not config_path.is_file():
        sys.stderr.write("не найден config: {}\n".format(args.config))
        return 66
    if not config_path.is_absolute():
        sys.stderr.write("ожидался абсолютный путь, получен {}\n".format(args.config))
        return 65
    lines.append("config: {} -> {}".format(config_path, config_path.read_text(encoding="utf-8").strip()))

    number = experiment_number(config_path)

    if args.affect is not None:
        affect_path = Path(args.affect)
        if not affect_path.is_file():
            sys.stderr.write("не найден affect: {}\n".format(args.affect))
            return 67
        lines.append("affect: {} -> {}".format(affect_path, affect_path.read_text(encoding="utf-8").strip()))

    if os.environ.get("FAKE_STORM_SPAWN_CHILD"):
        spawn_child(float(os.environ.get("FAKE_STORM_CHILD_SLEEP", "5")))
        lines.append("порождён потомок")

    time.sleep(float(os.environ.get("FAKE_STORM_SLEEP", "0.2")))

    rc = int(os.environ.get("FAKE_STORM_RC", "0"))
    fail_for = {
        int(part)
        for part in os.environ.get("FAKE_STORM_FAIL_FOR", "").split(",")
        if part.strip().isdigit()
    }
    if number in fail_for:
        rc = rc or 3
        lines.append("падаю на эксперименте {}".format(number))

    fail_text = os.environ.get("FAKE_STORM_FAIL_TEXT")
    if fail_text:
        lines.append(fail_text)

    lines.append("готово, код возврата {}".format(rc))
    body = "\n".join(lines) + "\n"

    # Лог в рабочую папку — по фиксированному имени, как настоящая утилита.
    (Path.cwd() / LOG_NAME).write_text(body, encoding="utf-8")
    sys.stdout.write(body)
    return rc


if __name__ == "__main__":
    sys.exit(main())
