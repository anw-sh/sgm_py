#!/usr/bin/env python3
"""Main script: run several modules in order, one module, or a single step.

    run_pipeline.py run all -b all_data -t 8            # every module, in MODULES order
    run_pipeline.py run qc taxonomy --profile human_taxonomy --tool kraken
    run_pipeline.py qc --profile other_mag              # one module
    run_pipeline.py bbduk -b all_data                   # one step
    run_pipeline.py COMMAND -h                          # options of a command

"""

from __future__ import annotations

import argparse
import sys
from types import ModuleType

import common
import generate_qc_reports
import run_bbduk
import run_fastp
import run_hostile
import run_kraken
import run_mpa
import run_qc_sg
import run_taxonomy
from rich.markup import escape

logger = common.logger

# Modules in the order `run all` runs them: each one reads what the previous one wrote.
MODULES: dict[str, ModuleType] = {
    "qc": run_qc_sg,
    "taxonomy": run_taxonomy,
}
# Single steps, for reruns or custom orders.
STEPS: dict[str, ModuleType] = {
    "qc_reports": generate_qc_reports,
    "bbduk": run_bbduk,
    "fastp": run_fastp,
    "hostile": run_hostile,
    "kraken": run_kraken,
    "metaphlan": run_mpa,
}


def selected_modules(names: list[str]) -> list[str]:
    """Modules to run, in MODULES order whatever order they were given in."""
    return list(MODULES) if "all" in names else [name for name in MODULES if name in names]


def run_modules(args: argparse.Namespace) -> int:
    """Run the selected modules one after the other; stop at the first one that does not succeed."""
    names = selected_modules(args.modules)
    for i, name in enumerate(names, start=1):
        common.console.rule(f"[bold]Module {i}/{len(names)}: {escape(name)}[/bold]", characters="#")
        code: int = MODULES[name].run(args)
        if code != 0:
            rest = names[i:]
            later = f" Not run: {', '.join(rest)}." if rest else ""
            logger.error("%s Module %s did not finish (exit code %d).%s Fix it and rerun with --resume.", common.EMOJI_CROSS, name, code, later)
            return code
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    chain = commands.add_parser(
        "run",
        help=f"Run modules in order: {' -> '.join(MODULES)} (or 'all').",
        description="Run the selected modules in this order: " + " -> ".join(MODULES) + ". Options of all modules are accepted.",
    )
    chain.add_argument("modules", nargs="+", choices=[*MODULES, "all"], metavar="MODULE", help=f"One or more of: {', '.join(MODULES)}, or all.")
    common.add_common_args(chain)
    for script in MODULES.values():
        script.add_args(chain)
    chain.set_defaults(func=run_modules)

    for name, script in {**MODULES, **STEPS}.items():
        command = commands.add_parser(name, help=script.DESCRIPTION, description=script.DESCRIPTION)
        common.add_common_args(command)
        script.add_args(command)
        command.set_defaults(func=script.run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())