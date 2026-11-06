import logging
from collections.abc import Iterator

import common
import pytest


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """setup_logging() changes global logger state: restore it after every test."""
    yield
    common.STOP.clear()
    for lg in (common.logger, common.cmd_logger):
        for handler in list(lg.handlers):
            handler.close()
        lg.handlers.clear()
        lg.setLevel(logging.NOTSET)
        lg.propagate = True


class FakeTools:
    """Stands in for common.run_command: records which tool ran and writes the files it would."""

    def __init__(self, fail_tool: str | None = None) -> None:
        self.tools: list[str] = []
        self.commands: list[str] = []
        self.fail_tool = fail_tool

    def __call__(self, command: str, **kwargs: object) -> int:
        import shlex
        from pathlib import Path

        tokens = shlex.split(command)
        if tokens[:1] == ["cd"]:  # "cd DIR && conda run ..."
            tokens = tokens[3:]
        tool = Path(tokens[5]).name  # conda run --no-capture-output -n ENV TOOL ... (common.ENV_RUNNER)
        self.tools.append(tool)
        self.commands.append(command)

        def after(flag: str) -> str:
            return tokens[tokens.index(flag) + 1]

        def stem(path: str) -> str:
            return Path(path).name.removesuffix(".gz").removesuffix(".fastq").removesuffix(".fq")

        if tool == "fastqc":
            for token in tokens:
                if token.endswith(".gz"):
                    (Path(after("-o")) / f"{stem(token)}_fastqc.html").touch()
        elif tool == "multiqc":
            out = Path(after("-o"))  # MultiQC writes a report and a data directory
            (out / "multiqc_report.html").touch()
            (out / "multiqc_data").mkdir(exist_ok=True)
        elif tool == "bbduk.sh":
            for token in tokens:
                if token.split("=", 1)[0] in ("out", "out1", "out2"):
                    Path(token.split("=", 1)[1]).touch()
            if kwargs.get("log_file") is not None:
                Path(str(kwargs["log_file"])).write_text("Result:\t100 reads (99.0%)\n")
        elif tool == "fastp":
            for flag in ("-o", "-O", "-h", "-j"):
                if flag in tokens:
                    Path(after(flag)).touch()
        elif tool == "hostile":
            out = Path(after("--output"))
            if "--fastq2" in tokens:
                (out / f"{stem(after('--fastq1'))}.clean_1.fastq.gz").touch()
                (out / f"{stem(after('--fastq2'))}.clean_2.fastq.gz").touch()
            else:
                (out / f"{stem(after('--fastq1'))}.clean.fastq.gz").touch()
        elif tool == "k2":
            Path(after("--output")).touch()
            Path(after("--report")).touch()
        elif tool == "bracken":
            out = Path(after("-o"))
            out.touch()
            (out.parent / f"{out.stem}_bracken_species.report").touch()
        elif tool in ("kreport2mpa.py", "combine_mpa_mod.py"):
            Path(after("-o")).touch()
        elif tool == "metaphlan":
            for flag in ("--bowtie2out", "-o"):
                if flag in tokens:
                    Path(after(flag)).touch()
        elif tool in ("merge_metaphlan_tables.py", "merge_metaphlan4_tables_abs.py"):
            Path(after(">")).write_text("merged\n")
        return 1 if tool == self.fail_tool else 0


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    fake = FakeTools()
    monkeypatch.setattr(common, "run_command", fake)
    return fake
