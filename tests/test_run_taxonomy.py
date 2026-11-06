import argparse
import logging

import pytest
import run_kraken
import run_mpa
import run_taxonomy as rt


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    rt.common.add_common_args(parser)
    rt.add_args(parser)
    return parser.parse_args(list(argv))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(run_kraken, "run", lambda args: seen.append("kraken") or 0)
    monkeypatch.setattr(run_mpa, "run", lambda args: seen.append("metaphlan") or 0)
    return seen


def test_default_tool_is_metaphlan(calls: list[str]) -> None:
    assert rt.run(parse()) == 0
    assert calls == ["metaphlan"]


def test_kraken_gets_its_confidence(calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[float] = []
    monkeypatch.setattr(run_kraken, "run", lambda args: got.append(args.confidence) or 0)
    assert rt.run(parse("--tool", "kraken", "--confidence", "0.3")) == 0
    assert got == [0.3]


def test_confidence_with_metaphlan_is_flagged(calls: list[str], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        rt.run(parse("--confidence", "0.3"))
    assert "Kraken2 option" in caplog.text and calls == ["metaphlan"]


def test_exit_code_of_the_tool_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_mpa, "run", lambda args: 130)
    assert rt.main([]) == 130


def test_bad_tool_is_rejected() -> None:
    with pytest.raises(SystemExit):
        parse("--tool", "blast")
