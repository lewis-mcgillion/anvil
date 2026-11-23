import json
import xml.etree.ElementTree as ET

import pytest

from anvil_server.suites.exporters import export_json, export_junit, export_markdown


def test_reports_preserve_unavailable_and_failed_cases():
    cases = [{"name": "passing", "status": "pass"},
             {"name": "incomplete", "status": "inconclusive"},
             {"name": "failed", "status": "fail"}]
    assert json.loads(export_json(cases))["cases"] == cases
    junit = ET.fromstring(export_junit(cases))
    assert junit.attrib["failures"] == "1" and junit.attrib["skipped"] == "1"
    assert junit.find("testcase[@name='incomplete']/skipped") is not None
    assert "| incomplete | inconclusive |" in export_markdown(cases)


def test_reports_reject_unknown_status():
    for exporter in (export_json, export_junit, export_markdown):
        with pytest.raises(ValueError):
            exporter([{"name": "unsafe", "status": "unknown"}])


@pytest.mark.parametrize("format,expected", [
    ("json", '"status":"fail"'),
    ("junit", 'failures="1"'),
    ("markdown", "| failing | fail |"),
])
def test_cli_report_formats(tmp_path, capsys, format, expected):
    from anvil_server.cli import main

    source = tmp_path / "report.json"
    source.write_text(json.dumps({"cases": [{"name": "failing", "status": "fail"}]}))
    assert main(["report", str(source), "--format", format]) == 0
    assert expected in capsys.readouterr().out


def test_cli_report_rejects_invalid_case_and_format(tmp_path, capsys):
    from anvil_server.cli import main

    source = tmp_path / "report.json"
    source.write_text(json.dumps({"cases": [{"name": "x", "status": "unknown"}]}))
    assert main(["report", str(source)]) == 1
    assert main(["report", str(source), "--format", "invalid"]) == 1
    assert "error:" in capsys.readouterr().err
