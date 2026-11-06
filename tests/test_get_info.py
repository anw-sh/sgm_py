from pathlib import Path

from get_info import make_dict, names_list


def test_names_list_skips_blank_lines(tmp_path: Path) -> None:
    f = tmp_path / "studies_list.txt"
    f.write_text("PRJ1\n\n  PRJ2  \n\n")
    assert names_list(f) == ["PRJ1", "PRJ2"]


def test_names_list_missing_file_returns_empty(tmp_path: Path) -> None:
    assert names_list(tmp_path / "nope.txt") == []


def test_make_dict(tmp_path: Path) -> None:
    f = tmp_path / "utility_paths.csv"
    f.write_text("Tool,Env\nFastQC, fastqc_env\nBBDuk,bbmap_env\n")
    assert make_dict(f) == {"FastQC": "fastqc_env", "BBDuk": "bbmap_env"}


def test_make_dict_skips_short_rows(tmp_path: Path) -> None:
    f = tmp_path / "utility_paths.csv"
    f.write_text("Tool,Env\nFastQC\nBBDuk,bbmap_env\n")
    assert make_dict(f) == {"BBDuk": "bbmap_env"}


def test_make_dict_empty_file_and_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.csv"
    f.write_text("")
    assert make_dict(f) == {}
    assert make_dict(tmp_path / "nope.csv") == {}


def test_make_dict_header_only_one_column(tmp_path: Path) -> None:
    f = tmp_path / "bad.csv"
    f.write_text("OnlyOne\nx\n")
    assert make_dict(f) == {}
