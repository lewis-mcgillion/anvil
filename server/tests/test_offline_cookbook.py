from pathlib import Path

from anvil_server.suites import compile_suite, parse_suite


def test_cookbook_baseline_compiles_without_execution():
    root = Path(__file__).resolve().parents[2] / "fixtures/suites"
    source = (root / "baseline.yaml").read_text()
    suite = parse_suite(source)
    first = compile_suite(source, root)
    second = compile_suite(source, root)
    assert suite.cases[0].model_mode == "recorded"
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64
