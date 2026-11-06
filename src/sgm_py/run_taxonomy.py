#!/usr/bin/env python3
"""Taxonomy module: profile the filtered reads with Kraken2+Bracken or MetaPhlAn (--tool)."""

from __future__ import annotations

import argparse
import sys
from types import ModuleType

import common
import run_kraken
import run_mpa

logger = common.logger

# --tool -> script; each script has run(args). Both merge the profiles of all studies (MPA format).
TOOLS: dict[str, ModuleType] = {"kraken": run_kraken, "metaphlan": run_mpa}
DESCRIPTION = "Taxonomy module: profile filtered reads with Kraken2+Bracken or MetaPhlAn and merge the profiles of all studies..."


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tool",
        choices=sorted(TOOLS),
        default="metaphlan",
        help="kraken: Kraken2+Bracken; metaphlan: MetaPhlAn. (Default: metaphlan)",
    )
    parser.add_argument("-r", "--reads_dir", default="filtered_reads", help="Input directory name containing reads. (Default: filtered_reads)")
    run_kraken.add_tool_args(parser)


def run(args: argparse.Namespace) -> int:
    if args.tool != "kraken" and args.confidence != 0.0:
        logger.warning("%s --confidence is a Kraken2 option: ignored with --tool %s", common.EMOJI_WARNING, args.tool)
    code: int = TOOLS[args.tool].run(args)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    common.add_common_args(parser)
    add_args(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())