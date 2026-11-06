#!/usr/bin/env python3
"""Shared helpers for the pipeline scripts: console, logging, subprocesses, sample splitting."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import filecmp
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from rich.console import Console
from rich.errors import MarkupError
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.pretty import Pretty
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

# Initiate Console() object
console = Console()

# Study name and Machine name
study_name: str = Path.cwd().name
sys_name: str = socket.gethostname()

# Emoji variables
EMOJI_CHECK = "[green]:heavy_check_mark:[/green]"
EMOJI_CROSS = "[red]:heavy_multiplication_x:[/red]"
EMOJI_DOWNLOAD = "[steel_blue]:arrow_down:[/steel_blue]"
EMOJI_LIST = "[magenta]:right_arrow:[/magenta]"
EMOJI_PLAY = "[white]:play_button:[/white]"
EMOJI_PROCESS = "[blue]:gear:[/blue]"
EMOJI_SPARKLE = "[cyan]:sparkle:[/cyan]"
EMOJI_TRASH = "[orange1]:wastebasket:[/orange1]"
EMOJI_WARNING = "[yellow]:warning:[/yellow]"
EMOJI_ZIP = "[purple]:compression:[/purple]"

# Conda run prefix with live output streaming
# --no-capture-output is unavailable for mamba, so using conda
ENV_RUNNER = "conda run --no-capture-output -n"

# Read-file patterns
READ_PATTERNS: tuple[str, ...] = (
    "{s}.*q.gz",
    "{s}_R1.*q.gz",
    "{s}_R2.*q.gz",
    "{s}_1.*q.gz",
    "{s}_2.*q.gz",
    "{s}_L*_R1_*.*q.gz",
    "{s}_L*_R2_*.*q.gz",
    "{s}_S*_L*_R1_*.*q.gz",
    "{s}_S*_L*_R2_*.*q.gz",
)

# Set on Ctrl+C / SIGTERM (or cancel button in WebUI): workers stop before starting the next sample
STOP = threading.Event()

# Terminal output lines classification (for run_command)
ERROR_RE = re.compile(r"\b(errors?|exceptions?|fatal|traceback|failed|failure)\b", re.IGNORECASE)
WARNING_RE = re.compile(r"\bwarn(ing)?s?\b", re.IGNORECASE)
IGNORE_RE = re.compile(
    r"\b(0|no|zero)\s+(errors?|warnings?)\b|\b(errors?|warnings?)\s*[:=]\s*0\b", re.IGNORECASE
)
LINE_STYLES: dict[int, str] = {
    logging.ERROR: "deep_pink3",
    logging.WARNING: "yellow2",
    logging.INFO: "dark_cyan",
}

# -----------------------------------------------
# Logging
# -----------------------------------------------
# Only configuring the "pipeline" logger. Not other python libraries' log
logger = logging.getLogger("pipeline")

# Tool output logs. Received by File and WebUI handlers, but not console_handler, as it prints live
CMD_LOGGER_NAME = "pipeline.cmd"
cmd_logger = logging.getLogger(CMD_LOGGER_NAME)

class PlainFormatter(logging.Formatter):
    """Format records as plain text for log files (Resolves rich markup and emoji codes)."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if record.name.startswith(CMD_LOGGER_NAME):
            return text    # raw tool output
        try:
            return console.render_str(text, markup=True, emoji=True).plain
        except MarkupError:
            return text

