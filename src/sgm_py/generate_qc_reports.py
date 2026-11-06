#!/usr/bin/env python3
"""Run FastQC on each sample's reads, followed by MultiQC on the FastQC reports"""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import common
import study_runner
from rich.markup import escape

logger = common.logger

REQUIRED_TOOLS = ('FastQC', 'MultiQC')

def fastqc_report(qc_out: Path, read: Path) -> Path:
    """HTML report FastQC writes for a read file"""
    stem = re.sub(r"\.gz$", "", read.name)
    stem = re.sub(r"\.(fastq|fq)$", "", stem)
    return qc_out / f"{stem}_fastqc.html"

def pending_reads(in_dir: Path, qc_out: Path, samples: list[str]) -> list[Path]:
    """Read files of `samples` that do not have a FastQC report yet"""
    pending: list[Path] = []
    for sample in samples:
        reads = common.find_reads(in_dir, sample)
        if not reads:
            logger.warning("%s No read files found for %s in %s", common.EMOJI_WARNING, escape(sample), escape(in_dir.name))
        pending.extend(r for r in reads if not fastqc_report(qc_out, r).exists())
    return pending

def _clear_dir(path: Path) -> None:
    for item in path.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

def qc_reports(
    in_dir: str | Path,
    qc_out: str | Path,
    mqc_out: str | Path,
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    samples: list[str],
    threads: int,
    split_size: int,
    utility_paths: dict[str, str],
) -> bool:
    """Run FastQC on reads without a report, followed by MultiQC. Returns True on success"""
    missing = [tool for tool in REQUIRED_TOOLS if tool not in utility_paths]
    if missing:
        raise ValueError(f"utility_paths is missing: {', '.join(missing)}")

    in_dir, qc_out, mqc_out = Path(in_dir), Path(qc_out), Path(mqc_out)
    qc_out.mkdir(parents=True, exist_ok=True)
    mqc_out.mkdir(parents=True, exist_ok=True)

    # common variables with rich colors
    study_tag = f"[blue]{escape(study)}[/blue]"
    in_tag = f"{study_tag} {common.EMOJI_PLAY} [magenta]{escape(in_dir.name)}[/magenta]"
    count = f"[dim]: ({studies.index(study) + 1}/{len(studies)}) [/dim]"
    q = shlex.quote

    # FastQC
    logger.info("%s Running FastQC on %s...", common.EMOJI_SPARKLE, in_tag)
    todo = pending_reads(in_dir, qc_out, samples)
    if not todo:
        status_sub.update(f"[i][dim]Generated QC reports for[/dim] {len(samples)} [dim]samples in[/dim] {in_tag} {count}[/i]", spinner="point", spinner_style="magenta")
        logger.info("%s FastQC reports already generated for %s. Skipping...", common.EMOJI_CHECK, study_tag)
    else:
        status_sub.update(f"[i][dim]Generating QC reports for[/dim] {len(todo)} [dim]read files in[/dim] {in_tag}{count}[/i]", spinner="point", spinner_style="magenta")
        logger.info("%s Generating FastQC reports for %d read files in %s...", common.EMOJI_PROCESS, len(todo), study_tag)
        files = " ".join(q(str(read)) for read in todo)
        returncode = common.run_command(
            common.env_command(utility_paths["FastQC"], f"fastqc {files} -o {q(str(qc_out))} -t {threads * split_size}"),
            desc=f"Generating FastQC reports for {study}",
        )
        if returncode != 0:
            logger.error("%s FastQC failed for %s (exit code %d)", common.EMOJI_CROSS, study_tag, returncode)
            return False
        logger.info("%s Cleaning previous MultiQC reports for %s, if any", common.EMOJI_TRASH, study_tag)
        _clear_dir(mqc_out)

    # MultiQC
    status_sub.update(f"[i][dim]Generating Cumulative QC reports for[/dim] {in_tag} {count}[/i]", spinner="point", spinner_style="magenta")
    logger.info("%s Running MultiQC on %s reports...", common.EMOJI_SPARKLE, study_tag)
    if len(list(mqc_out.iterdir())) == 2:
        logger.info("%s MultiQC report already generated for %s. Skipping...", common.EMOJI_CHECK, study_tag)
    else:
        _clear_dir(mqc_out)
        returncode = common.run_command(
            common.env_command(utility_paths["MultiQC"], f"multiqc {q(str(qc_out))} -o {q(str(mqc_out))} --interactive"),
            desc=f"Generating cumulative report for {study}",
        )
        if returncode != 0:
            logger.error("%s MultiQC failed for %s (exit code %d)", common.EMOJI_CROSS, study_tag, returncode)
            return False
    
    logger.info("%s Generated QC reports for %s", common.EMOJI_CHECK, study_tag)
    return True

def process_study(
    args: argparse.Namespace,
    study_dir: Path,
    study: str,
    studies: list[str],
    samples: list[str],
    utility_paths: dict[str, str],
    status_subs: Sequence[common.StatusLike],
) -> bool:
    common.set_waiting(status_subs)
    return qc_reports(study_dir / args.reads_dir, study_dir / args.qc_dir, study_dir / args.mqc_dir, status_subs[0], study, studies, samples, args.threads, args.split_size, utility_paths)


DESCRIPTION = "Generate QC reports. Runs FastQC and MultiQC. Expects the name of the directory with reads, by default looks for raw_reads directory"

def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-r", "--reads_dir", default="raw_reads", help="Input directory name containing reads. (Default: raw_reads)")
    parser.add_argument("-q", "--qc_dir", default="raw_qc", help="Output directory name for QC reports. (Default: raw_qc)")
    parser.add_argument("-m", "--mqc_dir", default="raw_mqc", help="Output directory name for MultiQC report. (Default: raw_mqc)")

def run(args: argparse.Namespace) -> int:
    return study_runner.run_studies(args, "Generating QC reports...", "QC reports", REQUIRED_TOOLS, process_study)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    common.add_common_args(parser)
    add_args(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())