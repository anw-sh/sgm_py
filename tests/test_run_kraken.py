import argparse
import logging
import os
import sys
from pathlib import Path

import common
import pytest
import run_kraken as rk
from conftest import FakeTools

UTILS = {"Kraken2": "k2env", "Bracken": "brenv", "krakenTools": "ktenv", "kraken_DB": "/db/main"}
SIZE = 109.0  # GB, the standard database


# ---- memory plan ---------------------------------------------------------------------------
def test_database_in_shm_needs_no_free_ram_and_runs_all_workers_with_memory_mapping(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="pipeline"):
        assert rk.plan_kraken(12, SIZE, True, 3) == (3, True)  # 12 GB free is irrelevant: the database is already in RAM
    assert "/dev/shm" in caplog.text and "WARNING" not in caplog.text


def test_disk_database_with_plenty_of_ram_runs_all_workers_without_mapping(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="pipeline"):
        assert rk.plan_kraken(400, SIZE, False, 3) == (3, False)  # three copies of 109.5 GB fit
    assert "WARNING" not in caplog.text


def test_disk_database_limits_workers_to_the_copies_that_fit(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert rk.plan_kraken(120, SIZE, False, 3) == (1, False)
    assert "running 1 at a time instead of 3" in caplog.text
    assert rk.plan_kraken(230, SIZE, False, 3) == (2, False)


def test_size_plus_margin_is_needed() -> None:
    assert rk.plan_kraken(SIZE + rk.MARGIN_GB + 0.1, SIZE, False, 1)[0] == 1
    assert rk.plan_kraken(SIZE + rk.MARGIN_GB - 0.1, SIZE, False, 1)[0] == 0  # the margin is needed on top of the database size


def test_not_enough_memory_returns_zero_and_logs_an_error(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        assert rk.plan_kraken(64, SIZE, False, 2) == (0, False)
    assert "Not enough memory" in caplog.text and "64 GB" in caplog.text


def test_unknown_ram_runs_one_sample_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert rk.plan_kraken(0, SIZE, False, 3) == (1, False)
    assert "could not be detected" in caplog.text


def make_db(path: Path, name_ok: bool = True) -> str:
    path.mkdir(parents=True)
    for name in rk.DB_FILES if name_ok else rk.DB_FILES[:1]:
        (path / name).write_bytes(b"x" * 10)
    return str(path)


def test_db_files_are_the_kraken2_index_names() -> None:
    """Literal names: the tests below build databases from rk.DB_FILES, so a typo there would go unnoticed."""
    assert set(rk.DB_FILES) == {"hash.k2d", "opts.k2d", "taxo.k2d"}


def test_db_info_sums_index_files_and_detects_shm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = rk.db_info(make_db(tmp_path / "db"))
    assert found is not None and found[0] == pytest.approx(30 / 1024**3) and found[1] is False
    assert rk.db_info(make_db(tmp_path / "partial", name_ok=False)) is None  # opts/taxo missing
    assert rk.db_info(str(tmp_path / "nowhere")) is None
    monkeypatch.setattr(rk, "SHM_DIR", tmp_path)  # pretend tmp_path is /dev/shm
    inside = rk.db_info(make_db(tmp_path / "copy"))
    assert inside is not None and inside[1] is True
    for name in rk.DB_FILES:  # the files directly in /dev/shm, not in a subdirectory
        (tmp_path / name).write_bytes(b"x")
    direct = rk.db_info(str(tmp_path))
    assert direct is not None and direct[1] is True


def test_setup_stores_decision_and_stops_without_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "get_system_info", lambda: common.SystemInfo("n1", 8, 64.0, 60.0, None))
    args = argparse.Namespace(split_size=2)
    assert rk.setup(args, {"kraken_DB": make_db(tmp_path / "db")})
    assert (args.kraken_db, args.kraken_workers, args.kraken_mapping) == (str(tmp_path / "db"), 2, False)  # tiny test database: all fit
    assert rk.setup(argparse.Namespace(split_size=1), {"kraken_DB": str(tmp_path / "nowhere")}) is False


def test_setup_refuses_when_there_is_not_enough_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(rk, "db_info", lambda path: (SIZE, False))
    monkeypatch.setattr(common, "get_system_info", lambda: common.SystemInfo("n1", 8, 64.0, 20.0, None))
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        assert rk.setup(argparse.Namespace(split_size=1), {"kraken_DB": make_db(tmp_path / "db")}) is False
    assert "Not enough memory" in caplog.text


# ---- --confidence --------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["0", "0.0", "0.1", "1", "1.0"])
def test_confidence_accepts_values_from_0_to_1(text: str) -> None:
    assert rk.confidence_value(text) == float(text)


@pytest.mark.parametrize("text", ["-0.1", "1.1", "abc", "", "nan"])
def test_confidence_rejects_anything_else(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        rk.confidence_value(text)


# ---- commands ------------------------------------------------------------------------------
def test_build_kraken_command_pe_se_memory_mapping_and_confidence() -> None:
    pe = rk.build_kraken_command([Path("/r/S1_R1.fq.gz"), Path("/r/S1_R2.fq.gz")], "/db/main", Path("/o/S1.out"), Path("/o/S1.report"), 8, True, 0.1)
    assert pe == "k2 classify --db /db/main --memory-mapping --threads 8 --confidence 0.1 --paired --output /o/S1.out --report /o/S1.report --use-names /r/S1_R1.fq.gz /r/S1_R2.fq.gz"
    se = rk.build_kraken_command([Path("/r/S1.fq.gz")], "/db/main", Path("/o/S1.out"), Path("/o/S1.report"), 8, False, 0.0)
    assert "--paired" not in se and "--memory-mapping" not in se and "--confidence 0 " in se and se.endswith("--use-names /r/S1.fq.gz")


# ---- per-sample steps ----------------------------------------------------------------------
def make_reads(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).touch()
    return directory


def classify(tmp_path: Path, samples: list[str]) -> None:
    rk.classify_reads(samples, common.NullStatus(), "PRJ1", ["PRJ1"], tmp_path / "filtered_reads", tmp_path / "kraken_out", UTILS, "/db/main", 4, True, 0.25)


def test_classify_pe_se_and_skip(tmp_path: Path, fake_tools: FakeTools) -> None:
    make_reads(tmp_path / "filtered_reads", "S1_R1.fq.gz", "S1_R2.fq.gz", "S2.fq.gz")
    classify(tmp_path, ["S1", "S2"])
    assert fake_tools.tools == ["k2", "k2"]
    assert "--paired" in fake_tools.commands[0] and "--paired" not in fake_tools.commands[1]
    assert all("--confidence 0.25 " in c for c in fake_tools.commands)
    assert (tmp_path / "kraken_out" / "S1" / "S1.report").exists()
    classify(tmp_path, ["S1", "S2"])
    assert fake_tools.tools == ["k2", "k2"]  # both skipped


def test_classify_failure_cleans_up_and_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_reads(tmp_path / "filtered_reads", "S1_R1.fq.gz", "S1_R2.fq.gz")
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="k2"))
    with pytest.raises(RuntimeError, match="S1"):
        classify(tmp_path, ["S1"])
    assert not (tmp_path / "kraken_out" / "S1" / "S1.report").exists()


