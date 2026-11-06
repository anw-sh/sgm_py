from pathlib import Path

import common
import pytest
import run_hostile as rh
from conftest import FakeTools

UTILS = {"Hostile": "hostile_env", "Hostile_DB": "/db/human-t2t-hla"}


def make_reads(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).touch()
    return directory


def host_removal(tmp_path: Path, samples: list[str], cleanup: list[Path] | None = None) -> None:
    rh.remove_host(samples, common.NullStatus(), "PRJ1", ["PRJ1"], tmp_path / "fp_out", tmp_path / "hostile_out", UTILS, 4, cleanup or [])


def test_output_names() -> None:
    out = Path("/o")
    assert rh.hostile_outputs(out, "S1", paired=True) == [out / "S1_R1.clean_1.fastq.gz", out / "S1_R2.clean_2.fastq.gz"]
    assert rh.hostile_outputs(out, "S1", paired=False) == [out / "S1.clean.fastq.gz"]


def test_build_command_pe_redirects_stats_to_log() -> None:
    cmd = rh.build_hostile_command([Path("/i/S1_R1.fq.gz"), Path("/i/S1_R2.fq.gz")], Path("/o"), "/db/idx", Path("/o/S1.log"), 4)
    assert cmd == "hostile clean --fastq1 /i/S1_R1.fq.gz --fastq2 /i/S1_R2.fq.gz --output /o --index /db/idx --threads 4 > /o/S1.log"


def test_build_command_se_uses_bowtie2() -> None:
    """Hostile treats a single fastq as long reads unless told otherwise."""
    cmd = rh.build_hostile_command([Path("/i/S1.fq.gz")], Path("/o"), "/db/idx", Path("/o/S1.log"), 4)
    assert cmd == "hostile clean --fastq1 /i/S1.fq.gz --aligner bowtie2 --output /o --index /db/idx --threads 4 > /o/S1.log"


def test_success_writes_marker_and_cleans_only_that_samples_inputs(tmp_path: Path, fake_tools: FakeTools) -> None:
    fp = make_reads(tmp_path / "fp_out", "S1_R1.fq.gz", "S1_R2.fq.gz", "S10_R1.fq.gz", "S10_R2.fq.gz")
    host_removal(tmp_path, ["S1"], cleanup=[fp])
    assert (tmp_path / "hostile_out" / "S1_R2.clean_2.fastq.gz").exists()
    assert (tmp_path / "hostile_out" / ".done" / "S1.json").exists()
    assert not (fp / "S1_R1.fq.gz").exists()
    assert (fp / "S10_R1.fq.gz").exists()  # prefix neighbour untouched


def test_single_end_run(tmp_path: Path, fake_tools: FakeTools) -> None:
    make_reads(tmp_path / "fp_out", "S1.fq.gz")
    host_removal(tmp_path, ["S1"])
    assert (tmp_path / "hostile_out" / "S1.clean.fastq.gz").exists()
    assert "--aligner bowtie2" in fake_tools.commands[0]


@pytest.mark.parametrize("paired", [True, False])
def test_finished_sample_is_skipped_even_if_inputs_are_gone(tmp_path: Path, fake_tools: FakeTools, paired: bool) -> None:
    (tmp_path / "fp_out").mkdir()
    out_dir = tmp_path / "hostile_out"
    outputs = rh.hostile_outputs(out_dir, "S1", paired)
    make_reads(out_dir, *[o.name for o in outputs])
    common.mark_done(out_dir, "S1", "hostile", "cmd")
    host_removal(tmp_path, ["S1"])
    assert fake_tools.tools == []


def test_outputs_without_marker_are_redone(tmp_path: Path, fake_tools: FakeTools) -> None:
    make_reads(tmp_path / "fp_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    make_reads(tmp_path / "hostile_out", "S1_R1.clean_1.fastq.gz", "S1_R2.clean_2.fastq.gz")
    host_removal(tmp_path, ["S1"])
    assert fake_tools.tools == ["hostile"]


def test_failure_keeps_inputs_and_removes_partial_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fp = make_reads(tmp_path / "fp_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="hostile"))
    with pytest.raises(RuntimeError, match="S1"):
        host_removal(tmp_path, ["S1"], cleanup=[fp])
    assert (fp / "S1_R1.fq.gz").exists()
    assert not list((tmp_path / "hostile_out").glob("*.fastq.gz"))
    assert not (tmp_path / "hostile_out" / ".done" / "S1.json").exists()


def test_missing_output_keeps_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit code 0 but no expected output file: never delete inputs."""
    fp = make_reads(tmp_path / "fp_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    monkeypatch.setattr(common, "run_command", lambda command, **kwargs: 0)
    with pytest.raises(RuntimeError, match="S1"):
        host_removal(tmp_path, ["S1"], cleanup=[fp])
    assert (fp / "S1_R2.fq.gz").exists()


def test_no_reads_is_an_error(tmp_path: Path, fake_tools: FakeTools) -> None:
    (tmp_path / "fp_out").mkdir()
    with pytest.raises(RuntimeError, match="S1"):
        host_removal(tmp_path, ["S1"])
    assert fake_tools.tools == []


def test_index_with_tilde_or_variable_is_expanded_before_quoting(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: shlex.quote('~/db/idx') is literal, so Hostile did not find the index."""
    monkeypatch.setenv("HOME", "/home/anwesh")
    make_reads(tmp_path / "fp_out", "S1_R1.fq.gz", "S1_R2.fq.gz")
    utils = {"Hostile": "hostile_env", "Hostile_DB": "~/hostile/idx"}
    rh.remove_host(["S1"], common.NullStatus(), "PRJ1", ["PRJ1"], tmp_path / "fp_out", tmp_path / "hostile_out", utils, 4)
    assert "--index /home/anwesh/hostile/idx" in fake_tools.commands[0]
    assert "~" not in fake_tools.commands[0]
