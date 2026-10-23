#!/usr/bin/env python3
"""Structural contract tests for Anvil's ADRs and threat model.

Verifies the documentation required by ANVIL-002 keeps its promised shape:

- every ADR has the required sections (context, decision, consequences,
  alternatives, errors/cancellation/cleanup);
- the ADR index lists every ADR file exactly once (regression guard against
  a dropped index entry);
- the threat model covers the required threat scenarios (untrusted code,
  malicious recordings, secret leakage, tenant isolation), its trust
  boundaries, and the operational rules for errors, cancellation and
  resource cleanup;
- key architecture invariants (execution modes, fail-closed replay,
  broker-only credentials) are stated in the ADRs;
- all relative links between the docs resolve to existing files/sections.

Run ``python3 scripts/check_docs.py`` from the repository root.
Run ``python3 scripts/check_docs.py --self-test`` to exercise the checker's
own success/rejection/edge/regression behavior against temporary copies.
Exit code 0 means all checks passed; 1 means at least one failed.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = Path("docs/adr")
ADR_INDEX = ADR_DIR / "README.md"
THREAT_MODEL = Path("docs/threat-model.md")
ARCHITECTURE = Path("docs/architecture.md")
DOCS_INDEX = Path("docs/README.md")

ADR_REQUIRED_SECTIONS = [
    "## Context",
    "## Decision",
    "## Consequences",
    "## Alternatives considered",
    "## Errors, cancellation and cleanup",
]

THREAT_MODEL_REQUIRED_SECTIONS = [
    "## 1. Scope and security objectives",
    "## 2. System overview and trust boundaries",
    "## 3. Adversary model",
    "## 4. Threat scenarios and mitigations",
    "## 5. Errors, cancellation and resource cleanup",
    "## 6. Assumptions and open limitations",
]

# Acceptance criteria: threat scenarios must cover untrusted code, malicious
# recordings, secret leakage and tenant isolation.
THREAT_MODEL_REQUIRED_SCENARIOS = [
    "untrusted",
    "malicious recording",
    "secret",
    "tenant isolation",
]
THREAT_MODEL_REQUIRED_TERMS = [
    "Mitigations",
    "Residual risk",
    "cancellation",
    "cleanup",
    "trust boundaries",
]

# Regression guards for the invariants these ADRs exist to freeze. Each entry
# is (relative doc path, case-insensitive pattern, description).
INVARIANT_PATTERNS = [
    (ARCHITECTURE, r"recorded playback", "architecture defines recorded playback mode"),
    (ARCHITECTURE, r"recorded re-execution", "architecture defines recorded re-execution mode"),
    (ARCHITECTURE, r"live-model re-execution", "architecture defines live-model re-execution mode"),
    (ARCHITECTURE, r"fail[s]? closed", "architecture requires fail-closed behavior"),
    (ARCHITECTURE, r"production tools", "architecture states the production-tool rule"),
    (Path("docs/adr/0001-execution-modes.md"), r"never\s+.*fallback|no\s+mode\s+silently\s+falls\s+back", "execution modes never fall back to production"),
    (Path("docs/adr/0004-runner-isolation.md"), r"no\**\s+arbitrary\s+http", "runner protocol exposes no arbitrary HTTP"),
    (Path("docs/adr/0005-replay-and-fixture-matching.md"), r"rfc\s+8785", "replay canonicalization is RFC 8785"),
    (Path("docs/adr/0006-model-broker.md"), r"only\s+the\s+broker", "only the broker holds provider credentials"),
    (THREAT_MODEL, r"no\s+production\s+effect\s+from\s+replay", "threat model states the replay objective"),
]

ADR_FILE_PATTERN = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
ADR_INDEX_LINK_PATTERN = re.compile(r"\[(\d{4})\]\((\d{4}-[a-z0-9-]+\.md)\)")
RELATIVE_LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)#]+)(#[^)]*)?\)")


@dataclass(frozen=True)
class Failure:
    check: str
    detail: str


def _read(root: Path, relative: Path) -> str:
    return (root / relative).read_text(encoding="utf-8")


def check_adr_files(root: Path) -> list[Failure]:
    """Every ADR file must exist in the index and contain all sections."""
    failures: list[Failure] = []
    adr_dir = root / ADR_DIR
    if not adr_dir.is_dir():
        return [Failure("adr-files", f"missing directory {ADR_DIR}")]
    adr_files = sorted(p.name for p in adr_dir.iterdir() if ADR_FILE_PATTERN.match(p.name))
    if not adr_files:
        failures.append(Failure("adr-files", "no ADR files found"))
        return failures

    index_path = root / ADR_INDEX
    if not index_path.is_file():
        failures.append(Failure("adr-files", f"missing index {ADR_INDEX}"))
        return failures
    index_text = index_path.read_text(encoding="utf-8")
    linked = sorted(match.group(2) for match in ADR_INDEX_LINK_PATTERN.finditer(index_text))

    if sorted(adr_files) != linked:
        failures.append(
            Failure(
                "adr-files",
                f"index/files mismatch: files={adr_files} linked={linked}",
            )
        )

    for name in adr_files:
        text = (adr_dir / name).read_text(encoding="utf-8")
        title = text.splitlines()[0] if text.splitlines() else ""
        if not title.startswith(f"# ADR-{name[:4]}:"):
            failures.append(Failure("adr-files", f"{name}: title must start with '# ADR-{name[:4]}:'"))
        for section in ADR_REQUIRED_SECTIONS:
            if section not in text:
                failures.append(Failure("adr-files", f"{name}: missing section '{section}'"))
    return failures


def check_threat_model(root: Path) -> list[Failure]:
    failures: list[Failure] = []
    path = root / THREAT_MODEL
    if not path.is_file():
        return [Failure("threat-model", f"missing {THREAT_MODEL}")]
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    for section in THREAT_MODEL_REQUIRED_SECTIONS:
        if section not in text:
            failures.append(Failure("threat-model", f"missing section '{section}'"))
    for scenario in THREAT_MODEL_REQUIRED_SCENARIOS:
        if scenario.lower() not in lowered:
            failures.append(Failure("threat-model", f"scenario not covered: '{scenario}'"))
    for term in THREAT_MODEL_REQUIRED_TERMS:
        if term.lower() not in lowered:
            failures.append(Failure("threat-model", f"missing required content: '{term}'"))
    for adr in ("0001", "0002", "0003", "0004", "0005", "0006"):
        if f"ADR-{adr}" not in text:
            failures.append(Failure("threat-model", f"does not reference ADR-{adr}"))
    return failures


def check_invariants(root: Path) -> list[Failure]:
    failures: list[Failure] = []
    for relative, pattern, description in INVARIANT_PATTERNS:
        path = root / relative
        if not path.is_file():
            failures.append(Failure("invariants", f"{relative}: missing ({description})"))
            continue
        if not re.search(pattern, _read(root, relative), re.IGNORECASE):
            failures.append(Failure("invariants", f"{relative}: pattern /{pattern}/ not found ({description})"))
    return failures


def check_links(root: Path) -> list[Failure]:
    failures: list[Failure] = []
    docs = [root / ARCHITECTURE, root / DOCS_INDEX, root / THREAT_MODEL, root / ADR_INDEX]
    adr_dir = root / ADR_DIR
    if adr_dir.is_dir():
        docs.extend(sorted(p for p in adr_dir.iterdir() if ADR_FILE_PATTERN.match(p.name)))
    for doc in docs:
        if not doc.is_file():
            continue
        for match in RELATIVE_LINK_PATTERN.finditer(doc.read_text(encoding="utf-8")):
            target = match.group(1)
            if "://" in target or target.startswith("mailto:"):
                continue
            resolved = (doc.parent / target).resolve()
            if not resolved.exists():
                failures.append(Failure("links", f"{doc.relative_to(root)}: broken link '{target}'"))
    return failures


ALL_CHECKS = [check_adr_files, check_threat_model, check_invariants, check_links]


def run_checks(root: Path) -> list[Failure]:
    failures: list[Failure] = []
    for check in ALL_CHECKS:
        failures.extend(check(root))
    return failures


def _expect_failure(root: Path, check, fragment: str) -> str | None:
    """Assert a check rejects a broken tree with a matching message."""
    failures = check(root)
    if not failures:
        return f"expected {check.__name__} to fail, but it passed"
    if not any(fragment in failure.detail for failure in failures):
        details = "; ".join(failure.detail for failure in failures)
        return f"expected failure containing '{fragment}', got: {details}"
    return None


def run_self_test() -> list[str]:
    """Exercise the checker against good and broken temporary trees."""
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="anvil-docs-selftest-") as tmp:
        tmp_root = Path(tmp)
        shutil.copytree(REPO_ROOT / "docs", tmp_root / "docs")

        # Success case: a faithful copy of the real docs must pass.
        good = run_checks(tmp_root)
        if good:
            errors.append("self-test success case failed: " + "; ".join(f.detail for f in good))

        # Rejection: removing a required ADR section must fail.
        broken = tmp_root / "broken-section"
        shutil.copytree(tmp_root / "docs", broken / "docs")
        adr = broken / ADR_DIR / "0001-execution-modes.md"
        adr.write_text(adr.read_text(encoding="utf-8").replace("## Context", "## Background"), encoding="utf-8")
        result = _expect_failure(broken, check_adr_files, "missing section '## Context'")
        if result:
            errors.append(result)

        # Rejection: dropping a threat scenario must fail.
        broken = tmp_root / "broken-scenario"
        shutil.copytree(tmp_root / "docs", broken / "docs")
        threat = broken / THREAT_MODEL
        threat.write_text(
            re.sub(r"[Tt]enant isolation", "project scoping", threat.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        result = _expect_failure(broken, check_threat_model, "tenant isolation")
        if result:
            errors.append(result)

        # Rejection: an ADR missing from the index must fail (regression).
        broken = tmp_root / "broken-index"
        shutil.copytree(tmp_root / "docs", broken / "docs")
        index = broken / ADR_INDEX
        index.write_text(
            index.read_text(encoding="utf-8").replace("0006-model-broker.md", "index-entry-removed.md"),
            encoding="utf-8",
        )
        result = _expect_failure(broken, check_adr_files, "index/files mismatch")
        if result:
            errors.append(result)

        # Rejection: weakening an invariant phrase must fail (regression).
        broken = tmp_root / "broken-invariant"
        shutil.copytree(tmp_root / "docs", broken / "docs")
        broker = broken / "docs/adr/0006-model-broker.md"
        broker.write_text(
            broker.read_text(encoding="utf-8").replace("Only the broker", "The broker usually"),
            encoding="utf-8",
        )
        result = _expect_failure(broken, check_invariants, "0006-model-broker.md")
        if result:
            errors.append(result)

        # Edge: a completely empty tree fails cleanly on every check.
        empty = tmp_root / "empty"
        empty.mkdir()
        empty_failures = run_checks(empty)
        if not empty_failures:
            errors.append("self-test edge case failed: empty tree produced no failures")
        if len(empty_failures) < len(ALL_CHECKS):
            errors.append("self-test edge case failed: empty tree did not fail every check")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the checker's own success/rejection/edge/regression tests",
    )
    args = parser.parse_args()

    if args.self_test:
        errors = run_self_test()
        if errors:
            for error in errors:
                print(f"SELF-TEST FAIL: {error}", file=sys.stderr)
            return 1
        print("self-test: all checker behavior tests passed")
        return 0

    failures = run_checks(REPO_ROOT)
    if failures:
        for failure in failures:
            print(f"FAIL [{failure.check}] {failure.detail}", file=sys.stderr)
        return 1
    print("docs checks: all ADR and threat-model contract checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