class _SkipToolOutput(logging.Filter):
    """Keep tool output out of the console handler, as it prints live."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(CMD_LOGGER_NAME)

# Call this function from each entry point
def setup_logging(verbose: bool = False, log_file: str | Path | None = None) -> None:
    """Attach a Rich console handler and an optional file handler."""

    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    console_handler = RichHandler(console=console, show_time=False, rich_tracebacks=True, markup=True)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.addFilter(_SkipToolOutput())
    console_handler.setFormatter(
        logging.Formatter("[%(asctime)s]: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(console_handler)

    if log_file is not None:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
        except OSError as e:
            logger.warning("%s Could not open log file: %s", EMOJI_WARNING, escape(str(e)))
            return
        file_handler.setFormatter(
            PlainFormatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(file_handler)

# -----------------------------------------------
# Rich objects
# -----------------------------------------------
def make_progress() -> Progress:
    """Progress bar for run_* scripts"""
    return Progress(
        SpinnerColumn(spinner_name="dots12", style="blue"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} completed in"),
        TimeElapsedColumn(),
        console=console,
        transient=False
    )

class StatusLike(Protocol):
    """What the pipeline functions need from a status display"""

    def update(self, status: str = "", *, spinner: str | None = None, spinner_style: str | None = None) -> None: ...

class NullStatus:
    """No-op status for non-terminal front-ends. Subclass to forward updates."""

    def update(self, status: str = "", *, spinner: str | None = None, spinner_style: str | None = None) -> None:
        return None


# -----------------------------------------------
# CLI helpers
# -----------------------------------------------
def print_header(message: str) -> None:
    """Start-of-script panel: Study name (title) and host name (subtitle)"""
    console.print(
        Panel(
            f"{EMOJI_SPARKLE} {message}", 
            title=f"Study: {study_name.upper()}",
            title_align="left",
            subtitle=f"Host: {sys_name}",
            subtitle_align="right",
            border_style="dim bold yellow",
            style="italic dim"
        )
    )

def set_waiting(status_subs: Sequence[StatusLike], message: str = "Waiting for the single thread process to finish") -> None:
    """Set the per-thread status lines while single-threaded task is running."""
    for status_sub in status_subs:
        status_sub.update(f"[dim]{message}[/]")

def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Arguments shared by all scripts"""
    parser.add_argument("-b", "--base_dir", default="./", help="Main directory with all the data. (Default: Current directory)")
    parser.add_argument("-s", "--samples", default="samples_list.txt", help="List of sample IDs as text file, just the file name, not its path. (Default: samples_list.txt)")
    parser.add_argument("-p", "--projects", default="studies_list.txt", help="List of project names as text file. (Default: studies_list.txt)")
    parser.add_argument("-u", "--utility_paths", default="utility_paths.csv", help="Envs or Paths for tools and DBs as a CSV file. (Default: utility_paths.csv)")
    parser.add_argument("-t", "--threads", type=int, default=1, help="Number of threads (Default: 1)")
    parser.add_argument("-l", "--split_size", type=int, choices=[1, 2, 3], nargs="?", default=1, help="Number of sub_lists to run concurrently (Default: 1, no splitting).")
    parser.add_argument("--resume", action="store_true", help="Continue a previous run: skip finished samples, redo the rest. Without it, stop if results of a previous run exist.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug messages.")
    parser.add_argument("--log_file", default=None, help="Also write log messages to this file.")

def find_base_dirs(base_dir: str | Path, projects_file: str) -> list[Path]:
    """Return [base_dir] if it holds the projects file, else its sub-directories that do (one sublevel)"""
    base = Path(base_dir).resolve()
    try:
        if (base / projects_file).is_file():
            logger.info("%s Found project file [bold blue]%s[/] in [bold blue]%s[/]", EMOJI_SPARKLE, projects_file, base.name)
            return [base]
        logger.info("%s [bold blue]%s[/] not in [bold blue]%s[/], searching subdirectories.", EMOJI_PROCESS, projects_file, base.name)
        return sorted(d for d in base.iterdir() if d.is_dir() and (d / projects_file).is_file())
    except OSError as e:
        logger.error("%s Cannot read %s: %s", EMOJI_CROSS, escape(str(base)), escape(str(e)))
        return []

def get_f_d_names(query_in: str | Path) -> tuple[str, str] | str:
    """(parent dir name, file name) for a file, or the directory name for a directory"""
    path = Path(query_in).resolve()
    if path.is_file():
        return path.parent.name, path.name
    if path.is_dir():
        return path.name
    raise FileNotFoundError(f"{query_in} is neither a file nor a directory")

# -----------------------------------------------
# Samples and reads
# -----------------------------------------------
def find_reads(reads_dir: str | Path, sample: str) -> list[Path]:
    """Read files of a sample (1 = Single End, 2 = Paired End)"""
    found: set[Path] = set()
    for pattern in READ_PATTERNS:
        found.update(Path(reads_dir).glob(pattern.format(s=sample)))
    return sorted(found)

def get_split_size(split_size: int, samples: list[str]) -> list[list[str]]:
    """Split samples into `split_size` lists (round-robin). Produces a list of lists, always"""
    if split_size < 1:
        raise ValueError(f"split_size must be >= 1, got {split_size}")
    return [samples[i::split_size] for i in range(split_size)]

