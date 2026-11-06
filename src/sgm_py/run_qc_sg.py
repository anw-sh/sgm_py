#!/usr/bin/env python3
"""QC pipeline for shotgun reads: FastQC/MultiQC at each stage, BBDuk, optional fastp and Hostile."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import common
import generate_qc_reports
import run_bbduk
import run_fastp
import run_hostile
import study_runner
from rich.markup import escape

logger = common.logger

# Pipeline variants (QC reports and BBDuk always run)
PROFILES: dict[str, frozenset[str]] = {
    "human_mag": frozenset({"fastp", "hostile"}),   # human gut samples -> MAG generation
    "human_taxonomy": frozenset({"hostile"}),       # human gut samples -> Kraken / MetaPhlAn
    "other_mag": frozenset({"fastp"}),              # non-human samples -> MAG generation
    "other_taxonomy": frozenset(),                  # non-human samples -> Kraken / MetaPhlAn
}

def required_keys(stages: frozenset[str]) -> list[str]:
    """utility_paths.csv entries need for the enabled stages"""
    keys = [*generate_qc_reports.REQUIRED_TOOLS, *run_bbduk.REQUIRED_KEYS]
    if "fastp" in stages:
        keys += run_fastp.REQUIRED_KEYS
    if "hostile" in stages:
        keys += run_hostile.REQUIRED_KEYS
    return keys

FILTERED_READS_DIR = "filtered_reads"    # Final reads of each profile, as symlinks to corresponding final stage
STAGE_DIRS = ("bb_out", "fp_out", "hostile_out")

def study_dirs(study_dir: Path, stages: frozenset[str]) -> dict[str, Path]:
    """Directories used by the enabled stages"""
    names = ["raw_reads", "raw_qc", "raw_mqc", "bb_out", "bb_qc", "bb_mqc", FILTERED_READS_DIR]
    if "fastp" in stages:
        names += ["fp_out", "fp_qc", "fp_mqc"]
    if "hostile" in stages:
        names.append("hostile_out")
    return {name: study_dir / name for name in names}

def final_stage(stages: frozenset[str]) -> tuple[str, str]:
    """(output directory key, stage name) of the last enabled stage"""
    if "hostile" in stages:
        return "hostile_out", "hostile"
    if "fastp" in stages:
        return "fp_out", "fastp"
    return "bb_out", "bbduk"

def final_outputs(sample: str, dirs: dict[str, Path], stages: frozenset[str]) -> list[Path]:
    """Read-files, the last enabled stage produces for a sample"""
    paired = len(common.find_reads(dirs["raw_reads"], sample)) == 2
    key = final_stage(stages)[0]
    if key == "hostile_out":
        return run_hostile.hostile_outputs(dirs[key], sample, paired)
    if key == "fp_out":
        return run_fastp.fastp_outputs(dirs[key], sample, paired)
    return run_bbduk.bbduk_outputs(dirs[key], sample, paired)

def is_finished(sample: str, dirs: dict[str, Path], stages: frozenset[str]) -> bool:
    """True if the last enabled stage completed processing the current sample"""
    return common.is_done(dirs[final_stage(stages)[0]], sample, final_outputs(sample, dirs, stages))

def run_stages(
    args: argparse.Namespace,
    dirs: dict[str, Path],
    study_dir: Path,
    study: str,
    studies: list[str],
    remaining: list[str],
    utility_paths: dict[str, str],
    status_subs: Sequence[common.StatusLike]
) -> bool:
    """Run BBDuk, fastp, QC of filtered reads and Hostile for the samples that are not finished."""
    stages = PROFILES[args.profile]
    threads, split_size = args.threads, args.split_size
    sample_lists = common.get_split_size(split_size, remaining)

    # BBDuk (+ fastp)
    try:
        common.run_concurrently(run_bbduk.trim_reads, split_size, sample_lists, status_subs, study, studies, dirs["raw_reads"], dirs["bb_out"], utility_paths, threads)
    finally:
        run_bbduk.write_bbduk_results(dirs["bb_out"], study_dir / "bb_out_count.txt")
    if "fastp" in stages:
        common.run_concurrently(run_fastp.dedup_reads, split_size, sample_lists, status_subs, study, studies, dirs["bb_out"], dirs['fp_out'], utility_paths, threads)

    # QC reports of filtered reads, before Hostile deletes the intermediates
    qc_targets = [("bb_out", "bb_qc", "bb_mqc")]
    if "fastp" in stages:
        qc_targets.append(("fp_out", "fp_qc", "fp_mqc"))
    for out_key, qc_key, mqc_key in qc_targets:
        common.set_waiting(status_subs)
        if not generate_qc_reports.qc_reports(dirs[out_key], dirs[qc_key], dirs[mqc_key], status_subs[0], study, studies, remaining, threads, split_size, utility_paths):
            return False

    # Hostile
    if "hostile" in stages:
        in_key = "fp_out" if "fastp" in stages else "bb_out"
        cleanup = [] if args.keep_intermediate else [dirs[key] for key in ("bb_out", "fp_out") if key in dirs]
        common.run_concurrently(run_hostile.remove_host, split_size, sample_lists, status_subs, study, studies, dirs[in_key], dirs["hostile_out"], utility_paths, threads, cleanup)
    
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
    stages = PROFILES[args.profile]
    dirs = study_dirs(study_dir, stages)
    if not common.resume_allowed(args, [dirs[key] for key in STAGE_DIRS if key in dirs], study):
        return False
    for name, path in dirs.items():
        if name != "raw_reads":
            path.mkdir(parents=True, exist_ok=True)

    # QC reports for raw reads
    common.set_waiting(status_subs)
    if not generate_qc_reports.qc_reports(dirs["raw_reads"], dirs["raw_qc"], dirs["raw_mqc"], status_subs[0], study, studies, samples, args.threads, args.split_size, utility_paths):
        return False

    # Samples whose final stage completed are done, their intermediates may have been deleted, so...
    remaining = [sample for sample in samples if not is_finished(sample, dirs, stages)]
    if remaining:
        logger.info("%s %d of %d samples left to process (profile: %s)", common.EMOJI_SPARKLE, len(remaining), len(samples), args.profile)
        if not run_stages(args, dirs, study_dir, study, studies, remaining, utility_paths, status_subs):
            return False
    else:
        logger.info("%s All %d samples of %s already processed", common.EMOJI_CHECK, len(samples), escape(study))

    # Same input names for the downstream scripts, irrespective of the profile
    for sample in samples:
        common.link_filtered_reads(final_outputs(sample, dirs, stages), dirs[FILTERED_READS_DIR], sample)
    logger.info("%s Final reads of %d samples linked in %s", common.EMOJI_CHECK, len(samples), escape(f"{study}/{FILTERED_READS_DIR}"))
    return True


DESCRIPTION = "Perform QC on shotgun reads. Runs FastQC, MultiQC and BBDuk, plus fastp and Hostile depending on the profile."

def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default="human_mag",
        help="Sample type and goal. human_mag: BBDuk+fastp+Hostile; human_taxonomy: BBDuk+Hostile; other_mag: BBDuk+fastp; other_taxonomy: BBDuk only. (Default: human_mag)",
    )
    parser.add_argument("--keep_intermediate", action="store_true", help="Keep BBDuk/fastp reads after Hostile succeeded (default: delete them to save space).")

def run(args: argparse.Namespace) -> int:
    stages = PROFILES[args.profile]
    return study_runner.run_studies(args, f"Performing QC on shotgun reads (profile: {args.profile})...", "QC", required_keys(stages), process_study)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    common.add_common_args(parser)
    add_args(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())