import argparse
import json
import logging
import os
import subprocess
from pathlib import Path

import common
import pytest


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Everything is fine", logging.INFO),
        ("Warning: low memory", logging.WARNING),
        ("java.lang.Exception: boom", logging.ERROR),
        ("Error: file not found", logging.ERROR),
        ("0 errors", logging.INFO),
        ("Warnings: 0", logging.INFO),
        ("Result: 100 reads", logging.INFO),
    ],
)
def test_classify_line(line: str, expected: int) -> None:
    assert common.classify_line(line) == expected


def test_get_split_size_round_robin() -> None:
    assert common.get_split_size(2, ["a", "b", "c", "d", "e"]) == [["a", "c", "e"], ["b", "d"]]


def test_get_split_size_single_is_list_of_lists() -> None:
    assert common.get_split_size(1, ["a", "b"]) == [["a", "b"]]


def test_get_split_size_more_splits_than_samples() -> None:
    assert common.get_split_size(3, ["a"]) == [["a"], [], []]


def test_get_split_size_invalid() -> None:
    with pytest.raises(ValueError, match="split_size"):
        common.get_split_size(0, ["a"])


def test_get_unique_items() -> None:
    assert common.get_unique_items(["a_R1", "a_R2", "b_R1"], pattern=r"_R[12]$") == ["a", "b"]
    assert common.get_unique_items(["b", "a", "b"]) == ["a", "b"]


def test_find_reads_pe_no_prefix_collision(tmp_path: Path) -> None:
    for name in ["S1_R1.fastq.gz", "S1_R2.fastq.gz", "S10_R1.fastq.gz", "S10_R2.fastq.gz"]:
        (tmp_path / name).touch()
    reads = common.find_reads(tmp_path, "S1")
    assert [r.name for r in reads] == ["S1_R1.fastq.gz", "S1_R2.fastq.gz"]


def test_find_reads_se_and_missing(tmp_path: Path) -> None:
    (tmp_path / "S2.fq.gz").touch()
    assert [r.name for r in common.find_reads(tmp_path, "S2")] == ["S2.fq.gz"]
    assert common.find_reads(tmp_path, "S3") == []


def test_find_base_dirs(tmp_path: Path) -> None:
    (tmp_path / "studies_list.txt").touch()
    assert common.find_base_dirs(tmp_path, "studies_list.txt") == [tmp_path.resolve()]


def test_find_base_dirs_subdirs_and_stray_file(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").touch()  # a plain file must not crash the search
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "studies_list.txt").touch()
    (tmp_path / "b").mkdir()
    assert common.find_base_dirs(tmp_path, "studies_list.txt") == [(tmp_path / "a").resolve()]


def test_find_base_dirs_missing_dir(tmp_path: Path) -> None:
    assert common.find_base_dirs(tmp_path / "nope", "studies_list.txt") == []


