#!/usr/bin/env python3
"""Trim SE and PE reads with BBDuk"""

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

REQUIRED_KEYS = ('BBDuk', "bb_adapters")
BBDUK_OPTS = "k=19 mink=7 ktrim=r trimq=20 qtrim=r hdist=1 hdist2=0"

def bbduk_outputs(bb_out: Path, sample: str, paired: bool) -> list[Path]:
    """Output files BBDuk writes for a sample (PE outputs are always _R1/_R2)"""
    if paired:
        return [bb_out / f"{sample}_R1.fq.gz", bb_out / f"{sample}_R2.fq.gz"]
    # For single ends
    return [bb_out / f"{sample}.fq.gz"]

def build_bbduk_command(reads: list[Path], outputs: list[Path], adapters: str, threads: int) -> str:
    """bbduk.sh command for SE or PE reads"""
    q = shlex.quote
    if len(reads) == 1:
        io = f"in={q(str(reads[0]))} out={q(str(outputs[0]))}"
        extra = ""
    else:
        io = f"in1={q(str(reads[0]))} in2={q(str(reads[1]))} out1={q(str(outputs[0]))} out2={q(str(outputs[1]))}"
        extra = " tpe tbo"
    return f"bbduk.sh {io} ref={q(adapters)} {BBDUK_OPTS}{extra} threads={threads}"

def write_bbduk_results(bb_out: str | Path, out_file: str | Path) -> int:
    """Collect 'Result' lines of BBDuk logs as `log:line` (replaces `grep Result *.log > file`)"""
    lines: list[str] = []
    for log in sorted(Path(bb_out).glob("*.log")):
        try:
            text = log.read_text(errors="replace")
        except OSError as e:
            logger.warning("%s Cannot read %s: %s", common.EMOJI_WARNING, escape(log.name), escape(str(e)))
            continue
        lines.extend(f"{log}:{line}" for line in text.splitlines() if "Result" in line)
    Path(out_file).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return len(lines)

# Run BBDuk
def trim_reads(
    sub_list: list[str],
    status_sub: common.StatusLike,
    study: str,
    studies: list[str],
    raw_reads: str | Path,
    bb_out: str | Path,
    utility_paths: dict[str, str],
    threads: int,
) -> None:
    """Trim every sample in the sub_list. Raises RuntimeError at the end if sample failed"""
    missing = [key for key in REQUIRED_KEYS if key not in utility_paths]
    if missing:
        raise ValueError(f"utility_paths is missing: {", ".join(missing)}")

    raw_reads, bb_out = Path(raw_reads), Path(bb_out)
    bb_out.mkdir(parents=True, exist_ok=True)
    st_count = f"{studies.index(study) + 1}/{len(studies)}"
    failed: list[str] = []

    for i, sample in enumerate(sub_list, start=1):
        # Common variables for rich colors
        st_sample = f"[blue]{escape(study)}[/blue] {common.EMOJI_PLAY} [magenta]{escape(sample)}[/magenta]"
        sample_count = f"{i}/{len(sub_list)}"
        status_sub.update(f"[i][dim]Filtering reads of[/dim] {st_sample}[dim]: ({sample_count}) | ({st_count}) [/dim][/i]", spinner="point", spinner_style="magenta")

        # Identify SE or PE
        reads = common.find_reads(raw_reads, sample)
        if len(reads) not in (1, 2):
            logger.error("%s Expected 1 or 2 read files for %s, found %d. Check %s directory.", common.EMOJI_CROSS, st_sample, len(reads), escape(raw_reads.name))
            failed.append(sample)
            continue

        outputs = bbduk_outputs(bb_out, sample, paired=len(reads) == 2)
        if common.is_done(bb_out, sample, outputs):
            logger.info("%s BBDuk already processed %s. Skipping...", common.EMOJI_CHECK, st_sample)
        else:
            common.prepare_redo(bb_out, sample, outputs)
            logger.info("%s Processing %s with BBDuk (%s)...", common.EMOJI_PROCESS, st_sample, "PE" if len(reads) == 2 else "SE")
            command = common.env_command(utility_paths["BBDuk"], build_bbduk_command(reads, outputs, utility_paths["bb_adapters"], threads))
            returncode = common.run_command(command, desc=f"Trimming {sample} reads", log_file=bb_out / f"{sample}.log")
            if returncode != 0:
                for out in outputs:
                    out.unlink(missing_ok=True)
                logger.error("%s BBDuk failed for %s (exit code %d)", common.EMOJI_CROSS, st_sample, returncode)
                failed.append(sample)
                continue
            common.mark_done(bb_out, sample, "bbduk", command)

        logger.info("%s Generated filtered reads for %s", common.EMOJI_CHECK, st_sample)
        status_sub.update(f"[i][dim]Filtering completed using BBDuk: ({sample_count}) | ({st_count}) [/dim][/i]", spinner="toggle9", spinner_style="blue")

    if failed:
        raise RuntimeError(f"BBDuk failed for {len(failed)} sample(s): {', '.join(failed)}")

def process_study(
    args: argparse.Namespace,
    study_dir: Path,
    study: str,
    studies: list[str],
    samples: list[str],
    utility_paths: dict[str, str],
    status_subs: Sequence[common.StatusLike],
) -> bool:
    bb_out = study_dir / args.bbduk_out
    if not common.resume_allowed(args, [bb_out], study):
        return False
    sample_lists = common.get_split_size(args.split_size, samples)
    try:
        common.run_concurrently(trim_reads, args.split_size, sample_lists, status_subs, study, studies, study_dir / args.reads_dir, bb_out, utility_paths, args.threads)
    finally:
        n_results = write_bbduk_results(bb_out, study_dir / "bb_out_counts.txt")
        logger.info("%s Collected %d BBDUk results lines for %s", common.EMOJI_SPARKLE, n_results, escape(study))
    return True

def main() -> int:
    parser = argparse.ArgumentParser(description="Trimming SE or PE reads with BBDuk...")
    common.add_common_args(parser)
    parser.add_argument("-r", "--reads_dir", default="raw_reads", help="Input directory name containing reads. (Default: raw_reads)")
    parser.add_argument("-o", "--bbduk_out", default="bb_out", help="Output directory name for BBDuk filtered reads. (Default: bb_out)")

    return study_runner.run_studies(parser.parse_args(), "Trimming reads with BBDuk...", "BBDuk", REQUIRED_KEYS, process_study)

if __name__ == "__main__":
    sys.exit(main())