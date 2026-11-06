#!/usr/bin/env python3
"""Taxonomic profiling with Kraken2 + Bracken, MPA conversion, and a merged MPA table of all studies."""

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

REQUIRED_KEYS = ("Kraken2", "Bracken", "krakenTools", "kraken_DB")
K2MPA_DIR = "k2mpa_files"
MERGED_NAME = "k2mpa_merged.txt"
DB_FILES = ("has.k2d", "opts.k2d", "taxo.k2d")
MARGIN_GB = 1.5   # Free RAM required on top of the DB size
SHM_DIR = Path("/dev/shm")

# -----------------------------------------------
# Database and memory
# -----------------------------------------------
def db_info(path: str) -> tuple[float, bool] | None:
    """(size in GB, lives in /dev/shm) of a Kraken2 database. None if its index files are missing."""
    root  = Path(path)
    try:
        size = sum((root / name).stat().st_size for name in DB_FILES)
        resolved = root.resolve()
    except OSError:
        return None
    return size / 1024**3, resolved == SHM_DIR or SHM_DIR in resolved.parents

def plan_kraken(free_gb: float, size_gb: float, in_shm: bool, workers: int) -> tuple[int, bool]:
    """(parallel runs, use --memory-mapping). 0 runs means: not enough memory, do not start.
    
    If /dev/shm is provided, the database is already in RAM and shared by all runs. 
    On disk every run loads its own copy, 
    so only free_gb // (size + margin) runs fit. free_gb <= 0 means "unknown".
    """
    need = size_gb + MARGIN_GB
    if in_shm:
        logger.info("%s Database is in /dev/shm (already in RAM): memory mapping on, %d parallel run(s) share one copy", common.EMOJI_SPARKLE, workers)
        return workers, True
    if free_gb <= 0:
        logger.warning("%s Free RAM could not be detected (the database needs ~%.0f GB per run): running one sample at a time", common.EMOJI_WARNING, need)
        return 1, False
    fit = int(free_gb // need)
    if fit < 1:
        logger.error("%s Not enough memory: %.0f GB RAM is free but the database needs ~%.0f GB, Kraken2 would crash. Free some memory or use a smaller database", common.EMOJI_CROSS, free_gb, need)
        return 0, False
    if fit < workers:
        logger.warning(
            "%s Each parallel run loads its own %.0f GB copy of the database and %.0f GB RAM is free: running %d at a time instead of %d. Place the database in /dev/shm to run more",
            common.EMOJI_WARNING, size_gb, free_gb, fit, workers,
        )
        return fit, False
    logger.info("%s %.0f GB RAM is free: enough for %d parallel run(s) of ~%.0f GB each", common.EMOJI_SPARKLE, free_gb, workers, need)
    return workers, False

def setup(args: argparse.Namespace, utility_paths: dict[str, str]) -> bool:
    """Show the machine's resources and decide how Kraken2 runs. Stores db, workers and mapping in args."""
    info = common.get_system_info()
    common.print_system_info(info)
    path = common.expand_path(utility_paths["kraken_DB"])
    found = db_info(path)
    if found is None:
        logger.error("%s Kraken2 database not found (%s missing in %s) [kraken_DB]", common.EMOJI_CROSS, "/".join(DB_FILES), escape(path))
        return False
    logger.info("%s Kraken2 database: %.0f GB, %s", common.EMOJI_SPARKLE, found[0], escape(path))
    workers, mapping = plan_kraken(info.usable_ram_gb, found[0], found[1], args.split_size)
    if workers == 0:
        return False
    args.kraken_db, args.kraken_workers, args.kraken_mapping = path, workers, mapping
    return True

def confidence_value(text: str) -> float:
    """argparse type for --confidence: a number between 0 and 1."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0.0 <= value <- 1.0:
        raise argparse.ArgumentTypeError(f"{text} must be between 0 and 1")
    return value

# -----------------------------------------------
# Per-sample steps
# -----------------------------------------------
def build_kraken_command(reads: list[Path], db: str, out: Path, report: Path, threads: int, memory_mapping: bool, confidence: float) -> str:
    """k2 classify command for SE and PE reads"""
    q = shlex.quote
    mapping = " --memory-mapping" if memory_mapping else ""
    paired = " --paired" if len(reads) == 2 else ""
    files = " ".join(q(str(read)) for read in reads)
    return f"k2 classify --db {q(db)}{mapping} --threads {threads} --confidence {confidence:g}{paired} --output {q(str(out))} --report {q(str(report))} --use-names {files}"

def classify_reads(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    reads_dir: str | Path,
    kraken_out: str |Path,
    utility_paths: dict[str, str],
    db: str,
    threads: int,
    memory_mapping: bool,
    confidence: float
) -> None:
    """Kraken2: classify the reads of every sample in the sub_list"""
    reads_dir, kraken_out = Path(reads_dir), Path(kraken_out)

    def plan(sample: str) -> tuple[list[Path], str] | None:
        reads = common.find_reads(reads_dir, sample)
        if len(reads) not in (1, 2):
            logger.error("%s Expected 1 or 2 read-files for %s, found %d. Check %s directory", common.EMOJI_CROSS, common.sample_tag(study, sample), len(reads), escape(reads_dir.name))
            return None
        out_dir = kraken_out / sample
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs = [out_dir / f"{sample}.out", out_dir / f"{sample}.report"]
        return outputs, common.env_command(utility_paths["Kraken2"], build_kraken_command(reads, db, outputs[0], outputs[1], threads, memory_mapping, confidence))

    common.run_step_for(sub_list, status_sub, study, studies, kraken_out, "kraken", "Classifying reads of", plan)

def estimate_abundance(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    kraken_out: str | Path,
    utility_paths: dict[str, str],
    db: str,
) -> None:
    """Bracken: abundance estimates from the Kraken2 reports (same database as the classification)."""
    kraken_out = Path(kraken_out)
    q = shlex.quote

    def plan(sample: str) -> tuple[list[Path], str] | None:
        out_dir = kraken_out / sample
        outputs = [out_dir / f"{sample}.bracken", out_dir / f"{sample}_bracken_species.report"]
        command = f"bracken -d {q(db)} -i {q(str(out_dir / f'{sample}.report'))} -o {q(str(outputs[0]))}"
        return outputs, common.env_command(utility_paths["Bracken"], command)

    common.run_step_for(sub_list, status_sub, study, studies, kraken_out, "bracken", "Running Bracken on", plan)

def convert_to_mpa(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    kraken_out: str | Path,
    utility_paths: dict[str, str],
) -> None:
    """KrakenTools: Bracken species report -> MPA format."""
    kraken_out = Path(kraken_out)
    q = shlex.quote

    def plan(sample: str) -> tuple[list[Path], str] | None:
        out_dir = kraken_out / sample
        outputs = [out_dir / f"{sample}_mpa.txt"]
        command = f"kreport2mpa.py -r {q(str(out_dir / f'{sample}_bracken_species.report'))} -o {q(str(outputs[0]))}"
        return outputs, common.env_command(utility_paths["krakenTools"], command)

    common.run_step_for(sub_list, status_sub, study, studies, kraken_out, "krakenTools", "Converting to MPA:", plan)


# -----------------------------------------------
# Studies and merged table
# -----------------------------------------------
def process_study(
    args: argparse.Namespace,
    study_dir: Path,
    study: str,
    studies: list[str],
    samples: list[str],
    utility_paths: dict[str, str],
    status_subs: Sequence[common.StatusLike],
) -> bool:
    kraken_out = study_dir / "kraken_out"
    if not common.resume_allowed(args, [kraken_out], study):
        return False
    kraken_out.mkdir(parents=True, exist_ok=True)
    workers: int = args.kraken_workers
    sample_lists = common.get_split_size(workers, samples)

    # Kraken2 runs concurrently as far as the memory allows
    for status_sub in status_subs[workers:]:
        status_sub.update("[dim]Not used: limited by free RAM[/dim]")
    common.run_concurrently(
        classify_reads, workers, sample_lists, status_subs[:workers], study, studies, study_dir / args.reads_dir, kraken_out, 
        utility_paths, args.kraken_db, args.threads, args.kraken_mapping, args.confidence,
    )
    common.set_waiting(status_subs, "Running Bracken and converting to MPA format...")
    estimate_abundance(samples, status_subs[0], study, studies, kraken_out, utility_paths, args.kraken_db)
    convert_to_mpa(samples, status_subs[0], study, studies, kraken_out, utility_paths)

    copied = common.collect_files((kraken_out / s / f"{s}_mpa.txt" for s in samples), study_dir.parent / K2MPA_DIR)
    logger.info("%s MPA files of %s: %d new or updated in %s", common.EMOJI_CHECK, escape(study), copied, K2MPA_DIR)
    return True

def merge_mpa(args: argparse.Namespace, base: Path, studies: list[str], utility_paths: dict[str, str]) -> bool:
    """Merge the MPA files of all studies into one table (runs after every study succeeded)."""
    k2mpa_dir = base / K2MPA_DIR
    files = sorted(k2mpa_dir.glob("*_mpa.txt"))
    merged = base / MERGED_NAME
    q = shlex.quote
    names = " ".join(q(f.name) for f in files)
    command = f"cd {q(str(k2mpa_dir))} && " + common.env_command(utility_paths["krakenTools"], f"combine_mpa_mod.py -i {names} -o {q(str(merged))}")
    return common.merge_files(files, merged, command, "MPA files")


DESCRIPTION = "Classify reads with Kraken2 and Bracken, convert to MPA format and merge the MPA files of all studies"

def add_tool_args(parser: argparse.ArgumentParser) -> None:
    """Kraken2-only options (also offered by run_taxonomy module)"""
    parser.add_argument("--confidence", type=confidence_value, default=0.0, help="Kraken2 confidence score threshold, from 0 to 1. (Default: 0.0)")

def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-r", "--reads_dir", default="filtered_reads", help="Input directory name containing reads. (Default: filtered_reads)")
    add_tool_args(parser)

def run(args: argparse.Namespace) -> int:
    return study_runner.run_studies(args, "Classifying reads with Kraken2 suite", "Kraken2", REQUIRED_KEYS, process_study, setup=setup, finish_base=merge_mpa)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    common.add_common_args(parser)
    add_args(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())