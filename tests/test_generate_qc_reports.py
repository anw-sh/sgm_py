import shlex
from pathlib import Path

import common
import generate_qc_reports as gq
import pytest

UTILS = {"FastQC": "fastqc_env", "MultiQC": "multiqc_env"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("S1_R1.fastq.gz", "S1_R1_fastqc.html"),
        ("S1_R1.fq.gz", "S1_R1_fastqc.html"),
        ("S2.fq.gz", "S2_fastqc.html"),
    ],
)
def test_fastqc_report(tmp_path: Path, name: str, expected: str) -> None:
    assert gq.fastqc_report(tmp_path, tmp_path / name) == tmp_path / expected


def make_reads(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).touch()


class FakeRunner:
    """Stands in for common.run_command: records commands and creates the outputs the tools would."""

    def __init__(self, qc_out: Path, mqc_out: Path, fastqc_rc: int = 0) -> None:
        self.qc_out, self.mqc_out, self.fastqc_rc = qc_out, mqc_out, fastqc_rc
        self.commands: list[str] = []

    def __call__(self, command: str, **kwargs: object) -> int:
        self.commands.append(command)
        tokens = shlex.split(command)
        if "fastqc" in tokens:
            if self.fastqc_rc == 0:
                for token in tokens:
                    if token.endswith(".gz"):
                        stem = Path(token).name.removesuffix(".gz").removesuffix(".fastq").removesuffix(".fq")
                        (self.qc_out / f"{stem}_fastqc.html").touch()
            return self.fastqc_rc
        (self.mqc_out / "multiqc_report.html").touch()  # MultiQC writes a report and a data directory
        (self.mqc_out / "multiqc_data").mkdir(exist_ok=True)
        return 0

    def tools_run(self) -> list[str]:
        return ["fastqc" if " fastqc " in c else "multiqc" for c in self.commands]


def run_qc(tmp_path: Path, samples: list[str], runner: FakeRunner, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(common, "run_command", runner)
    return gq.qc_reports(tmp_path / "reads", runner.qc_out, runner.mqc_out, common.NullStatus(), "PRJ1", ["PRJ1"], samples, 2, 1, UTILS)


def test_pending_reads_no_prefix_collision(tmp_path: Path) -> None:
    reads, qc = tmp_path / "reads", tmp_path / "qc"
    make_reads(reads, "S1_R1.fastq.gz", "S1_R2.fastq.gz", "S10_R1.fastq.gz", "S10_R2.fastq.gz")
    qc.mkdir()
    (qc / "S10_R1_fastqc.html").touch()  # S10 done, S1 is not
    pending = gq.pending_reads(reads, qc, ["S1"])
    assert [r.name for r in pending] == ["S1_R1.fastq.gz", "S1_R2.fastq.gz"]


def test_qc_reports_full_run_then_skip_then_incremental(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_reads(tmp_path / "reads", "S1_R1.fastq.gz", "S1_R2.fastq.gz", "S2.fq.gz")
    runner = FakeRunner(tmp_path / "qc", tmp_path / "mqc")

    assert run_qc(tmp_path, ["S1", "S2"], runner, monkeypatch)
    assert runner.tools_run() == ["fastqc", "multiqc"]
    assert "fastqc_env" in runner.commands[0]
    assert "-t 2" in runner.commands[0]

    runner.commands.clear()
    assert run_qc(tmp_path, ["S1", "S2"], runner, monkeypatch)
    assert runner.commands == []  # everything already done

    make_reads(tmp_path / "reads", "S3_R1.fastq.gz", "S3_R2.fastq.gz")
    assert run_qc(tmp_path, ["S1", "S2", "S3"], runner, monkeypatch)
    assert runner.tools_run() == ["fastqc", "multiqc"]  # MultiQC redone because it was stale
    assert "S3_R1" in runner.commands[0]
    assert "S1_R1" not in runner.commands[0]


def test_qc_reports_fastqc_failure_skips_multiqc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_reads(tmp_path / "reads", "S1_R1.fastq.gz", "S1_R2.fastq.gz")
    runner = FakeRunner(tmp_path / "qc", tmp_path / "mqc", fastqc_rc=1)
    assert run_qc(tmp_path, ["S1"], runner, monkeypatch) is False
    assert runner.tools_run() == ["fastqc"]


def test_qc_reports_missing_tool_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MultiQC"):
        gq.qc_reports(tmp_path, tmp_path / "qc", tmp_path / "mqc", common.NullStatus(), "P", ["P"], ["S1"], 1, 1, {"FastQC": "x"})


def test_qc_reports_quotes_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_reads(tmp_path / "reads", "S 1_R1.fastq.gz", "S 1_R2.fastq.gz")
    runner = FakeRunner(tmp_path / "qc", tmp_path / "mqc")
    assert run_qc(tmp_path, ["S 1"], runner, monkeypatch)
    assert "'" in runner.commands[0] and "S 1_R1" in runner.commands[0]