# May not be required
def get_unique_items(in_list: Iterable[str], pattern: str | None = None) -> list[str]:
    """Sorted unique items"""
    items = [re.sub(pattern, "", item) for item in in_list] if pattern else list(in_list)
    return sorted(set(items))

# -----------------------------------------------
#  Resume support
# -----------------------------------------------
DONE_DIR = ".done"

def _marker(out_dir: Path, sample: str, suffix: str) -> Path:
    """Marker file of a sample; suffix separates several steps that share one out_dir"""
    return out_dir / DONE_DIR / (f"{sample}.{suffix}.json" if suffix else f"{sample}.json")

def is_done(out_dir: Path, sample: str, outputs: Sequence[Path], *, suffix: str = "") -> bool:
    """True if the sample's stage finished: indicates completion marker present and all outputs exist"""
    return _marker(out_dir, sample, suffix).is_file() and all(out.exists() for out in outputs)

def mark_done(out_dir: Path, sample: str, stage: str, command: str, *, suffix: str = "") -> None:
    """Record that a stage finished for the sample (provenance)"""
    done_dir = out_dir / DONE_DIR
    done_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "stage": stage, 
        "sample": sample, 
        "host": sys_name, 
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": command
    }
    marker = _marker(out_dir, sample, suffix)
    tmp = marker.with_name(f".{marker.name}.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(marker)

def prepare_redo(out_dir: Path, sample: str, outputs: Sequence[Path], *, suffix: str = "") -> None:
    """"Before (re)running a stage, remove partial outputs"""
    _marker(out_dir, sample, suffix).unlink(missing_ok=True)
    leftovers = [out for out in outputs if out.exists()]
    if leftovers:
        logger.warning("%s No completion marker for %s (partial or old output): redoing", EMOJI_WARNING, escape(sample))
    for out in leftovers:
        out.unlink()

def has_previous_results(out_dirs: Iterable[Path]) -> bool:
    """True if any directory holds complete outputs and marker from earlier runs"""
    for directory in out_dirs:
        done_dir = directory / DONE_DIR
        if (done_dir.is_dir() and any(done_dir.iterdir())) or (directory.is_dir() and any(directory.glob("*.gz"))):
            return True
    return False

def resume_allowed(args: argparse.Namespace, out_dirs: Sequence[Path], what: str) -> bool:
    """False if earlier results exist but --resume was not provided"""
    if args.resume or not has_previous_results(out_dirs):
        return True
    logger.error(
        "%s Results of a previous run found for [blue bold]%s[/] in [blue]%s[/]. Use [i pink3]--resume[/] to continue it or remove them to start over", EMOJI_CROSS, escape(what), escape(", ".join(d.name for d in out_dirs)),
    )
    return False

def link_filtered_reads(outputs: Sequence[Path], dest_dir: Path, sample: str) -> list[Path]:
    """Symlink a sample's final filtered reads (based on the profile) to a common input directory for the downstream commands"""
    names = [f"{sample}_R1.fq.gz", f"{sample}_R2.fq.gz"] if len(outputs) == 2 else [f"{sample}.fq.gz"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    links: list[Path] = []
    for source, name in zip(outputs, names, strict=True):
        link = dest_dir / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise FileExistsError(f"{link} exists, and is not a symlink")
        link.symlink_to(os.path.relpath(source, dest_dir))
        links.append(link)
    return links

# -----------------------------------------------
# System resources (Linux)
# -----------------------------------------------
@dataclass(frozen=True)
class SystemInfo:
    """Machine resources"""

    hostname: str
    cpus: int
    ram_total_gb: float
    ram_available_gb: float
    ram_limit_gb: float | None

    @property
    def usable_ram_gb(self) -> float:
        """"Available RAM the current process can use"""
        if self.ram_limit_gb is None:
            return self.ram_available_gb
        return min(self.ram_available_gb, self.ram_limit_gb)

def _meminfo_gb(field: str, path: Path = Path("/proc/meminfo")) -> float:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{field}:"):
                return int(line.split()[1]) / 1024**2
    except (OSError, ValueError, IndexError):
        pass
    return 0.0

CGROUP_MEMORY_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")

def _memory_limit_gb(env: Mapping[str, str] = os.environ, cgroup_files: Sequence[str] = CGROUP_MEMORY_FILES) -> float | None:
    """A memory limit imposed on this process, or None (the usual case on a workstation).
    Smallest of: SLURM_MEM_PER_NODE (MB, only inside a SLURM job) and the cgroup memory limits
    (containers, systemd, SLURM). Without any of them nothing is capped.
    """
    limits: list[float] = []
    slurm = env.get("SLURM_MEM_PER_NODE", "")
    if slurm.isdigit():
        limits.append(int(slurm) / 1024)
    for path in cgroup_files:
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 2**60:  # "max" / huge numbers mean unlimited
            limits.append(int(raw) / 1024**3)
    return min(limits) if limits else None

def _psutil_ram_gb() -> tuple[float, float]:
    """(total, available) GiB from psutil, for systems without /proc/meminfo (macOS, Windows). (0, 0) if unavailable."""
    try:
        import psutil
    except ImportError:
        return 0.0, 0.0
    memory = psutil.virtual_memory()
    return memory.total / 1024**3, memory.available / 1024**3

def get_system_info() -> SystemInfo:
    """Host name, usable cores (respects CPU affinity, e.g. a SLURM allocation) and RAM.
    
    Works on any Linux machine (workstation or HPC node): RAM comes from /proc/meminfo, where
    "available" is what can be used without swapping (free + reclaimable cache). SLURM and cgroup
    limits only apply when present. Other systems need psutil; without it RAM is reported as 0.
    """
    affinity = getattr(os, "sched_getaffinity", None)
    cpus = len(affinity(0)) if affinity is not None else (os.cpu_count() or 1)
    total, available = _meminfo_gb('MemTotal'), _meminfo_gb('MemAvailable')
    if total <= 0 or available <= 0:
        total, available = _psutil_ram_gb()
    return SystemInfo(sys_name, cpus, total, available, _memory_limit_gb())

def print_system_info(info: SystemInfo) -> None:
    """Panel with machine's resources"""
    limit = f", limit [bold]{info.ram_limit_gb:.0f}[/] GB" if info.ram_limit_gb is not None else ""
    console.print(
        Panel.fit(
            f"[dim]{EMOJI_SPARKLE} Host[/] [bold]{escape(info.hostname)}[/]: [bold]{info.cpus}[/] cores, "
            f"[bold]{info.ram_total_gb:.0f}[/] GB RAM ([bold]{info.usable_ram_gb:.0f}[/] GB free{limit})",
            title="System", border_style="dim cyan"
        ),
        style="italic",
    )

# -----------------------------------------------
# Running commands
# -----------------------------------------------
def expand_path(value: str) -> str:
    """Expand ~ and $VARS in a utility_paths.csv value, as the shell would.
 
    Needed because values are shell-quoted into commands, and quoting turns off the shell's
    own expansion ('~/db/idx' would be taken literally). Plain names are returned unchanged.
    """
    return os.path.expandvars(os.path.expanduser(value))

def env_command(env: str, command: str) -> str:
    """Wrap the command"""
    return f"{ENV_RUNNER} {env} {command}"

def classify_line(line: str) -> int:
    """Determining the lines as logging.ERROR/WARNING"""
    if IGNORE_RE.search(line):
        return logging.INFO
    if ERROR_RE.search(line):
        return logging.ERROR
    if WARNING_RE.search(line):
        return logging.WARNING
    return logging.INFO

def run_command(
        command: str,
        desc: str | None = None,
        check: bool = False,
        log_file: str | Path | None = None,
) -> int:
    """Run a shell command with colored stream, and returns exit code.
    Runs under `bash -o pipefail`, so that `tee` reports the piped tool's failure.
    Each line is logged to cmd_logger, for file/WebUI handlers.
    `check` raises CalledProcessError on a non-zero exit.
    """
    if STOP.is_set(): # safety net: a stage loop that forgot to check STOP must not start new tools
        logger.warning("%s Stop requested, not starting: %s", EMOJI_WARNING, escape(desc or command))
        return 130
    
    console.print(Panel(f"[yellow dim]$ {escape(command)}[/yellow dim]", border_style="dim", title=desc, expand=False))
    cmd_logger.info("$ %s", command)
    counts = {logging.WARNING: 0, logging.ERROR: 0}
    start = time.monotonic()

    with contextlib.ExitStack() as stack:
        log_fh = stack.enter_context(Path(log_file).open("w", encoding="utf-8")) if log_file else None
        process = stack.enter_context(
            subprocess.Popen(
                ["bash", "-o", "pipefail", "-c", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        )
        if process.stdout is not None:
            for raw in process.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                level = classify_line(line)
                if level in counts:
                    counts[level] += 1
                console.print(line, style=LINE_STYLES[level], markup=False, highlight=False)
                if log_fh is not None:
                    log_fh.write(line + "\n")
                cmd_logger.log(level, line)
        returncode = process.wait()

    minutes = (time.monotonic() - start) / 60
    if returncode == 0:
        console.print(f"{EMOJI_CHECK} Command finished in {minutes:.2f} minutes", style="italic")
    else:
        console.print(f"{EMOJI_CROSS} Command stopped after {minutes:.2f} minutes (exit code [red]{returncode}[/red])", style="italic")
        logger.error("Exit code %d: %s", returncode, escape(command))
    if counts[logging.WARNING] or counts[logging.ERROR]:
        console.print(
            f"{EMOJI_WARNING} Flagged lines: {counts[logging.WARNING]} warning(s), {counts[logging.ERROR]} error(s)", style="italic"
        )
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, command)
    return returncode

def run_concurrently(
        run_func: Callable[..., None],
        split_size: int,
        sample_lists: list[list[str]],
        status_subs: Sequence[StatusLike],
        *args: Any,
        stagger: float = 10.0,
        raise_on_error: bool = True,
) -> None:
    """Call run_func(sub_list, status_sub, *args) for each sub-list, in threads if split_size > 1.
    `stagger`: delay between thread starts (in seconds)
    `raise_on_error`: If any of the threads fail, Raise runtimeError after all workers finish - acts as a check to avoid partial data
    """
    if split_size == 1:
        logger.info("%s Executing as single process...", EMOJI_PROCESS)
        run_func(sample_lists[0], status_subs[0], *args)
        return

    logger.info("%s Initiating %d processes...", EMOJI_PROCESS, split_size)
    failures = 0
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=split_size)
    try:
        futures = []
        for i, sub_list in enumerate(sample_lists):
            if i > 0 and STOP.wait(stagger):  # slight delay to stagger starts; wakes up on a stop request
                break
            futures.append(executor.submit(run_func, sub_list, status_subs[i], *args))
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception:  # noqa: BLE001 - worker boundary: report here, decide below
                failures += 1
                logger.exception("%s Error in concurrent execution", EMOJI_CROSS)
    except KeyboardInterrupt:
        # Ctrl+C also reached the running tools; tell the workers not to start the next sample
        STOP.set()
        logger.warning("%s Interrupted: waiting for running tools to stop...", EMOJI_WARNING)
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if failures and raise_on_error:
        raise RuntimeError(f"{failures} of {split_size} worker(s) failed")

# -----------------------------------------------
# Per-sample steps with resume markers, 
# and merged tables (Kraken2, MetaPhlAn)
# -----------------------------------------------
def sample_tag(study: str, sample: str) -> str:
    return f"[blue]{escape(study)}[/blue] {EMOJI_PLAY} [magenta]{escape(sample)}[/magenta]"

def run_step(out_dir: Path, sample: str, step: str, outputs: list[Path], command: str, tag: str) -> bool:
    """One step of one sample with resume markers. True if succeeded or was already done"""
    if is_done(out_dir, sample, outputs, suffix=step):
        logger.info("%s %s already done for %s. Skipping...", EMOJI_CHECK, step, tag)
        return True
    prepare_redo(out_dir, sample, outputs, suffix=step)
    logger.info("%s Running %s on %s...", EMOJI_PROCESS, step, tag)
    returncode = run_command(command, desc=f"Running {step} on {sample}")
    if returncode != 0 or not all(out.exists() for out in outputs):
        for out in outputs:
            out.unlink(missing_ok=True)
        logger.error("%s %s failed for %s (exit code %d, or expected output missing)", EMOJI_CROSS, step, tag, returncode)
        return False
    mark_done(out_dir, sample, step, command, suffix=step)
    return True

def run_step_for(
    sub_list: list[str],
    status_sub: StatusLike,
    study: str,
    studies: list[str],
    out_dir: Path,
    step: str,
    verb: str,
    plan: Callable[[str], tuple[list[Path], str] | None],
) -> None:
    """Run one step for every sample. plan(sample) -> (outputs, command), or None if it cannot be planned
    (it logs why). Raises RuntimeError at the end if any sample failed."""
    st_count = f"{studies.index(study) + 1}/{len(studies)}"
    failed: list[str] = []
    for i, sample in enumerate(sub_list, start = 1):
        if STOP.is_set():
            break
        tag = sample_tag(study, sample)
        status_sub.update(f"[i][dim]{verb}[/dim] {tag}[dim]: ({i}/{len(sub_list)}) | ({st_count}) [/dim][/i]", spinner="point", spinner_style="magenta")
        planned = plan(sample)
        if planned is None or not run_step(out_dir, sample, step, planned[0], planned[1], tag):
            failed.append(sample)
        status_sub.update(f"[i][dim]{step} done: ({i}/{len(sub_list)}) | ({st_count}), waiting for other processes [/dim][/i]", spinner="toggle9", spinner_style="blue")
    if failed:
        raise RuntimeError(f"{step} failed for {len(failed)} sample(s): {', '.join(failed)}")

def collect_files(sources: Iterable[Path], dest_dir: Path) -> int:
    """Copy files into dest_dir. Returns the number copied"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sources:
        dst = dest_dir / src.name
        if dst.exists():
            if filecmp.cmp(src, dst, shallow=False):
                continue # unchanged: keep its timestamp
            logger.warning("%s Overwriting %s: it differs (same sample ID in another study, or a re-run?)", EMOJI_WARNING, escape(dst.name))
        shutil.copy(src, dst)
        copied += 1
    return copied

def merge_files(files: Sequence[Path], merged: Path, command: str, what: str) -> bool:
    """Run a merge command unless merged is newer than every input. Removes a partial table on failure."""
    if not files:
        logger.error("%s No %s to merge", EMOJI_CROSS, what)
        return False
    if merged.is_file() and merged.stat().st_mtime >= max(f.stat().st_mtime for f in files):
        logger.info("%s %s is up to date. Skipping...", EMOJI_CHECK, merged.name)
        return True
    returncode = run_command(command, desc=f"Merging {len(files)} {what}")
    if returncode != 0 or not merged.is_file():
        merged.unlink(missing_ok=True)
        logger.error("%s Merging the %s failed (exit code %d, or no output)", EMOJI_CROSS, what, returncode)
        return False
    logger.info("%s Merged %d %s into %s", EMOJI_CHECK, len(files), what, escape(str(merged)))
    return True


if __name__ == "__main__":
    setup_logging()
    console.print(
        Panel.fit(
            f"{EMOJI_SPARKLE} Common module for {study_name.upper()} scripts",
            title=f"Study: {study_name.upper()}",
            border_style="yellow dim",
            title_align="left",
        ),
        style="italic dim"
    )
    logger.warning("%s This is a common module and is not intended to be run directly.", EMOJI_WARNING)
    logger.info("%s However, here are a few [bold cyan]example usages[/] of the functions defined herein.", EMOJI_SPARKLE)

    logger.info("%s run_command: output lines are color-coded by classify_line", EMOJI_PROCESS)
    run_command('echo "Hello, $USER"; echo "Warning: low memory"; echo "Error: demo only"', desc="Test")

    samples = ["sample 1", "sample 2", "sample 3", "sample 4", "sample 5"]
    split_samples = get_split_size(2, samples)
    logger.info("%s get_split_size(2, samples):", EMOJI_PROCESS)
    console.print(Panel.fit(Pretty(split_samples, expand_all=True), title="Split lists", border_style="yellow dim"))

    def example_run_func(sub_list: list[str], status_sub: StatusLike) -> None:
        for i, sample in enumerate(sub_list, start=1):
            status_sub.update(f"[i][dim]Running on[/dim] [blue]{sample}[/blue][dim]: ({i}/{len(sub_list)}) [/dim][/i]")
            run_command(f'echo "Processing {sample}"', desc="Test")

    status_subs = [console.status(f"[i][dim]Running on[/dim] {i + 1} [dim]thread...[/dim][/i]") for i in range(2)]
    logger.info("%s run_concurrently(run_fun, split_size, sample_lists, status_subs):", EMOJI_PROCESS)
    run_concurrently(example_run_func, 2, split_samples, status_subs, stagger=1.0)

    console.print(
        Panel.fit(f"{EMOJI_CHECK} [bold green]Finished[/bold green] example usages of common module functions.", title="Done", border_style="green", title_align="left"),
        style="italic"
    )