import argparse
import sys
from pathlib import Path

import common
import pytest
import run_mpa as rm
from conftest import FakeTools

INDEX = "mpa_vOct22_CHOCOPhlAnSGB_202212"
UTILS = {"MetaPhlAn": "mpa", "mpa_DB": "/db/mpa", "mpa_index": INDEX}


def make_reads(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).touch()
    return directory


def make_db(path: Path) -> str:
    path.mkdir(parents=True)
    (path / f"{INDEX}.pkl").touch()
    return str(path)


# ---- commands ------------------------------------------------------------------------------
def test_abs_command_pe_and_se() -> None:
    pe = rm.build_abs_command([Path("/r/S1_R1.fq.gz"), Path("/r/S1_R2.fq.gz")], Path("/o/S1.bowtie2.bz2"), Path("/o/S1_abs_profile.txt"), "/db/mpa", INDEX, 8)
    assert pe == (
        "metaphlan /r/S1_R1.fq.gz,/r/S1_R2.fq.gz --input_type fastq --bowtie2out /o/S1.bowtie2.bz2 --nproc 8 "
        f"-t rel_ab_w_read_stats --bowtie2db /db/mpa -x {INDEX} -o /o/S1_abs_profile.txt"
    )
    se = rm.build_abs_command([Path("/r/S1.fq.gz")], Path("/o/S1.bowtie2.bz2"), Path("/o/S1_abs_profile.txt"), "/db/mpa", INDEX, 8)
    assert se.startswith("metaphlan /r/S1.fq.gz --input_type fastq ")


def test_rel_command_reads_the_saved_mapping() -> None:
    cmd = rm.build_rel_command(Path("/o/S1.bowtie2.bz2"), Path("/o/S1_rel_profile.txt"), "/db/mpa", INDEX, 4)
    assert cmd == f"metaphlan /o/S1.bowtie2.bz2 --input_type bowtie2out --nproc 4 -t rel_ab --bowtie2db /db/mpa -x {INDEX} -o /o/S1_rel_profile.txt"


def test_setup_checks_the_index_and_expands_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    make_db(tmp_path / "mpa")
    args = argparse.Namespace()
    assert rm.setup(args, {**UTILS, "mpa_DB": "~/mpa"})
    assert args.mpa_db == str(tmp_path / "mpa")
    assert rm.setup(argparse.Namespace(), {**UTILS, "mpa_DB": "~/mpa", "mpa_index": "mpa_vJun23_typo"}) is False


# ---- per-sample steps ----------------------------------------------------------------------
def profile(tmp_path: Path, samples: list[str]) -> None:
    rm.profile_abs(samples, common.NullStatus(), "PRJ1", ["PRJ1"], tmp_path / "filtered_reads", tmp_path / "out", UTILS, "/db/mpa", 4)


def test_abs_step_pe_se_and_skip(tmp_path: Path, fake_tools: FakeTools) -> None:
    make_reads(tmp_path / "filtered_reads", "S1_R1.fq.gz", "S1_R2.fq.gz", "S2.fq.gz")
    (tmp_path / "out").mkdir()
    profile(tmp_path, ["S1", "S2"])
    assert fake_tools.tools == ["metaphlan", "metaphlan"]
    assert (tmp_path / "out" / "S1.bowtie2.bz2").exists() and (tmp_path / "out" / "S2_abs_profile.txt").exists()
    profile(tmp_path, ["S1", "S2"])
    assert len(fake_tools.tools) == 2  # both skipped