def test_run_command_success_and_log_file(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    assert common.run_command("echo hello; echo 'Warning: x'; echo 'Error: y'", log_file=log) == 0
    assert log.read_text().splitlines() == ["hello", "Warning: x", "Error: y"]


def test_run_command_logs_tool_lines_with_levels() -> None:
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collect()
    common.cmd_logger.addHandler(handler)
    common.cmd_logger.setLevel(logging.DEBUG)
    try:
        common.run_command("echo hello; echo 'Warning: x'; echo 'Error: y'")
    finally:
        common.cmd_logger.removeHandler(handler)
    assert [(r.getMessage(), r.levelno) for r in records] == [
        ("$ echo hello; echo 'Warning: x'; echo 'Error: y'", logging.INFO),  # the command itself
        ("hello", logging.INFO),
        ("Warning: x", logging.WARNING),
        ("Error: y", logging.ERROR),
    ]


def test_run_log_file_has_raw_tool_output(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    common.setup_logging(log_file=log)
    common.run_command("echo '[bold]raw[/bold] line'")
    for handler in common.logger.handlers:
        handler.flush()
    assert "[bold]raw[/bold] line" in log.read_text()


def test_run_command_pipefail_catches_failure_before_tee() -> None:
    assert common.run_command("false | cat") != 0


def test_run_command_markup_in_output_does_not_crash() -> None:
    assert common.run_command("echo '[/oops] [bold]x'") == 0


def test_run_command_check_raises() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        common.run_command("exit 3", check=True)


def test_run_concurrently_single_and_error_handling() -> None:
    calls: list[list[str]] = []

    def ok(sub_list: list[str], status: object) -> None:
        calls.append(sub_list)

    def boom(sub_list: list[str], status: object) -> None:
        raise ValueError("bad sample")

    common.run_concurrently(ok, 1, [["a", "b"]], [None])  # type: ignore[list-item]
    assert calls == [["a", "b"]]

    with pytest.raises(RuntimeError, match="2 of 2"):
        common.run_concurrently(boom, 2, [["a"], ["b"]], [None, None], stagger=0)  # type: ignore[list-item]
    # Not raising keeps the old "log and continue" behaviour
    common.run_concurrently(boom, 2, [["a"], ["b"]], [None, None], stagger=0, raise_on_error=False)  # type: ignore[list-item]


def test_setup_logging_file_is_plain_text(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    common.setup_logging(log_file=log)
    common.logger.info("%s [bold]hello[/bold]", common.EMOJI_CHECK)
    for handler in common.logger.handlers:
        handler.flush()
    text = log.read_text()
    assert "hello" in text
    assert "[bold]" not in text
    assert "[green]" not in text


def test_get_f_d_names(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.touch()
    assert common.get_f_d_names(f) == (tmp_path.name, "x.txt")
    assert common.get_f_d_names(tmp_path) == tmp_path.name
    with pytest.raises(FileNotFoundError):
        common.get_f_d_names(tmp_path / "missing")


def test_expand_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/anwesh")
    monkeypatch.setenv("DB_ROOT", "/data/dbs")
    assert common.expand_path("~/hostile/idx") == "/home/anwesh/hostile/idx"
    assert common.expand_path("$DB_ROOT/idx") == "/data/dbs/idx"
    assert common.expand_path("human-t2t-hla.argos-bt2-minimap2") == "human-t2t-hla.argos-bt2-minimap2"  # index names pass through
    assert common.expand_path("/abs/path") == "/abs/path"


def test_null_status_accepts_update_kwargs() -> None:
    common.NullStatus().update("text", spinner="point", spinner_style="magenta")


def test_marker_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "bb_out"
    outputs = [out / "S1_R1.fq.gz"]
    out.mkdir()
    outputs[0].touch()
    assert not common.is_done(out, "S1", outputs)  # outputs alone are not trusted
    common.mark_done(out, "S1", "bbduk", "bbduk.sh in=x")
    assert common.is_done(out, "S1", outputs)
    record = json.loads((out / ".done" / "S1.json").read_text())
    assert record["stage"] == "bbduk"
    assert record["host"] == common.sys_name
    outputs[0].unlink()
    assert not common.is_done(out, "S1", outputs)  # marker without outputs: redo


def test_prepare_redo_removes_marker_and_partial_outputs(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.mkdir()
    partial = out / "S1.fq.gz"
    partial.touch()
    common.mark_done(out, "S1", "x", "cmd")
    common.prepare_redo(out, "S1", [partial])
    assert not partial.exists()
    assert not (out / ".done" / "S1.json").exists()


def test_resume_allowed(tmp_path: Path) -> None:
    out = tmp_path / "bb_out"
    out.mkdir()
    no_resume, resume = argparse.Namespace(resume=False), argparse.Namespace(resume=True)
    assert common.resume_allowed(no_resume, [out], "PRJ1")  # nothing there yet
    common.mark_done(out, "S1", "bbduk", "cmd")
    assert not common.resume_allowed(no_resume, [out], "PRJ1")
    assert common.resume_allowed(resume, [out], "PRJ1")
    other = tmp_path / "old_results"
    other.mkdir()
    (other / "S1_R1.fq.gz").touch()  # outputs of an older script, no marker
    assert not common.resume_allowed(no_resume, [other], "PRJ1")


def test_link_filtered_reads_pe_and_se(tmp_path: Path) -> None:
    src = tmp_path / "hostile_out"
    src.mkdir()
    pe = [src / "S1_R1.clean_1.fastq.gz", src / "S1_R2.clean_2.fastq.gz"]
    se = [src / "S2.clean.fastq.gz"]
    for f in (*pe, *se):
        f.write_text(f.name)
    dest = tmp_path / "filtered_reads"
    links = common.link_filtered_reads(pe, dest, "S1") + common.link_filtered_reads(se, dest, "S2")
    assert sorted(p.name for p in dest.iterdir()) == ["S1_R1.fq.gz", "S1_R2.fq.gz", "S2.fq.gz"]
    assert links[0].read_text() == "S1_R1.clean_1.fastq.gz"
    assert not Path(os.readlink(links[0])).is_absolute()  # relative link survives moving the study dir
    assert [r.name for r in common.find_reads(dest, "S1")] == ["S1_R1.fq.gz", "S1_R2.fq.gz"]
    common.link_filtered_reads(pe, dest, "S1")  # idempotent: refreshes its own links


def test_link_filtered_reads_never_replaces_real_files(tmp_path: Path) -> None:
    src = tmp_path / "s"
    dest = tmp_path / "d"
    src.mkdir()
    dest.mkdir()
    (src / "a.fq.gz").touch()
    (dest / "S1.fq.gz").write_text("precious")
    with pytest.raises(FileExistsError):
        common.link_filtered_reads([src / "a.fq.gz"], dest, "S1")
    assert (dest / "S1.fq.gz").read_text() == "precious"


def test_run_command_does_not_start_when_stop_requested(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    common.STOP.set()
    assert common.run_command(f"touch {marker}") == 130
    assert not marker.exists()


def test_interrupt_in_one_worker_stops_the_others() -> None:
    import threading
    import time

    saw_stop = threading.Event()
    started = threading.Event()

    def interrupted(sub_list: list[str], status: object) -> None:
        started.wait(timeout=5)  # make sure the other worker is really running
        raise KeyboardInterrupt

    def long_running(sub_list: list[str], status: object) -> None:
        started.set()
        while not common.STOP.is_set():
            time.sleep(0.01)
        saw_stop.set()

    def worker(sub_list: list[str], status: object) -> None:
        (interrupted if sub_list == ["a"] else long_running)(sub_list, status)

    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        common.run_concurrently(worker, 2, [["a"], ["b"]], [None, None], stagger=0)  # type: ignore[list-item]
    assert saw_stop.is_set()
    assert time.monotonic() - start < 5


def test_markers_with_suffix_are_independent(tmp_path: Path) -> None:
    out = tmp_path / "kraken_out"
    result = out / "S1" / "S1.report"
    result.parent.mkdir(parents=True)
    result.touch()
    common.mark_done(out, "S1", "kraken", "cmd", suffix="kraken")
    assert common.is_done(out, "S1", [result], suffix="kraken")
    assert not common.is_done(out, "S1", [result], suffix="bracken")
    assert not common.is_done(out, "S1", [result])  # the plain marker is a different file
    common.prepare_redo(out, "S1", [result], suffix="kraken")
    assert not common.is_done(out, "S1", [result], suffix="kraken")


def test_system_info_usable_ram() -> None:
    info = common.SystemInfo("node1", 8, 128.0, 100.0, None)
    assert info.usable_ram_gb == 100.0
    assert common.SystemInfo("node1", 8, 128.0, 100.0, 64.0).usable_ram_gb == 64.0
    assert common.SystemInfo("node1", 8, 128.0, 30.0, 64.0).usable_ram_gb == 30.0


def test_meminfo_parsing(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16777216 kB\nMemAvailable:    8388608 kB\n")
    assert common._meminfo_gb("MemTotal", meminfo) == 16.0
    assert common._meminfo_gb("MemAvailable", meminfo) == 8.0
    assert common._meminfo_gb("Nope", meminfo) == 0.0
    assert common._meminfo_gb("MemTotal", tmp_path / "missing") == 0.0


def test_memory_limit_none_on_a_workstation(tmp_path: Path) -> None:
    """No SLURM variable and no cgroup limit: nothing caps the memory."""
    assert common._memory_limit_gb({}, [str(tmp_path / "missing")]) is None
    unlimited = tmp_path / "memory.max"
    unlimited.write_text("max\n")  # cgroup v2 without a limit
    assert common._memory_limit_gb({}, [str(unlimited)]) is None


def test_memory_limit_from_slurm_and_cgroup(tmp_path: Path) -> None:
    assert common._memory_limit_gb({"SLURM_MEM_PER_NODE": "65536"}, []) == 64.0  # MB per node
    limited = tmp_path / "memory.max"
    limited.write_text(str(32 * 1024**3))
    assert common._memory_limit_gb({}, [str(limited)]) == 32.0
    assert common._memory_limit_gb({"SLURM_MEM_PER_NODE": "65536"}, [str(limited)]) == 32.0  # the smaller wins


def test_system_info_falls_back_to_psutil_without_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "_meminfo_gb", lambda field, path=None: 0.0)
    monkeypatch.setattr(common, "_psutil_ram_gb", lambda: (16.0, 9.0))
    info = common.get_system_info()
    assert (info.ram_total_gb, info.ram_available_gb) == (16.0, 9.0)


def test_get_system_info_is_sane() -> None:
    info = common.get_system_info()
    assert info.cpus >= 1
    assert info.hostname == common.sys_name
    assert 0.0 <= info.usable_ram_gb <= max(info.ram_total_gb, info.usable_ram_gb)


# ---- shared step runner and merged tables ------------------------------------------------
def test_collect_files_skips_identical_and_flags_changed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    src = tmp_path / "S1_mpa.txt"
    src.write_text("one")
    dest = tmp_path / "shared"
    assert common.collect_files([src], dest) == 1
    stamp = (dest / "S1_mpa.txt").stat().st_mtime_ns
    assert common.collect_files([src], dest) == 0
    assert (dest / "S1_mpa.txt").stat().st_mtime_ns == stamp  # untouched: the merge relies on timestamps
    src.write_text("changed")
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        assert common.collect_files([src], dest) == 1
    assert "Overwriting" in caplog.text and (dest / "S1_mpa.txt").read_text() == "changed"


def test_merge_files_runs_skips_when_up_to_date_and_cleans_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = [tmp_path / "a.txt"]
    inputs[0].write_text("x")
    merged = tmp_path / "merged.txt"
    calls: list[str] = []

    def fake(command: str, **kwargs: object) -> int:
        calls.append(command)
        merged.write_text("partial")
        return 0 if command == "ok" else 1

    monkeypatch.setattr(common, "run_command", fake)
    assert common.merge_files([], merged, "ok", "profiles") is False  # nothing to merge
    assert common.merge_files(inputs, merged, "ok", "profiles") and calls == ["ok"]
    assert common.merge_files(inputs, merged, "ok", "profiles") and calls == ["ok"]  # up to date
    merged.unlink()
    assert common.merge_files(inputs, merged, "fail", "profiles") is False
    assert not merged.exists()  # no partial table left behind


def test_run_step_skips_done_and_removes_outputs_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "S1.txt"

    def fake(command: str, **kwargs: object) -> int:
        out.write_text("x")
        return 0 if command == "ok" else 1

    monkeypatch.setattr(common, "run_command", fake)
    assert common.run_step(tmp_path, "S1", "step", [out], "fail", "S1") is False
    assert not out.exists()
    assert common.run_step(tmp_path, "S1", "step", [out], "ok", "S1")
    assert (tmp_path / ".done" / "S1.step.json").exists()
    monkeypatch.setattr(common, "run_command", lambda command, **kwargs: pytest.fail("should be skipped"))
    assert common.run_step(tmp_path, "S1", "step", [out], "ok", "S1")
