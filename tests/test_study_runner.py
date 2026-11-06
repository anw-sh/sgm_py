import argparse
from pathlib import Path

import study_runner


def make_project(tmp_path: Path, keys: str = "Tool,Env\nX,x_env\n") -> argparse.Namespace:
    (tmp_path / "studies_list.txt").write_text("A\nB\n")
    for study, sample in (("A", "S1"), ("B", "S2")):
        (tmp_path / study).mkdir()
        (tmp_path / study / "samples_list.txt").write_text(f"{sample}\n")
    (tmp_path / "utility_paths.csv").write_text(keys)
    return argparse.Namespace(
        base_dir=str(tmp_path), samples="samples_list.txt", projects="studies_list.txt",
        utility_paths=str(tmp_path / "utility_paths.csv"), threads=1, split_size=1, verbose=False, log_file=None,
    )


def test_all_studies_processed(tmp_path: Path) -> None:
    seen: list[tuple[str, list[str]]] = []

    def ok(args, study_dir, study, studies, samples, utility_paths, status_subs) -> bool:  # type: ignore[no-untyped-def]
        seen.append((study, samples))
        return True

    assert study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["X"], ok) == 0
    assert seen == [("A", ["S1"]), ("B", ["S2"])]


def test_failed_study_does_not_stop_the_others(tmp_path: Path) -> None:
    seen: list[str] = []

    def flaky(args, study_dir, study, studies, samples, utility_paths, status_subs) -> bool:  # type: ignore[no-untyped-def]
        seen.append(study)
        if study == "A":
            raise RuntimeError("stage failed")
        return True

    assert study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["X"], flaky) == 1
    assert seen == ["A", "B"]


def test_false_return_counts_as_failure(tmp_path: Path) -> None:
    assert study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["X"], lambda *a: False) == 1


def test_missing_required_key_returns_1(tmp_path: Path) -> None:
    assert study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["Nope"], lambda *a: True) == 1


def test_study_without_samples_is_failed(tmp_path: Path) -> None:
    args = make_project(tmp_path)
    (tmp_path / "B" / "samples_list.txt").write_text("\n")
    assert study_runner.run_studies(args, "Test", "Test", ["X"], lambda *a: True) == 1


def test_no_studies_found_returns_1(tmp_path: Path) -> None:
    args = make_project(tmp_path)
    (tmp_path / "studies_list.txt").unlink()
    assert study_runner.run_studies(args, "Test", "Test", ["X"], lambda *a: True) == 1


def test_keyboard_interrupt_returns_130(tmp_path: Path) -> None:
    def interrupted(*a):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    assert study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["X"], interrupted) == 130


def test_setup_false_stops_before_any_study(tmp_path: Path) -> None:
    seen: list[str] = []

    def process(args, study_dir, study, studies, samples, utility_paths, status_subs) -> bool:  # type: ignore[no-untyped-def]
        seen.append(study)
        return True

    code = study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["X"], process, setup=lambda args, utility_paths: False)
    assert code == 1 and seen == []


def test_finish_base_runs_once_after_all_studies(tmp_path: Path) -> None:
    calls: list[tuple[str, list[str]]] = []

    def finish(args, base, studies, utility_paths) -> bool:  # type: ignore[no-untyped-def]
        calls.append((base.name, studies))
        return True

    assert study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["X"], lambda *a: True, finish_base=finish) == 0
    assert calls == [(tmp_path.name, ["A", "B"])]


def test_finish_base_skipped_when_a_study_failed_or_fails_itself(tmp_path: Path) -> None:
    calls: list[str] = []

    def finish(args, base, studies, utility_paths) -> bool:  # type: ignore[no-untyped-def]
        calls.append("finish")
        return True

    def process_b_fails(args, study_dir, study, studies, samples, utility_paths, status_subs) -> bool:  # type: ignore[no-untyped-def]
        return study != "B"

    assert study_runner.run_studies(make_project(tmp_path), "Test", "Test", ["X"], process_b_fails, finish_base=finish) == 1
    assert calls == []  # not run on partial results
    other = tmp_path / "other"
    other.mkdir()
    assert study_runner.run_studies(make_project(other), "Test", "Test", ["X"], lambda *a: True, finish_base=lambda *a: False) == 1
