#!/usr/bin/env python3
"""Taxonomic profiling with MetaPhlAn: read-stats ("absolute") and relative-abundance profiles, merged over all studies."""

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

REQUIRED_KEYS = ("MetaPhlAn", "mpa_DB", "mpa_index")
OUT_DIR = "mpa_out"
PROFILES_DIR = "mpa_profiles"
# profile kind
KINDS = {
    "abs": ("_abs_profile.txt", "rel_ab_w_read_stats", "merge_metaphlan4_tables_abs.py", "mpa_merged_abs_profiles.txt"),
    "rel": ("_rel_profile.txt", "rel_ab", "merge_metaphlan_tables.py", "mpa_merged_rel_profiles.txt")
}

def profile_path(out_dir: Path, sample: str, kind: str) -> Path:
    return out_dir / f"{sample}{KINDS[kind][0]}"

def db_args(db: str, index: str) -> str:
    return f"--bowtie2db {shlex.quote(db)} -x {shlex.quote(index)}"

def setup(args: argparse.Namespace, utility_paths: dict[str, str]) -> bool:
    """Check that the database directory holds the index, and keep the expanded path in args.mpa_db."""
    db, index = common.expand_path(utility_paths["mpa_DB"]), utility_paths["mpa_index"]
    if not any(Path(db).glob(f"{index}*")):
        logger.error("%s MetaPhlAn index %s not found in %s [mpa_DB, mpa_index]", common.EMOJI_CROSS, escape(index), escape(db))
        return False
    logger.info("%s MetaPhlAn database: %s in %s", common.EMOJI_SPARKLE, escape(index), escape(db))
    args.mpa_db = db
    return True

# -----------------------------------------------
# Per-sample steps
# -----------------------------------------------
def build_abs_command(reads: list[Path], bowtie2out: Path, profile: Path, db: str, index: str, threads: int) -> str:
    """Reads -> bowtie2 mapping (kept) + profile with estimated read counts. 1 (SE) or 2 (PE) read files."""
    q = shlex.quote
    files = ",".join(q(str(read)) for read in reads)
    return (
        f"metaphlan {files} --input_type fastq --bowtie2out {q(str(bowtie2out))} --nproc {threads} "
        f"-t {KINDS['abs'][1]} {db_args(db, index)} -o {q(str(profile))}"
    )

def build_rel_command(bowtie2out: Path, profile: Path, db: str, index: str, threads: int) -> str:
    """Saved bowtie2 mapping -> relative abundance profile (no new mapping)."""
    q = shlex.quote
    return f"metaphlan {q(str(bowtie2out))} --input_type bowtie2out --nproc {threads} -t {KINDS['rel'][1]} {db_args(db, index)} -o {q(str(profile))}"

def profile_abs(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    reads_dir: str | Path,
    out_dir: str | Path,
    utility_paths: dict[str, str],
    db: str,
    threads: int,
) -> None:
    """MetaPhlAn on the reads of every sample in sub_list (the slow step: runs concurrently)."""
    reads_dir, out_dir = Path(reads_dir), Path(out_dir)
    index = utility_paths["mpa_index"]

    def plan(sample: str) -> tuple[list[Path], str] | None:
        reads = common.find_reads(reads_dir, sample)
        if len(reads) not in (1, 2):
            logger.error("%s Expected 1 or 2 read files for %s, found %d. Check %s directory.", common.EMOJI_CROSS, common.sample_tag(study, sample), len(reads), escape(reads_dir.name))
            return None
        # MetaPhlAn refuses to overwrite an existing bowtie2out: run_step removes unfinished outputs first
        outputs = [out_dir / f"{sample}.bowtie2.bz2", profile_path(out_dir, sample, "abs")]
        return outputs, common.env_command(utility_paths["MetaPhlAn"], build_abs_command(reads, outputs[0], outputs[1], db, index, threads))

    common.run_step_for(sub_list, status_sub, study, studies, out_dir, "abs", "Profiling (read stats)", plan)


def profile_rel(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    out_dir: str | Path,
    utility_paths: dict[str, str],
    db: str,
    threads: int,
) -> None:
    """Relative abundance profiles from the saved bowtie2 mappings (quick)."""
    out_dir = Path(out_dir)
    index = utility_paths["mpa_index"]

    def plan(sample: str) -> tuple[list[Path], str] | None:
        bowtie2out = out_dir / f"{sample}.bowtie2.bz2"
        profile = profile_path(out_dir, sample, "rel")
        return [profile], common.env_command(utility_paths["MetaPhlAn"], build_rel_command(bowtie2out, profile, db, index, threads))

    common.run_step_for(sub_list, status_sub, study, studies, out_dir, "rel", "Profiling (relative abundance)", plan)


# -----------------------------------------------
# Studies and the merged table
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
    out_dir = study_dir / OUT_DIR
    if not common.resume_allowed(args, [out_dir], study):
        return False
    out_dir.mkdir(parents=True, exist_ok=True)

    sample_lists = common.get_split_size(args.split_size, samples)
    common.run_concurrently(profile_abs, args.split_size, sample_lists, status_subs, study, studies, study_dir / args.reads_dir, out_dir, utility_paths, args.mpa_db, args.threads)
    common.set_waiting(status_subs, "Making relative abundance profiles...")
    profile_rel(samples, status_subs[0], study, studies, out_dir, utility_paths, args.mpa_db, args.threads)

    copied = common.collect_files((profile_path(out_dir, s, kind) for kind in KINDS for s in samples), study_dir.parent / PROFILES_DIR)
    logger.info("%s Profiles of %s: %d new or updated in %s", common.EMOJI_CHECK, escape(study), copied, PROFILES_DIR)
    return True


def merge_profiles(args: argparse.Namespace, base: Path, studies: list[str], utility_paths: dict[str, str]) -> bool:
    """Merge the profiles of all studies: one table per kind (runs after every study succeeded)."""
    profiles_dir = base / PROFILES_DIR
    q = shlex.quote
    ok = True
    for kind, (suffix, _, script, merged_name) in KINDS.items():
        files = sorted(profiles_dir.glob(f"*{suffix}"))
        merged = base / merged_name
        names = " ".join(q(f.name) for f in files)
        command = f"cd {q(str(profiles_dir))} && " + common.env_command(utility_paths["MetaPhlAn"], f"{script} {names}") + f" > {q(str(merged))}"
        ok = common.merge_files(files, merged, command, f"{kind} profiles") and ok
    return ok


DESCRIPTION = "Profile read-files with MetaPhlAn (read-stats and relative-abundance profiles) and merge the profiles of all studies."

def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-r", "--reads_dir", default="filtered_reads", help="Input directory name containing reads. (Default: filtered_reads)")

def run(args: argparse.Namespace) -> int:
    return study_runner.run_studies(args, "Profiling reads with MetaPhlAn...", "MetaPhlAn", REQUIRED_KEYS, process_study, setup=setup, finish_base=merge_profiles)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    common.add_common_args(parser)
    add_args(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())