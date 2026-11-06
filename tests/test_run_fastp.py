from pathlib import Path

import common
import pytest
import run_fastp as rf
from conftest import FakeTools

UTILS = {"fastp": "fastp_env"}


def make_reads(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).touch()
    return directory


def dedup(tmp_path: Path, samples: list[str]) -> None:
    rf.dedup_reads(samples, common.NullStatus(), "PRJ1", ["PRJ1"], tmp_path / "bb_out", tmp_path / "fp_out", UTILS, 4)


def test_build_command() -> None:
    reads = [Path("/i/S1_R1.fq.gz"), Path("/i/S1_R2.fq.gz")]
    outs = [Path("/o/S1_R1.fq.gz"), Path("/o/S1_R2.fq.gz")]
    cmd = rf.build_fastp_command(reads, outs, Path("/o/S1.html"), Path("/o/S1.json"), 4)
    assert cmd == "fastp -i /i/S1_R1.fq.gz -o /o/S1_R1.fq.gz -I /i/S1_R2.fq.gz -O /o/S1_R2.fq.gz -D -A -h /o/S1.html -j /o/S1.json -w 4"


def test_dedup_runs_then_skips(tmp_path: Path, fake_tools: FakeTools) -> None:
    make_reads(tmp_path / "bb_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    dedup(tmp_path, ["S1"])
    assert fake_tools.tools == ["fastp"]
    assert (tmp_path / "fp_out" / "S1_R2.fq.gz").exists()
    assert (tmp_path / "fp_out" / "S1.json").exists()
    dedup(tmp_path, ["S1"])
    assert fake_tools.tools == ["fastp"]  # skipped the second time


def test_single_end(tmp_path: Path, fake_tools: FakeTools) -> None:
    make_reads(tmp_path / "bb_out", "S1.fq.gz")
    dedup(tmp_path, ["S1"])
    assert fake_tools.tools == ["fastp"]
    assert " -I " not in fake_tools.commands[0]
    assert (tmp_path / "fp_out" / "S1.fq.gz").exists()
    assert rf.fastp_outputs(tmp_path / "fp_out", "S1", paired=False) == [tmp_path / "fp_out" / "S1.fq.gz"]


def test_build_command_single_end() -> None:
    cmd = rf.build_fastp_command([Path("/i/S1.fq.gz")], [Path("/o/S1.fq.gz")], Path("/o/S1.html"), Path("/o/S1.json"), 2)
    assert cmd == "fastp -i /i/S1.fq.gz -o /o/S1.fq.gz -D -A -h /o/S1.html -j /o/S1.json -w 2"


def test_output_without_marker_is_redone(tmp_path: Path, fake_tools: FakeTools) -> None:
    """A killed run can leave outputs that look complete: no marker means no trust."""
    make_reads(tmp_path / "bb_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    make_reads(tmp_path / "fp_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    dedup(tmp_path, ["S1"])
    assert fake_tools.tools == ["fastp"]
    assert (tmp_path / "fp_out" / ".done" / "S1.json").exists()


def test_failure_removes_partial_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_reads(tmp_path / "bb_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="fastp"))
    with pytest.raises(RuntimeError, match="S1"):
        dedup(tmp_path, ["S1"])
    assert not list((tmp_path / "fp_out").glob("*.fq.gz"))


def test_missing_utility_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fastp"):
        rf.dedup_reads(["S1"], common.NullStatus(), "P", ["P"], tmp_path, tmp_path / "o", {}, 1)
