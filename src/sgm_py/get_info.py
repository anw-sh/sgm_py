#!/usr/bin/env python3
"""Read IDs from a text file and key-value pairs from a CSV file"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import common
import inflect
from rich.markup import escape

# Using logger, and rich elements from common.py
logger = common.logger
console = common.console
panel = common.Panel

# Initiate inflect engine
p = inflect.engine()

def names_list(in_file: str | Path) -> list[str]:
    """Stripped, non-blank lines from a text file"""
    path = Path(in_file).resolve()
    console.print(f"{common.EMOJI_SPARKLE} Reading IDs from [i blue]{path.parent.name} {common.EMOJI_PLAY} {path.name}[/]")
    try:
        with path.open(encoding="utf-8") as f:
            names = [line.strip() for line in f if line.strip()]
    except (OSError, UnicodeDecodeError) as e:
        logger.error("%s Error reading file: %s", common.EMOJI_CROSS, escape(str(e)))
        return []
    return names

def make_dict(in_file: str | Path) -> dict[str, str]:
    """Read first 2 columns of a CSV (header skipped) as a dict"""
    path = Path(in_file).resolve()
    pairs: dict[str, str] = {}
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None or len(header) < 2:
                logger.error("%s %s needs a header row with at least 2 columns", common.EMOJI_CROSS, escape(str(path)))
                return {}
            console.print(f"{common.EMOJI_SPARKLE} Generating [i][dim]{escape(header[0])}:[/dim] [blue]{escape(header[1])}[/blue][/i] pairs from [i dim]{escape(str(in_file))}[/]")
            for line_no, row in enumerate(reader, start=2):
                if len(row) < 2:
                    logger.warning("%s Skipping abnormal row %d in %s", common.EMOJI_WARNING, line_no, escape(path.name))
                    continue
                pairs[row[0].strip()] = row[1].strip()
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        logger.error("%s Error reading file: %s", common.EMOJI_CROSS, escape(str(e)))
        return {}
    return pairs

def main() -> None:
    parser = argparse.ArgumentParser(description="Read IDs or names from a text file and parse a CSV file to generate key-value pairs.")
    parser.add_argument("-i", "--input", help="Text file containing the IDs or names", default="studies_list.txt")
    parser.add_argument("-c", "--csv", help="List of key-value pairs as a CSV file", default="utility_paths.csv")
    args = parser.parse_args()

    common.setup_logging()
    common.print_header("Obtaining study details...")

    names = names_list(args.input)
    env_paths = make_dict(args.csv)
    if names and env_paths:
        names_count = len(names)
        in_path = Path(args.input).resolve()
        dir_name, file_name = in_path.parent.name, in_path.name
        logger.info("%s [blue bold italic]%s %s %s[/] has %d %s...\n", common.EMOJI_SPARKLE, dir_name, common.EMOJI_PLAY, file_name, names_count, p.plural("ID", names_count))

        console.print(
            panel.fit(
                f"[dim]{common.EMOJI_SPARKLE}[/] [bold]{len(env_paths)}[/] [dim]{p.plural("pair", len(env_paths))} found in {args.csv}[/]",
                title="Info",
                title_align="left",
                border_style="dim yellow",
            ),
            style="italic",
        )
        for key, value in env_paths.items():
            console.print(f"{common.EMOJI_LIST} [dim]{escape(key)}:[/]\t[blue]{escape(value)}[/]", style="bold")

        console.print(
            panel.fit(f"{common.EMOJI_CHECK} [bold green]Finished[/bold green] extracting information from input files.", title="Done", border_style="green", title_align="right"),
            style="italic",
        )
    else:
        console.print(
            panel.fit(f"{common.EMOJI_CROSS} Errors found, see above.", title="Failed", border_style="red", title_align="right"),
            style="italic red",
        )

    if __name__ == "__main__":
        main()