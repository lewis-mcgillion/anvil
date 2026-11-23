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
