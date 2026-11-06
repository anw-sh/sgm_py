import argparse
from pathlib import Path

import common
import pytest
import run_qc_sg as sg
from conftest import FakeTools

UTILS = {
    "FastQC": "fq", "MultiQC": "mq", "BBDuk": "bb", "bb_adapters": "/refs/a.fa",
    "fastp": "fp", "Hostile": "ho", "Hostile_DB": "/db/idx",
}
QC = ["fastqc", "multiqc"]


def make_study(tmp_path: Path, se_samples: tuple[str, ...] = ()) -> Path:
    raw = tmp_path / "PRJ1" / "raw_reads"
    raw.mkdir(parents=True)
    for sample in ("S1", "S2"):
        (raw / f"{sample}_R1.fastq.gz").touch()
        (raw / f"{sample}_R2.fastq.gz").touch()
    for sample in se_samples:
        (raw / f"{sample}.fastq.gz").touch()
    return tmp_path / "PRJ1"


def run(study_dir: Path, profile: str, *, keep: bool = False, resume: bool = False, samples: tuple[str, ...] = ("S1", "S2")) -> bool:
    args = argparse.Namespace(profile=profile, keep_intermediate=keep, resume=resume, threads=2, split_size=1)
    return sg.process_study(args, study_dir, "PRJ1", ["PRJ1"], list(samples), UTILS, [common.NullStatus()])


def linked_to(study: Path, name: str) -> str:
    return (study / "filtered_reads" / name).resolve().relative_to(study.resolve()).as_posix()


def test_human_mag_runs_all_stages_and_cleans_intermediates(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path)
    assert run(study, "human_mag")
    assert fake_tools.tools == [*QC, "bbduk.sh", "bbduk.sh", "fastp", "fastp", *QC, *QC, "hostile", "hostile"]
    assert (study / "hostile_out" / "S2_R2.clean_2.fastq.gz").exists()
    assert not list((study / "bb_out").glob("*.gz")) and not list((study / "fp_out").glob("*.gz"))
    assert (study / "bb_out" / "S1.log").exists()  # logs are kept
    assert (study / "bb_out_counts.txt").read_text().count("Result") == 2
    # final reads under common names, whatever the profile
    assert linked_to(study, "S1_R1.fq.gz") == "hostile_out/S1_R1.clean_1.fastq.gz"
    assert linked_to(study, "S2_R2.fq.gz") == "hostile_out/S2_R2.clean_2.fastq.gz"


def test_human_taxonomy_skips_fastp_and_hostile_reads_bbduk_output(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path)
    assert run(study, "human_taxonomy")
    assert fake_tools.tools == [*QC, "bbduk.sh", "bbduk.sh", *QC, "hostile", "hostile"]
    assert "bb_out/S1_R1.fq.gz" in fake_tools.commands[-2]
    assert not (study / "fp_out").exists()
    assert not list((study / "bb_out").glob("*.gz"))
    assert linked_to(study, "S1_R1.fq.gz") == "hostile_out/S1_R1.clean_1.fastq.gz"


def test_other_mag_skips_hostile_and_keeps_reads(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path)
    assert run(study, "other_mag")
    assert fake_tools.tools == [*QC, "bbduk.sh", "bbduk.sh", "fastp", "fastp", *QC, *QC]
    assert not (study / "hostile_out").exists()
    assert linked_to(study, "S1_R1.fq.gz") == "fp_out/S1_R1.fq.gz"


def test_other_taxonomy_is_bbduk_only(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path)
    assert run(study, "other_taxonomy")
    assert fake_tools.tools == [*QC, "bbduk.sh", "bbduk.sh", *QC]
    assert linked_to(study, "S2_R2.fq.gz") == "bb_out/S2_R2.fq.gz"


def test_keep_intermediate(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path)
    assert run(study, "human_mag", keep=True)
    assert (study / "bb_out" / "S1_R1.fq.gz").exists()
    assert (study / "fp_out" / "S1_R1.fq.gz").exists()


def test_single_end_sample_through_all_stages(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path, se_samples=("S3",))
    assert run(study, "human_mag", samples=("S1", "S3"))
    assert (study / "hostile_out" / "S3.clean.fastq.gz").exists()
    assert linked_to(study, "S3.fq.gz") == "hostile_out/S3.clean.fastq.gz"
    assert linked_to(study, "S1_R2.fq.gz") == "hostile_out/S1_R2.clean_2.fastq.gz"
    hostile_commands = [c for c in fake_tools.commands if " hostile " in c]
    assert "--aligner bowtie2" in hostile_commands[1] and "--aligner" not in hostile_commands[0]


def test_rerun_needs_resume_then_does_nothing(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path)
    assert run(study, "human_mag")
    fake_tools.tools.clear()
    assert run(study, "human_mag") is False  # results exist: refuse instead of guessing
    assert fake_tools.tools == []
    assert run(study, "human_mag", resume=True)
    assert fake_tools.tools == []  # nothing to redo, although the intermediates are gone


def test_resume_after_hostile_failure_redoes_only_hostile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    study = make_study(tmp_path)
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="hostile"))
    with pytest.raises(RuntimeError, match="Hostile"):
        run(study, "human_mag")
    assert (study / "fp_out" / "S1_R1.fq.gz").exists()  # intermediates kept for the resume
    assert not list((study / "hostile_out").glob("*.fastq.gz"))
    assert run(study, "human_mag") is False  # still needs --resume

    good = FakeTools()
    monkeypatch.setattr(common, "run_command", good)
    assert run(study, "human_mag", resume=True)
    assert good.tools == ["hostile", "hostile"]
    assert not list((study / "fp_out").glob("*.gz"))


def test_resume_redoes_a_stage_that_was_killed_midway(tmp_path: Path, fake_tools: FakeTools) -> None:
    study = make_study(tmp_path)
    assert run(study, "other_mag")
    (study / "fp_out" / ".done" / "S2.json").unlink()  # simulate: marker never written for S2
    (study / "fp_out" / "S2_R1.fq.gz").write_text("truncated")
    fake_tools.tools.clear()
    assert run(study, "other_mag", resume=True)
    assert fake_tools.tools == ["fastp"]  # only S2's fastp step
    assert (study / "fp_out" / "S2_R1.fq.gz").read_text() == ""


@pytest.mark.parametrize(
    ("profile", "expected_extra"),
    [("human_mag", {"fastp", "Hostile", "Hostile_DB"}), ("human_taxonomy", {"Hostile", "Hostile_DB"}), ("other_mag", {"fastp"}), ("other_taxonomy", set())],
)
def test_required_keys(profile: str, expected_extra: set[str]) -> None:
    base = {"FastQC", "MultiQC", "BBDuk", "bb_adapters"}
    assert set(sg.required_keys(sg.PROFILES[profile])) == base | expected_extra
