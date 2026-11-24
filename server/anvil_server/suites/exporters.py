"""Deterministic suite case reporting with no network or code execution."""

import json
import xml.etree.ElementTree as ET
from collections.abc import Sequence


def _validate(cases: Sequence[dict]) -> None:
    for case in cases:
        if (type(case) is not dict or type(case.get("name")) is not str
                or not case["name"] or any(
                    ord(character) < 32 and character not in "\t\n\r"
                    or ord(character) in (0xFFFE, 0xFFFF)
                    for character in case["name"])
                or case.get("status") not in {"pass", "fail", "inconclusive", "error", "cancelled"}):
            raise ValueError("Invalid suite case report")


def export_json(cases: Sequence[dict]) -> str:
    _validate(cases)
    return json.dumps({"cases": list(cases)}, sort_keys=True, separators=(",", ":")) + "\n"


def export_junit(cases: Sequence[dict]) -> str:
    _validate(cases)
    suite = ET.Element("testsuite", tests=str(len(cases)),
                       failures=str(sum(case["status"] == "fail" for case in cases)),
                       errors=str(sum(case["status"] in {"error", "cancelled"} for case in cases)),
                       skipped=str(sum(case["status"] == "inconclusive" for case in cases)))
    for case in cases:
        item = ET.SubElement(suite, "testcase", name=case["name"])
        if case["status"] == "fail":
            ET.SubElement(item, "failure", message="Suite assertion failed")
        elif case["status"] in {"error", "cancelled"}:
            ET.SubElement(item, "error", message=case["status"])
        elif case["status"] == "inconclusive":
            ET.SubElement(item, "skipped", message="Evidence unavailable")
    return ET.tostring(suite, encoding="unicode") + "\n"


def export_markdown(cases: Sequence[dict]) -> str:
    _validate(cases)
    lines = ["| Case | Status |", "| --- | --- |"]
    for case in cases:
        name = case["name"].replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        lines.append(f"| {name} | {case['status']} |")
    return "\n".join(lines) + "\n"
