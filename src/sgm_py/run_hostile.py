#!/usr/bin/env python3
"""Remove host (e.g. human) reads from SE or PE short reads with Hostile."""

from __future__ import annotations

import argparse
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

import common
import study_runner
from rich.markup import escape

logger = common.logger

REQUIRED_KEYS = ("Hostile", "Hostile_DB")

def hostile_outputs(hostile_out: Path, sample: str, paired: bool) -> list[Path]:
    """Files Hostile writes: it names them after the input files (inputs are {sample}_R1/_R2.fq.gz or {sample}.fq.gz)."""
    if paired:
        return [hostile_out / f"{sample}_R1.clean_1.fastq.gz", hostile_out / f"{sample}_R2.clean_2.fastq.gz"]
    return [hostile_out / f"{sample}.clean.fastq.gz"]

def build_hostile_command(reads: list[Path], hostile_out: Path, index: str, log: Path, threads: int) -> str:
    """hostile command line. Its stdout (run statistics) goes to the sample's .log file.
    Hostile assumes a single fastq is a long-read file, so SE short reads need --aligner bowtie2.
    """
    q = shlex.quote
    if len(reads) == 1:
        io = f"--fastq1 {q(str(reads[0]))} --aligner bowtie2"
    else:
        io = f"--fastq1 {q(str(reads[0]))} --fastq2 {q(str(reads[1]))}"
    return f"hostile clean {io} --output {q(str(hostile_out))} --index {q(index)} --threads {threads} > {q(str(log))}"

def remove_reads(sample: str, directories: Sequence[str | Path]) -> None:
    """Delete a sample's read files from the given directories to save space (intermediate outputs)"""
    for directory in directories:
        for read in common.find_reads(directory, sample):
            read.unlink(missing_ok=True)
            logger.info("%s Removed %s", common.EMOJI_TRASH, escape(str(read)))

# Run hostile
def remove_host(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    in_dir: str | Path,
    hostile_out: str | Path,
    utility_paths: dict[str, str],
    threads: int,
    cleanup_dirs: Sequence[str |Path] = (),
) -> None:
    """Remove host reads for every sample in sub_list.
    `cleanup_dirs`: after a sample succeeded (its output exists), delete that sample's reads in these
    directories to save space. Nothing is deleted if Hostile failed or its output is missing.
    Raises RuntimeError at the end if any sample failed.
    """
    missing = [key for key in REQUIRED_KEYS if key not in utility_paths]
    if missing:
        raise ValueError(f"utility_paths is missing: {', '.join(missing)}")

    in_dir, hostile_out = Path(in_dir), Path(hostile_out)
    hostile_out.mkdir(parents=True, exist_ok=True)
    st_count = f"{studies.index(study) + 1}/{len(studies)}"
    failed: list[str] = []

    for i, sample in enumerate(sub_list, start=1):
        if common.STOP.is_set():
            break
        st_sample = f"[blue]{escape(study)}[/blue] {common.EMOJI_PLAY} [magenta]{escape(sample)}[/magenta]"
        sample_count = f"{i}/{len(sub_list)}"
        status_sub.update(f"[i][dim]Removing host reads from [/dim] {st_sample}[dim]: ({sample_count}) | ({st_count}) [/dim][/i]", spinner="point", spinner_style="magenta")

        reads = common.find_reads(in_dir, sample)
        # Check if the outputs exist or deleted with cleanup
        finished = any(common.is_done(hostile_out, sample, hostile_outputs(hostile_out, sample, p)) for p in (True, False))
        if finished:
            logger.info("%s Host reads already removed from %s. Skipping...", common.EMOJI_CHECK, st_sample)
        else:
            if len(reads) not in (1, 2):
                logger.error("%s Expected 1 or 2 read files for %s, found %d. Check %s directory.", common.EMOJI_CROSS, st_sample, len(reads), escape(in_dir.name))
                failed.append(sample)
                continue
            paired = len(reads) == 2
            outputs = hostile_outputs(hostile_out, sample, paired)
            common.prepare_redo(hostile_out, sample, outputs)
            logger.info("%s Processing %s with Hostile (%s)...", common.EMOJI_PROCESS, st_sample, "PE" if paired else "SE")
            command = common.env_command(utility_paths["Hostile"], build_hostile_command(reads, hostile_out, common.expand_path(utility_paths["Hostile_DB"]), hostile_out / f"{sample}.log", threads))
            returncode = common.run_command(command, desc=f"Removing host reads from {sample}")
            if returncode != 0 or not all(out.exists() for out in outputs):
                for out in outputs:
                    out.unlink(missing_ok=True)
                logger.error("%s Hostile failed for %s (exit code %d, or expected output missing). Inputs retained", common.EMOJI_CROSS, st_sample, returncode)
                failed.append(sample)
                continue
            common.mark_done(hostile_out, sample, "hostile", command)
            # Remove intermediate reads to save space
            remove_reads(sample, cleanup_dirs)

        logger.info("%s Removed host reads from %s", common.EMOJI_CHECK, st_sample)
        status_sub.update(f"[i][dim]Removing host reads completed: ({sample_count}) | ({st_count}) [/dim][/i]", spinner="toggle9", spinner_style="blue")

    if failed:
        raise RuntimeError(f"Hostile failed for {len(failed)} sample(s): {', '.join(failed)}")

def process_study(
    args: argparse.Namespace,
    study_dir: Path,
    study: str,
    studies: list[str],
    samples: list[str],
    utility_paths: dict[str, str],
    status_subs: Sequence[common.StatusLike],
) -> bool:
    in_dir = study_dir / args.reads_dir
    hostile_out = study_dir / args.hostile_out
    if not common.resume_allowed(args, [hostile_out], study):
        return False
    cleanup = [in_dir] if args.rm_input else []
    sample_lists = common.get_split_size(args.split_size, samples)
    common.run_concurrently(remove_host, args.split_size, sample_lists, status_subs, study, studies, in_dir, hostile_out, utility_paths, args.threads, cleanup)
    return True


DESCRIPTION = "Remove host reads from SE or PE short reads with Hostile"

def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-r", "--reads_dir", default="fp_out", help="Input directory name containing reads. (Default: fp_out)")
    parser.add_argument("-o", "--hostile_out", default="hostile_out", help="Output directory name for host-removed reads. (Default: hostile_out)")
    parser.add_argument("--rm_input", action="store_true", help="Delete each sample's input reads once its hostile output exists")


def run(args: argparse.Namespace) -> int:
    return study_runner.run_studies(args, "Removing host reads with Hostile...", "Hostile", REQUIRED_KEYS, process_study)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    common.add_common_args(parser)
    add_args(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())