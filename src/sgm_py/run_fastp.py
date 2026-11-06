#!/usr/bin/env python3
"""Remove duplicate reads from SE or PE reads with fastp (adapter trimming off: Already performed using BBDuk)"""

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

REQUIRED_KEYS = ("fastp",)

def fastp_outputs(fp_out: Path, sample: str, paired: bool) -> list[Path]:
    """Read files fastp writes for a samples"""
    if paired:
        return [fp_out / f"{sample}_R1.fq.gz", fp_out / f"{sample}_R2.fq.gz"]
    return [fp_out / f"{sample}.fq.gz"]

def build_fastp_command(reads: list[Path], outputs: list[Path], html_report: Path, json_report: Path, threads: int) -> str:
    """fastp command for SE or PE read files: -D dedup, -A no adapter trimming"""
    q = shlex.quote
    if len(reads) == 1:
        io = f"-i {q(str(reads[0]))} -o {q(str(outputs[0]))}"
    else:
        io = f"-i {q(str(reads[0]))} -o {q(str(outputs[0]))} -I {q(str(reads[1]))} -O {q(str(outputs[1]))}"
    return f"fastp {io} -D -A -h {q(str(html_report))} -j {q(str(json_report))} -w {threads}"

# Run fastp
def dedup_reads(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    in_dir: str | Path,
    fp_out: str | Path,
    utility_paths: dict[str, str],
    threads: int,
) -> None:
    """Deduplicate every sample in the sub_list. Raises RuntimeError at the end if any sample failed"""
    missing = [key for key in REQUIRED_KEYS if key not in utility_paths]
    if missing:
        raise ValueError(f"utility_paths is missing: {', '.join(missing)}")

    in_dir, fp_out = Path(in_dir), Path(fp_out)
    fp_out.mkdir(parents=True, exist_ok=True)
    st_count = f"{studies.index(study) + 1}/{len(studies)}"
    failed: list[str] = []

    for i, sample in enumerate(sub_list, start=1):
        if common.STOP.is_set():
            break
        st_sample = f"[blue]{escape(study)}[/blue] {common.EMOJI_PLAY} [magenta]{escape(sample)}[/magenta]"
        sample_count = f"{i}/{len(sub_list)}"
        status_sub.update(f"[i][dim]Deduplicating reads of [/dim] {st_sample}[dim]: ({sample_count}) | ({st_count}) [/dim][/i]", spinner="point", spinner_style="magenta")

        reads = common.find_reads(in_dir, sample)
        if len(reads) not in (1, 2):
            logger.error("%s Expected 1 or 2 read files for %s, found %d. Check %s directory.", common.EMOJI_CROSS, st_sample, len(reads), escape(in_dir.name))
            failed.append(sample)
            continue

        outputs = fastp_outputs(fp_out, sample, paired=len(reads) == 2)
        if common.is_done(fp_out, sample, outputs):
            logger.info("%s fastp already processed %s. Skipping...", common.EMOJI_CHECK, st_sample)
        else:
            common.prepare_redo(fp_out, sample, outputs)
            logger.info("%s Processing %s with fastp (%s)...", common.EMOJI_PROCESS, st_sample, "PE" if len(reads) == 2 else "SE")
            command = common.env_command(utility_paths["fastp"], build_fastp_command(reads, outputs, fp_out / f"{sample}.html", fp_out / f"{sample}.json", threads))
            returncode = common.run_command(command, desc=f"Performing deduplication of {sample} reads")
            if returncode != 0:
                for out in outputs:
                    out.unlink(missing_ok=True)
                logger.error("%s fastp failed for %s (exit code %d)", common.EMOJI_CROSS, st_sample, returncode)
                failed.append(sample)
                continue
            common.mark_done(fp_out, sample, "fastp", command)

        logger.info("%s Generated HQ reads for %s", common.EMOJI_CHECK, st_sample)
        status_sub.update(f"[i][dim]Filtering completed with fastp: ({sample_count}) | ({st_count}) [/dim][/i]", spinner="toggle9", spinner_style="blue")

    if failed:
        raise RuntimeError(f"fastp failed for {len(failed)} sample(s): {', '.join(failed)}")

def process_study(
        args: argparse.Namespace,
        study_dir: Path,
        study: str,
        studies: list[str],
        samples: list[str],
        utility_paths: dict[str, str],
        status_subs: Sequence[common.StatusLike]
) -> bool:
    fp_out = study_dir / args.fastp_out
    if not common.resume_allowed(args, [fp_out], study):
        return False
    sample_lists = common.get_split_size(args.split_size, samples)
    common.run_concurrently(dedup_reads, args.split_size, sample_lists, status_subs, study, studies, study_dir / args.reads_dir, fp_out, utility_paths, args.threads)
    return True

DESCRIPTION = "Reduce duplicate reads from SE or PE reads with fastp"

def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-r", "--reads_dir", default="bb_out", help="Input directory name containing reads. (Default: bb_out)")
    parser.add_argument("-o", "--fastp_out", default="fp_out", help="Output directory name for fastp reads. (Default: fp_out)")

def run(args: argparse.Namespace) -> int:
    return study_runner.run_studies(args, "Deduplicating reads with fastp...", "fastp", REQUIRED_KEYS, process_study)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    common.add_common_args(parser)
    add_args(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())