def test_unfinished_bowtie2out_is_removed_before_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MetaPhlAn will not overwrite a bowtie2out file, so an unfinished one must go first."""
    make_reads(tmp_path / "filtered_reads", "S1_R1.fq.gz", "S1_R2.fq.gz")
    out = tmp_path / "out"
    out.mkdir()
    (out / "S1.bowtie2.bz2").write_text("partial")
    seen: list[bool] = []

    def fake(command: str, **kwargs: object) -> int:
        seen.append((out / "S1.bowtie2.bz2").exists())
        return FakeTools()(command, **kwargs)

    monkeypatch.setattr(common, "run_command", fake)
    profile(tmp_path, ["S1"])
    assert seen == [False]


def test_abs_failure_and_missing_reads_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_reads(tmp_path / "filtered_reads", "S1_R1.fq.gz", "S1_R2.fq.gz")
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="metaphlan"))
    with pytest.raises(RuntimeError, match="S1"):
        profile(tmp_path, ["S1"])
    assert not list((tmp_path / "out").glob("S1*"))
    with pytest.raises(RuntimeError, match="S9"):
        profile(tmp_path, ["S9"])  # no reads


def test_rel_step(tmp_path: Path, fake_tools: FakeTools) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "S1.bowtie2.bz2").touch()
    rm.profile_rel(["S1"], common.NullStatus(), "PRJ1", ["PRJ1"], out, UTILS, "/db/mpa", 4)
    assert "--input_type bowtie2out" in fake_tools.commands[0]
    assert (out / "S1_rel_profile.txt").exists() and (out / ".done" / "S1.rel.json").exists()


# ---- merge ---------------------------------------------------------------------------------
def test_merge_uses_the_right_script_for_each_kind(tmp_path: Path, fake_tools: FakeTools) -> None:
    profiles = tmp_path / "mpa_profiles"
    profiles.mkdir()
    for name in ("S2_abs_profile.txt", "S1_abs_profile.txt", "S1_rel_profile.txt", "S2_rel_profile.txt"):
        (profiles / name).write_text("x")
    assert rm.merge_profiles(argparse.Namespace(), tmp_path, [], UTILS)
    abs_cmd, rel_cmd = fake_tools.commands
    assert abs_cmd == f"cd {profiles} && {common.ENV_RUNNER} mpa merge_metaphlan4_tables_abs.py S1_abs_profile.txt S2_abs_profile.txt > {tmp_path}/mpa_merged_abs_profiles.txt"
    assert rel_cmd.endswith(f"-n mpa merge_metaphlan_tables.py S1_rel_profile.txt S2_rel_profile.txt > {tmp_path}/mpa_merged_rel_profiles.txt")
    assert (tmp_path / "mpa_merged_abs_profiles.txt").exists() and (tmp_path / "mpa_merged_rel_profiles.txt").exists()


def test_merge_failure_of_one_kind_still_merges_the_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = tmp_path / "mpa_profiles"
    profiles.mkdir()
    (profiles / "S1_abs_profile.txt").write_text("x")
    (profiles / "S1_rel_profile.txt").write_text("x")
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="merge_metaphlan4_tables_abs.py"))
    assert rm.merge_profiles(argparse.Namespace(), tmp_path, [], UTILS) is False
    assert not (tmp_path / "mpa_merged_abs_profiles.txt").exists()
    assert (tmp_path / "mpa_merged_rel_profiles.txt").exists()


# ---- whole script --------------------------------------------------------------------------
def make_project(tmp_path: Path) -> Path:
    base = tmp_path / "all_data"
    base.mkdir()
    (base / "studies_list.txt").write_text("PRJ1\nPRJ2\n")
    for study, samples in (("PRJ1", ["S1", "S2"]), ("PRJ2", ["S3"])):
        (base / study).mkdir()
        (base / study / "samples_list.txt").write_text("\n".join(samples) + "\n")
        for sample in samples:
            make_reads(base / study / "filtered_reads", f"{sample}_R1.fq.gz", f"{sample}_R2.fq.gz")
    utils = {**UTILS, "mpa_DB": make_db(tmp_path / "mpa")}
    (tmp_path / "utility_paths.csv").write_text("Tool,Value\n" + "".join(f"{k},{v}\n" for k, v in utils.items()))
    return base


def run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["run_mpa.py", "-b", str(tmp_path / "all_data"), "-u", str(tmp_path / "utility_paths.csv"), *extra])
    return rm.main()


def test_full_run_collects_merges_and_resumes(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    base = make_project(tmp_path)
    assert run_main(monkeypatch, tmp_path, "-l", "2") == 0
    assert sorted(fake_tools.tools) == sorted(["metaphlan"] * 6 + ["merge_metaphlan4_tables_abs.py", "merge_metaphlan_tables.py"])
    assert fake_tools.tools[-2:] == ["merge_metaphlan4_tables_abs.py", "merge_metaphlan_tables.py"]  # after all studies
    assert sorted(p.name for p in (base / "mpa_profiles").iterdir()) == sorted(f"S{i}_{k}_profile.txt" for i in (1, 2, 3) for k in ("abs", "rel"))
    assert (base / "mpa_merged_abs_profiles.txt").exists() and (base / "mpa_merged_rel_profiles.txt").exists()

    fake_tools.tools.clear()
    assert run_main(monkeypatch, tmp_path) == 1  # results exist: needs --resume
    assert run_main(monkeypatch, tmp_path, "--resume") == 0
    assert fake_tools.tools == []  # everything done and the merged tables are up to date


def test_failed_study_skips_the_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = make_project(tmp_path)
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="metaphlan"))
    assert run_main(monkeypatch, tmp_path) == 1
    assert not (base / "mpa_merged_abs_profiles.txt").exists()


def test_missing_index_runs_nothing(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    make_project(tmp_path)
    (tmp_path / "mpa" / f"{INDEX}.pkl").unlink()
    assert run_main(monkeypatch, tmp_path) == 1
    assert fake_tools.tools == []
