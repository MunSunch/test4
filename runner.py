#!/usr/bin/env python3
"""Параллельный запуск CLI-утилиты по парам yml-файлов.

    python3 runner.py --dir /Users/munir/exp --mode create

Что именно запускается, описано в config.json — код под конкретную утилиту
править не нужно.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Set

from batch_runner import config as config_module
from batch_runner import pairing, state
from batch_runner.command import quote_for_display
from batch_runner.config import Config, ConfigError
from batch_runner.executor import STATUS_OK, build_step_argv, workdir_for
from batch_runner.pool import Runner, build_report, fmt_duration
from batch_runner.state import Journal

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DRY_RUN_SAMPLES = 3


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        description="Параллельно гоняет внешнюю утилиту по парам yml-файлов.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Порядок ввода в эксплуатацию:\n"
            "  1) --dry-run            сверить команды глазами\n"
            "  2) --only 1 --mode all  один настоящий эксперимент\n"
            "  3) полный прогон\n"
        ),
    )
    parser.add_argument("--dir", required=True, type=Path, help="папка с yml-файлами")
    parser.add_argument(
        "--mode",
        default="create",
        help='какие шаги выполнять: имена через запятую ("create,apply") '
        'или "all" — все шаги из конфига по порядку (по умолчанию: create)',
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="путь к config.json")
    parser.add_argument("--out", type=Path, default=Path("storm_runs"), help="куда складывать журнал и логи")
    parser.add_argument("--state", type=Path, default=None, help="путь к state.jsonl (по умолчанию внутри --out)")
    parser.add_argument("--workers", type=int, default=None, help="сколько запусков держать одновременно")
    parser.add_argument("--timeout", type=int, default=None, help="переопределить таймаут шага, секунды")
    parser.add_argument("--attempts", type=int, default=None, help="сколько попыток на шаг")
    parser.add_argument(
        "--only",
        default=None,
        help="только эти эксперименты: 103,105m4,107-109. Голое число берёт "
        "эксперимент на всех кластерах, 105m4 — только на этом",
    )
    parser.add_argument("--limit", type=int, default=None, help="взять только первые N экспериментов")
    parser.add_argument("-r", "--recursive", action="store_true", help="искать yml и в подпапках")
    parser.add_argument("--force", action="store_true", help="игнорировать журнал, прогнать всё заново")
    parser.add_argument("--only-failed", action="store_true", help="взять только то, что раньше не прошло")
    parser.add_argument("--dry-run", action="store_true", help="показать команды и выйти")
    return parser.parse_args(argv)


def resolve_steps(cfg: Config, mode: str) -> List[str]:
    """Превратить --mode в список шагов в порядке выполнения."""
    text = mode.strip()
    if text in ("all", "*"):
        return list(cfg.steps)
    names = [part.strip() for part in text.split(",") if part.strip()]
    if not names:
        raise ConfigError("--mode пустой")
    for name in names:
        cfg.step(name)  # бросит ConfigError со списком известных шагов
    return names


def apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    """Аргументы CLI перекрывают конфиг."""
    if args.workers is not None:
        cfg.workers = args.workers
    if args.attempts is not None:
        cfg.attempts = args.attempts


def print_dry_run(
    cfg: Config,
    steps: Sequence[str],
    experiments: Sequence["pairing.Experiment"],
    out_root: Path,
    input_dir: Path,
) -> None:
    print("Команды, которые будут запущены (первые {}):".format(min(DRY_RUN_SAMPLES, len(experiments))))
    for experiment in list(experiments)[:DRY_RUN_SAMPLES]:
        print("\n  N={}".format(experiment.n))
        for step_name in steps:
            step = cfg.step(step_name)
            workdir = workdir_for(out_root, experiment.n, step_name, 1)
            argv = build_step_argv(cfg, step, experiment, workdir, input_dir, out_root)
            print("    {:<8} {}".format(step_name, quote_for_display(argv)))
            if cfg.run.isolate_cwd:
                print("    {:<8} рабочая папка: {}".format("", workdir))
    remaining = len(experiments) - DRY_RUN_SAMPLES
    if remaining > 0:
        print("\n  ... и ещё {} экспериментов в том же виде".format(remaining))
    total_steps = len(experiments) * len(steps)
    print("\nВсего запусков утилиты: {} ({} экспериментов x {} шага)".format(
        total_steps, len(experiments), len(steps)
    ))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    try:
        # Отсутствующий config.json штатен только для пути по умолчанию:
        # тогда работаем на встроенных значениях. Если путь указали явно
        # и его нет — это опечатка, и молчать про неё нельзя.
        config_path = args.config  # type: Optional[Path]
        if not config_path.exists() and config_path.resolve() == DEFAULT_CONFIG:
            config_path = None
        cfg = config_module.load(config_path)
        apply_overrides(cfg, args)
        steps = resolve_steps(cfg, args.mode)
        warnings = config_module.validate(cfg, steps, require_exe=not args.dry_run)

        resolved_exe = config_module.resolve_exe(cfg.exe)
        if resolved_exe:
            cfg.exe = resolved_exe

        experiments, scan_warnings = pairing.scan(
            args.dir,
            cfg.patterns,
            cfg.required_roles(steps),
            recursive=args.recursive,
        )
        warnings.extend(scan_warnings)

        only = pairing.parse_selection(args.only) if args.only else None
        experiments = pairing.select(experiments, only=only, limit=args.limit)
    except ConfigError as exc:
        print("Ошибка: {}".format(exc), file=sys.stderr)
        return 2

    for warning in warnings:
        print("[warn ] {}".format(warning))

    if not experiments:
        print("Нечего запускать: не нашлось ни одного полного набора файлов.")
        return 2

    out_root = args.out.expanduser().resolve()
    input_dir = args.dir.expanduser().resolve()
    state_path = args.state if args.state is not None else out_root / "state.jsonl"

    records = [] if args.force else state.read_records(state_path)
    done_keys = state.completed_keys(records)
    failed = state.failed_keys(records)

    if args.only_failed:
        wanted = {e.n for e in experiments if any((e.n, s) in failed for s in steps)}
        experiments = [e for e in experiments if e.n in wanted]
        if not experiments:
            print("В журнале нет незавершённых шагов — нечего перезапускать.")
            return 0

    planned = [
        experiment
        for experiment in experiments
        if any((experiment.n, step) not in done_keys for step in steps)
    ]
    skipped_steps = sum(
        1 for e in experiments for s in steps if (e.n, s) in done_keys
    )

    if args.dry_run:
        print_dry_run(cfg, steps, planned or experiments, out_root, input_dir)
        return 0

    if not planned:
        print("Всё уже сделано: {} шагов отмечены успешными в {}".format(
            skipped_steps, state_path
        ))
        return 0

    out_root.mkdir(parents=True, exist_ok=True)

    print("Утилита  : {}".format(cfg.exe))
    print("Папка    : {}".format(input_dir))
    print("Шаги     : {}".format(", ".join(steps)))
    print("Найдено  : {} экспериментов".format(len(experiments)))
    if skipped_steps:
        print("Пропуск  : {} шагов уже выполнено ({})".format(skipped_steps, state_path))
    print("К работе : {} экспериментов, до {} одновременно".format(
        len(planned), min(cfg.workers, len(planned))
    ))
    print("Логи     : {}".format(out_root / "runs"))
    print("-" * 72)

    started = time.monotonic()
    with Journal(state_path, out_root) as journal:
        runner = Runner(
            cfg=cfg,
            step_names=steps,
            experiments=planned,
            out_root=out_root,
            input_dir=input_dir,
            journal=journal,
            done_keys=done_keys,
            timeout_override=args.timeout,
        )
        outcomes = runner.run()
    elapsed = time.monotonic() - started

    report = build_report(outcomes, warnings, elapsed, len(planned))
    print("-" * 72)
    print(report)

    report_path = out_root / "report.txt"
    try:
        report_path.write_text(report + "\n", encoding="utf-8")
        print("\nОтчёт: {}".format(report_path))
    except OSError as exc:
        print("Не удалось записать отчёт: {}".format(exc), file=sys.stderr)

    unfinished = len(planned) - len(outcomes)
    problems = sum(1 for o in outcomes if o.status != STATUS_OK)
    return 1 if (problems or unfinished) else 0


if __name__ == "__main__":
    sys.exit(main())
