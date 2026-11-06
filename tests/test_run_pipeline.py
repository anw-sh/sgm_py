from pathlib import Path

import common
import pytest
import run_bbduk
import run_pipeline as rp
import run_qc_sg
import run_taxonomy
from conftest import FakeTools


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the module runs with recorders (exit code 0)."""
    seen: list[str] = []
    for name, script in rp.MODULES.items():
        monkeypatch.setattr(script, "run", lambda args, name=name: seen.append(name) or 0)
    return seen


def test_all_runs_every_module_in_order(calls: list[str]) -> None:
    assert rp.main(["run", "all"]) == 0
    assert calls == list(rp.MODULES)


def test_given_order_does_not_matter(calls: list[str]) -> None:
    assert rp.main(["run", "taxonomy", "qc", "qc"]) == 0
    assert calls == ["qc", "taxonomy"]


def test_failed_module_stops_the_chain(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_qc_sg, "run", lambda args: calls.append("qc") or 1)
    assert rp.main(["run", "all"]) == 1
    assert calls == ["qc"]  # taxonomy not run


def test_run_accepts_options_of_every_module(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, float]] = []
    monkeypatch.setattr(run_taxonomy, "run", lambda args: seen.append((args.profile, args.tool, args.confidence)) or 0)
    assert rp.main(["run", "all", "--profile", "human_taxonomy", "--tool", "kraken", "--confidence", "0.1"]) == 0
    assert seen == [("human_taxonomy", "kraken", 0.1)]


def test_single_step_gets_its_own_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(run_bbduk, "run", lambda args: seen.append((args.reads_dir, args.bbduk_out)) or 0)
    assert rp.main(["bbduk", "-o", "trimmed"]) == 0
    assert seen == [("raw_reads", "trimmed")]  # step defaults, not the taxonomy ones


@pytest.mark.parametrize("command", ["run", *rp.MODULES, *rp.STEPS])
def test_every_command_has_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        rp.main([command, "-h"])
    assert excinfo.value.code == 0 and "--base_dir" in capsys.readouterr().out


def test_unknown_module_and_missing_command_are_rejected() -> None:
    for argv in (["run", "amr"], ["run"], []):
        with pytest.raises(SystemExit):
            rp.main(argv)


def test_qc_then_metaphlan_end_to_end(tmp_path: Path, fake_tools: FakeTools) -> None:
    """QC writes filtered_reads/, taxonomy reads it: the hand-over between modules."""
    base = tmp_path / "all_data"
    raw = base / "PRJ1" / "raw_reads"
    raw.mkdir(parents=True)
    (base / "studies_list.txt").write_text("PRJ1\n")
    (base / "PRJ1" / "samples_list.txt").write_text("S1\nS2\n")
    for sample in ("S1", "S2"):
        (raw / f"{sample}_R1.fastq.gz").touch()
        (raw / f"{sample}_R2.fastq.gz").touch()
    db = tmp_path / "mpa"
    db.mkdir()
    (db / "mpa_vOct22_CHOCOPhlAnSGB_202212.pkl").touch()
    utils = {
        "FastQC": "fq", "MultiQC": "mq", "BBDuk": "bb", "bb_adapters": "/refs/a.fa",
        "MetaPhlAn": "mpa", "mpa_DB": str(db), "mpa_index": "mpa_vOct22_CHOCOPhlAnSGB_202212",
    }
    csv = tmp_path / "utility_paths.csv"
    csv.write_text("Tool,Value\n" + "".join(f"{k},{v}\n" for k, v in utils.items()))

    assert rp.main(["run", "all", "-b", str(base), "-u", str(csv), "--profile", "other_taxonomy"]) == 0
    assert (base / "PRJ1" / "filtered_reads" / "S1_R1.fq.gz").exists()
    metaphlan = [c for c in fake_tools.commands if " metaphlan " in c and "--input_type fastq" in c]
    assert len(metaphlan) == 2 and "filtered_reads/S1_R1.fq.gz" in metaphlan[0]
    assert (base / "mpa_merged_abs_profiles.txt").exists() and (base / "mpa_merged_rel_profiles.txt").exists()
    assert common.STOP.is_set() is False