def test_classify_without_reads_is_an_error(tmp_path: Path, fake_tools: FakeTools) -> None:
    (tmp_path / "filtered_reads").mkdir()
    with pytest.raises(RuntimeError, match="S1"):
        classify(tmp_path, ["S1"])
    assert fake_tools.tools == []


def test_bracken_and_mpa_steps(tmp_path: Path, fake_tools: FakeTools) -> None:
    out = tmp_path / "kraken_out"
    (out / "S1").mkdir(parents=True)
    (out / "S1" / "S1.report").touch()
    rk.estimate_abundance(["S1"], common.NullStatus(), "PRJ1", ["PRJ1"], out, UTILS, "/db/main")
    rk.convert_to_mpa(["S1"], common.NullStatus(), "PRJ1", ["PRJ1"], out, UTILS)
    assert fake_tools.tools == ["bracken", "kreport2mpa.py"]
    assert "bracken -d /db/main -i " in fake_tools.commands[0]
    assert "S1_bracken_species.report" in fake_tools.commands[1]
    assert (out / "S1" / "S1_mpa.txt").exists()
    assert {p.name for p in (out / ".done").iterdir()} == {"S1.bracken.json", "S1.krakenTools.json"}


# ---- common directory and merge ------------------------------------------------------------
def merge(tmp_path: Path, studies: list[str] | None = None) -> bool:
    return rk.merge_mpa(argparse.Namespace(), tmp_path, studies or [], UTILS)


def test_merge_command_and_up_to_date_check(tmp_path: Path, fake_tools: FakeTools) -> None:
    dest = tmp_path / "k2mpa_files"
    dest.mkdir()
    for name in ("S2_mpa.txt", "S1_mpa.txt"):
        (dest / name).write_text("x")
    assert merge(tmp_path)
    cmd = fake_tools.commands[0]
    assert cmd.startswith(f"cd {dest} && {common.ENV_RUNNER} ktenv ")
    assert f"-n ktenv combine_mpa_mod.py -i S1_mpa.txt S2_mpa.txt -o {tmp_path}/k2mpa_merged.txt" in cmd  # part of KrakenTools
    assert (tmp_path / "k2mpa_merged.txt").exists()

    assert merge(tmp_path) and len(fake_tools.commands) == 1  # up to date: not run again
    newer = dest / "S3_mpa.txt"
    newer.write_text("x")
    os.utime(newer, (os.path.getmtime(tmp_path / "k2mpa_merged.txt") + 10,) * 2)
    assert merge(tmp_path) and len(fake_tools.commands) == 2  # a new sample: merged again


def test_merge_failure_and_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert merge(tmp_path) is False  # nothing to merge
    dest = tmp_path / "k2mpa_files"
    dest.mkdir()
    (dest / "S1_mpa.txt").write_text("x")
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="combine_mpa_mod.py"))
    assert merge(tmp_path) is False
    assert not (tmp_path / "k2mpa_merged.txt").exists()


