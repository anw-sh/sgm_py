#!/usr/bin/env python3
"""Shared main loop of the run_* scripts: base_dir -> studies -> process_study()"""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import common
import inflect
from get_info import make_dict, names_list
from rich.console import Group
from rich.live import Live
from rich.markup import escape
from rich.traceback import install

logger = common.logger
console = common.console
panel = common.Panel

p = inflect.engine()

# (args, study_dir, study, studies, samples, utility_paths, status_subs) -> Return True on success
# Raises RuntimeError if a stage fails, and reports as the study failed
StudyFunc = Callable[
    [argparse.Namespace, Path, str, list[str], list[str], dict[str, str], Sequence[common.StatusLike]],
    bool,
]

# setup(args, utility_paths): runs once before the first study (e.g. choose a database). False = stop.
SetupFunc = Callable[[argparse.Namespace, dict[str, str]], bool]
# finish_base(args, base, studies, utility_paths): runs after all studies of a base dir succeeded.
FinishFunc = Callable[[argparse.Namespace, Path, list[str], dict[str, str]], bool]

def _terminate(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def run_studies(
    args: argparse.Namespace,
    header: str,
    label: str,
    required_keys: Sequence[str],
    process_study: StudyFunc,
    setup: SetupFunc | None = None,
    finish_base: FinishFunc | None = None,
) -> int:
    """Run process_study on every study found under args.base_dir.
    Returns an exit code (0 = all ok).

    setup and finish_base are optional hooks (see SetupFunc, FinishFunc). finish_base is skipped
    for a base dir with failed studies: fix them and rerun with --resume.

    `args` needs: base_dir, projects, samples, utility_paths, split_size, verbose, log_file (see common.add_common_args).
    `label` names the tool/stage in status lines.
    """
    common.setup_logging(args.verbose, args.log_file)
    install(show_locals=args.verbose)
    common.STOP.clear()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _terminate)
    common.print_header(header)

    utility_paths = make_dict(args.utility_paths)
    missing = [key for key in required_keys if key not in utility_paths]
    if missing:
        logger.error("%s %s is missing: %s", common.EMOJI_CROSS, escape(args.utility_paths), ", ".join(missing))
        return 1
    console.print(
        panel.fit(
            f"[dim]{common.EMOJI_SPARKLE} Found[/] [bold]{len(utility_paths)}[/] [dim]utility {p.plural('path', len(utility_paths))} in[/] {escape(args.utility_paths)}",
            title="Utility Paths",
            border_style="dim cyan",
        ),
        style="italic",
    )

    base_dirs = common.find_base_dirs(args.base_dir, args.projects)
    if not base_dirs:
        logger.error("%s Studies not found, is the provided %s directory correct?", common.EMOJI_CROSS, escape(args.base_dir))
        return 1

    if setup is not None and not setup(args, utility_paths):
        return 1

    # Per-split statuses
    status_subs = [console.status(f"[i][dim]Starting {escape(label)} on thread {i + 1}...[/dim][/i]") for i in range(args.split_size)]
    progress = common.make_progress()
    failed: list[str] = []

    try:
        with Live(panel(Group(progress, *status_subs)), console=console, transient=False):
            for base in base_dirs:
                base_tag = f"[cyan]{escape(base.name)}[/cyan]"
                console.rule(f"Running {escape(label)} on all studies in [b]{base_tag}[/]", characters="=", style="dim")

                studies = names_list(base / args.projects)
                console.print(
                    panel.fit(f"[dim]{common.EMOJI_SPARKLE} Processing[/] [bold]{len(studies)}[/] [dim]{p.plural("study", len(studies))} from[/] {base_tag} [dim]dir[/]", title="# Studies", border_style="dim cyan"),
                    style="italic",
                )
                task = progress.add_task(f"{common.EMOJI_PROCESS} [i][dim]Running {escape(label)} on[/dim] {base_tag} [dim]studies[/dim][/i]", total=len(studies))
                failed_before = len(failed)

                for study in studies:
                    console.rule(f"[dim][i]Running {escape(label)} on[/dim] {escape(study)}[/i]", characters="-", style="dim")
                    study_dir = base / study
                    samples = names_list(study_dir / args.samples)
                    if not samples:
                        logger.error("%s No samples found for %s, skipping...", common.EMOJI_CROSS, escape(study))
                        failed.append(study)
                    else:
                        logger.info("%s Project [bold blue]%s[/] has %d %s...", common.EMOJI_SPARKLE, escape(study), len(samples), p.plural("sample", len(samples)))
                        try:
                            if not process_study(args, study_dir, study, studies, samples, utility_paths, status_subs):
                                failed.append(study)
                        except RuntimeError as e:
                            logger.error("%s %s: %s", common.EMOJI_CROSS, escape(study), escape(str(e)))
                            failed.append(study)
                    progress.update(task, advance=1)

                if finish_base is not True:
                    if len(failed) > failed_before:
                        logger.warning(
                            "%s Skipping the final step for %s because %d studies failed. Fix them and rerun with [i pink3]--resume[/]", common.EMOJI_WARNING, escape(base.name), len(failed) - failed_before,
                        )
                    else:
                        try:
                            if not finish_base(args, base, studies, utility_paths):
                                failed.append(f"{base.name} (final step)")
                        except RuntimeError as e:
                            logger.error("%s %s: %s", common.EMOJI_CROSS, escape(base.name), escape(str(e)))
                            failed.append(f"{base.name} (final step)")

                console.rule(f"Completed {escape(label)} for all studies in [b]{base_tag}[/]", characters="=", style="dim")
    except KeyboardInterrupt:
        logger.warning("%s Interrupted. Samples that finished are kept; rerun with [i pink3]--resume[/] to continue", common.EMOJI_WARNING)
        return 130

    if failed:
        console.print(
            panel.fit(f"{common.EMOJI_CROSS} {escape(label)} failed for: {escape(', '.join(failed))}", title="Failed", border_style="red", title_align="right"),
            style="italic red",
        )
        return 1
    console.print(
        panel.fit(f"{common.EMOJI_CHECK} [bold green]Finished[/bold green] {escape(label)}.", title="Done", border_style="green", title_align="right"),
        style="italic",
    )
    return 0