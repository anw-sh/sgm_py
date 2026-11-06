import shlex
from pathlib import Path

import common
import pytest
import run_bbduk as rb

UTILS = {"BBDuk": "bbmap_env", "bb_adapters": "/refs/adapters.fa"}


class FakeBBDuk:
    """Stands in for common.run_command: records commands, writes the output files bbduk would."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.commands: list[str] = []
        self.fail_for = fail_for

    def __call__(self, command: str, **kwargs: object) -> int:
        self.commands.append(command)
        tokens = shlex.split(command)
        outputs = [Path(t.split("=", 1)[1]) for t in tokens if t.split("=", 1)[0] in ("out", "out1", "out2")]
        for out in outputs:
            out.touch()
        log = kwargs.get("log_file")
        if log is not None:
            Path(str(log)).write_text("Result:\t100 reads (99.0%)\n")
        if self.fail_for and any(self.fail_for in str(o) for o in outputs):
            return 1
        return 0


def setup_reads(tmp_path: Path, *names: str) -> Path:
    raw = tmp_path / "raw_reads"
    raw.mkdir()
    for name in names:
        (raw / name).touch()
    return raw


def trim(tmp_path: Path, raw: Path, samples: list[str], monkeypatch: pytest.MonkeyPatch, fake: FakeBBDuk) -> None:
    monkeypatch.setattr(common, "run_command", fake)
    rb.trim_reads(samples, common.NullStatus(), "PRJ1", ["PRJ1"], raw, tmp_path / "bb_out", UTILS, 4)


def test_build_command_se_and_pe() -> None:
    se = rb.build_bbduk_command([Path("/r/S1.fq.gz")], [Path("/o/S1.fq.gz")], "/refs/a.fa", 4)
    assert se.startswith("bbduk.sh in=/r/S1.fq.gz out=/o/S1.fq.gz ref=/refs/a.fa")
    assert "tpe" not in se
    assert se.endswith("threads=4")

    pe = rb.build_bbduk_command([Path("/r/S1_1.fq.gz"), Path("/r/S1_2.fq.gz")], [Path("/o/S1_R1.fq.gz"), Path("/o/S1_R2.fq.gz")], "/refs/a.fa", 4)
    assert "in1=/r/S1_1.fq.gz in2=/r/S1_2.fq.gz out1=/o/S1_R1.fq.gz out2=/o/S1_R2.fq.gz" in pe
    assert "tpe tbo" in pe


def test_pe_run_then_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = setup_reads(tmp_path, "S1_1.fastq.gz", "S1_2.fastq.gz")
    fake = FakeBBDuk()
    trim(tmp_path, raw, ["S1"], monkeypatch, fake)
    assert len(fake.commands) == 1
    assert "bbmap_env" in fake.commands[0]
    assert (tmp_path / "bb_out" / "S1_R2.fq.gz").exists()

    trim(tmp_path, raw, ["S1"], monkeypatch, fake)
    assert len(fake.commands) == 1  # skipped


def test_se_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = setup_reads(tmp_path, "S2.fastq.gz")
    fake = FakeBBDuk()
    trim(tmp_path, raw, ["S2"], monkeypatch, fake)
    assert (tmp_path / "bb_out" / "S2.fq.gz").exists()
    assert " in=" in fake.commands[0]


def test_missing_sample_does_not_reuse_previous_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a sample with no reads used to run with the previous sample's read1/read2."""
    raw = setup_reads(tmp_path, "A_R1.fastq.gz", "A_R2.fastq.gz", "C_R1.fastq.gz", "C_R2.fastq.gz")
    fake = FakeBBDuk()
    with pytest.raises(RuntimeError, match="B"):
        trim(tmp_path, raw, ["A", "B", "C"], monkeypatch, fake)
    assert len(fake.commands) == 2  # A and C were still processed
    assert not (tmp_path / "bb_out" / "B_R1.fq.gz").exists()
    assert (tmp_path / "bb_out" / "C_R1.fq.gz").exists()


def test_failed_run_removes_partial_outputs_and_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = setup_reads(tmp_path, "S1_R1.fastq.gz", "S1_R2.fastq.gz")
    with pytest.raises(RuntimeError, match="S1"):
        trim(tmp_path, raw, ["S1"], monkeypatch, FakeBBDuk(fail_for="S1"))
    assert not list((tmp_path / "bb_out").glob("*.fq.gz"))


def test_too_many_read_files_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = setup_reads(tmp_path, "S1_L001_R1_001.fastq.gz", "S1_L001_R2_001.fastq.gz", "S1_L002_R1_001.fastq.gz", "S1_L002_R2_001.fastq.gz")
    fake = FakeBBDuk()
    with pytest.raises(RuntimeError, match="S1"):
        trim(tmp_path, raw, ["S1"], monkeypatch, fake)
    assert fake.commands == []


def test_missing_utility_key_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bb_adapters"):
        rb.trim_reads(["S1"], common.NullStatus(), "P", ["P"], tmp_path, tmp_path / "o", {"BBDuk": "x"}, 1)


def test_write_bbduk_results(tmp_path: Path) -> None:
    (tmp_path / "S1.log").write_text("noise\nResult:\t90 reads (90%)\n")
    (tmp_path / "S2.log").write_text("Result:\t80 reads (80%)\n")
    out = tmp_path / "bb_out_count.txt"
    assert rb.write_bbduk_results(tmp_path, out) == 2
    assert out.read_text().splitlines() == [f"{tmp_path}/S1.log:Result:\t90 reads (90%)", f"{tmp_path}/S2.log:Result:\t80 reads (80%)"]


def test_unmarked_outputs_are_removed_before_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BBDuk refuses to overwrite, and unmarked outputs may be truncated: they are removed first."""
    raw = setup_reads(tmp_path, "S1_R1.fastq.gz", "S1_R2.fastq.gz")
    bb = tmp_path / "bb_out"
    bb.mkdir()
    (bb / "S1_R1.fq.gz").write_text("truncated")
    (bb / "S1_R2.fq.gz").write_text("truncated")
    seen_at_start: list[bool] = []

    class Spy(FakeBBDuk):
        def __call__(self, command: str, **kwargs: object) -> int:
            seen_at_start.append((bb / "S1_R1.fq.gz").exists())
            return super().__call__(command, **kwargs)

    trim(tmp_path, raw, ["S1"], monkeypatch, Spy())
    assert seen_at_start == [False]
    assert (bb / ".done" / "S1.json").exists()
    assert (bb / "S1_R1.fq.gz").read_text() == ""  # fresh output, not the truncated one


def test_adapter_path_with_tilde_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/anwesh")
    raw = setup_reads(tmp_path, "S1_R1.fastq.gz", "S1_R2.fastq.gz")
    fake = FakeBBDuk()
    monkeypatch.setattr(common, "run_command", fake)
    utils = {"BBDuk": "bbmap_env", "bb_adapters": "~/refs/adapters.fa"}
    rb.trim_reads(["S1"], common.NullStatus(), "PRJ1", ["PRJ1"], raw, tmp_path / "bb_out", utils, 4)
    assert "ref=/home/anwesh/refs/adapters.fa" in fake.commands[0]


def test_stop_requested_skips_remaining_samples_without_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = setup_reads(tmp_path, "A_R1.fastq.gz", "A_R2.fastq.gz")
    fake = FakeBBDuk()
    common.STOP.set()
    trim(tmp_path, raw, ["A", "B"], monkeypatch, fake)  # no RuntimeError: stopping is not a failure
    assert fake.commands == []