# ---- whole script --------------------------------------------------------------------------
def make_project(tmp_path: Path) -> Path:
    base = tmp_path / "all_data"
    (base / "studies_list.txt").parent.mkdir()
    (base / "studies_list.txt").write_text("PRJ1\nPRJ2\n")
    for study, samples in (("PRJ1", ["S1", "S2"]), ("PRJ2", ["S3"])):
        (base / study).mkdir()
        (base / study / "samples_list.txt").write_text("\n".join(samples) + "\n")
        for sample in samples:
            make_reads(base / study / "filtered_reads", f"{sample}_R1.fq.gz", f"{sample}_R2.fq.gz")
    csv = tmp_path / "utility_paths.csv"
    utils = {**UTILS, "kraken_DB": make_db(tmp_path / "db")}  # tiny stand-in
    csv.write_text("Tool,Value\n" + "".join(f"{k},{v}\n" for k, v in utils.items()))
    return base


def run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["run_kraken.py", "-b", str(tmp_path / "all_data"), "-u", str(tmp_path / "utility_paths.csv"), *extra])
    return rk.main()


def real_sized_db(monkeypatch: pytest.MonkeyPatch, in_shm: bool = False) -> None:
    """The stand-in database is a few bytes: report a real size (109 GB) instead."""
    monkeypatch.setattr(rk, "db_info", lambda path: (SIZE, in_shm))


def ram(monkeypatch: pytest.MonkeyPatch, free_gb: float) -> None:
    monkeypatch.setattr(common, "get_system_info", lambda: common.SystemInfo("n1", 64, free_gb * 2, free_gb, None))


def classify_commands(fake_tools: FakeTools) -> list[str]:
    return [c for c in fake_tools.commands if " k2 classify" in c]


def test_full_run_on_disk_copies_merges_and_resumes(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    base = make_project(tmp_path)
    real_sized_db(monkeypatch)
    ram(monkeypatch, 400)
    assert run_main(monkeypatch, tmp_path, "-l", "2") == 0
    assert fake_tools.tools == ["k2", "k2", "bracken", "bracken", "kreport2mpa.py", "kreport2mpa.py", "k2", "bracken", "kreport2mpa.py", "combine_mpa_mod.py"]
    assert all(f"--db {tmp_path}/db " in c and "--memory-mapping" not in c for c in classify_commands(fake_tools))
    assert sorted(p.name for p in (base / "k2mpa_files").iterdir()) == ["S1_mpa.txt", "S2_mpa.txt", "S3_mpa.txt"]
    assert (base / "k2mpa_merged.txt").exists()

    fake_tools.tools.clear()
    assert run_main(monkeypatch, tmp_path) == 1  # results exist: needs --resume
    assert run_main(monkeypatch, tmp_path, "--resume") == 0
    assert fake_tools.tools == []


def test_confidence_option_reaches_every_classification(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    make_project(tmp_path)
    ram(monkeypatch, 400)
    assert run_main(monkeypatch, tmp_path, "--confidence", "0.2") == 0
    assert len(classify_commands(fake_tools)) == 3 and all("--confidence 0.2 " in c for c in classify_commands(fake_tools))


def test_confidence_default_is_zero_and_bad_values_are_rejected(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    make_project(tmp_path)
    ram(monkeypatch, 400)
    assert run_main(monkeypatch, tmp_path) == 0
    assert all("--confidence 0 " in c for c in classify_commands(fake_tools))
    with pytest.raises(SystemExit) as excinfo:
        run_main(monkeypatch, tmp_path, "--confidence", "1.5")
    assert excinfo.value.code == 2


def test_database_in_shm_uses_memory_mapping_even_with_little_free_ram(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    make_project(tmp_path)
    real_sized_db(monkeypatch, in_shm=True)
    ram(monkeypatch, 8)
    assert run_main(monkeypatch, tmp_path, "-l", "2") == 0
    assert classify_commands(fake_tools) and all("--memory-mapping" in c for c in classify_commands(fake_tools))


def test_not_enough_ram_runs_nothing(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    make_project(tmp_path)
    real_sized_db(monkeypatch)
    ram(monkeypatch, 64)
    assert run_main(monkeypatch, tmp_path) == 1
    assert fake_tools.tools == []


def test_tight_ram_limits_parallel_runs_but_still_finishes(tmp_path: Path, fake_tools: FakeTools, monkeypatch: pytest.MonkeyPatch) -> None:
    make_project(tmp_path)
    real_sized_db(monkeypatch)
    ram(monkeypatch, 120)  # one copy fits, two do not
    assert run_main(monkeypatch, tmp_path, "-l", "2") == 0
    assert len(classify_commands(fake_tools)) == 3


def test_failed_step_skips_the_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_project(tmp_path)
    ram(monkeypatch, 60)
    monkeypatch.setattr(common, "run_command", FakeTools(fail_tool="bracken"))
    assert run_main(monkeypatch, tmp_path) == 1
    assert not (tmp_path / "all_data" / "k2mpa_merged.txt").exists()
