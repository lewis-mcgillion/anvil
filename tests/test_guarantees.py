"""Every guarantee Anvil documents must cite a test that exists.

``docs/guarantees.md`` is the document a reader uses to decide what they can
depend on, which makes it the document most likely to drift: a claim is
cheap to write and expensive to verify, and a test that gets renamed takes
its proof with it silently.

These checks are the enforcement. They parse the document and require that
every claim names a proof, that every proof resolves to a test that exists
in the file it names, and that the sections covering the features the
project promises to document are present. They do not check that the test
proves the claim -- nothing can -- but they do make an unproven claim
impossible to leave in the file.
"""

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).parents[1]
GUARANTEES = REPO_ROOT / "docs" / "guarantees.md"

# ANVIL-079 names the guides that must exist: capture, playback,
# re-execution, determinism, redaction, incomplete recordings and judges.
REQUIRED_SECTIONS = [
    "## Capture",
    "## Redaction and capture policy",
    "## Determinism",
    "## Playback",
    "## Incomplete recordings",
    "## Re-execution",
    "## Fault injection",
    "## Comparison and gating",
    "## Suites",
    "## Audit, deletion and privacy",
    "## Dashboard",
    "## Not guaranteed",
]

CLAIM = re.compile(r"^- (?!\*\*)(?P<claim>.+?)— proof: `(?P<proof>[^`]+)`", re.S)
PROOF = re.compile(r"^(?P<path>[\w./-]+)::(?P<name>.+)$")


def _text() -> str:
    return GUARANTEES.read_text(encoding="utf-8")


def _claim_blocks(text: str) -> list[str]:
    """Return each top-level bullet in the claim sections, joined over wraps.

    Claims wrap across lines, so a line-by-line scan would see a proof-less
    bullet wherever the citation fell onto the next line.
    """
    body, _, _ = text.partition("## Not guaranteed")
    blocks: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            # A fenced block holds the format example, not a claim.
            fenced = not fenced
        elif fenced:
            continue
        elif line.startswith("- "):
            blocks.append(line)
        elif line.startswith("  ") and blocks:
            blocks[-1] += " " + line.strip()
        elif not line.strip():
            continue
        else:
            blocks.append("")  # A heading or prose ends the current bullet.
    return [block for block in blocks if block]


def proofs(text: str) -> list[tuple[str, str]]:
    found = []
    for block in _claim_blocks(text):
        match = CLAIM.match(block)
        if match is None:
            continue
        reference = PROOF.match(match.group("proof").strip())
        assert reference is not None, match.group("proof")
        found.append((reference.group("path"), reference.group("name")))
    return found


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.text = _text()

    def test_the_document_exists_and_names_its_own_enforcement(self):
        self.assertIn("tests/test_guarantees.py", self.text)

    def test_every_required_feature_area_is_covered(self):
        for section in REQUIRED_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(section + "\n", self.text)

    def test_the_limitations_section_is_not_empty(self):
        _, _, limits = self.text.partition("## Not guaranteed")
        self.assertGreaterEqual(len(re.findall(r"^- ", limits, re.M)), 3)

    def test_every_claim_bullet_cites_a_proof(self):
        for block in _claim_blocks(self.text):
            with self.subTest(claim=block[:60]):
                self.assertRegex(block, r"— proof: `[^`]+`")

    def test_there_are_claims_to_check(self):
        self.assertGreater(len(proofs(self.text)), 30)


class ProofTests(unittest.TestCase):
    """Each citation must resolve to a test that exists today."""

    def setUp(self):
        self.proofs = proofs(_text())

    def test_every_cited_file_exists(self):
        for path, _ in self.proofs:
            with self.subTest(path=path):
                self.assertTrue((REPO_ROOT / path).is_file(), path)

    def test_every_cited_test_exists_in_the_file_it_names(self):
        for path, name in self.proofs:
            with self.subTest(proof=f"{path}::{name}"):
                source = (REPO_ROOT / path).read_text(encoding="utf-8")
                leaf = name.rsplit("::", 1)[-1]
                self.assertIn(leaf, source, f"{leaf} is not in {path}")

    def test_a_cited_python_test_is_named_as_a_test(self):
        for path, name in self.proofs:
            if not path.endswith(".py"):
                continue
            with self.subTest(proof=name):
                leaf = name.rsplit("::", 1)[-1]
                self.assertTrue(leaf.startswith("test_"), leaf)

    def test_a_cited_class_exists_when_one_is_named(self):
        for path, name in self.proofs:
            if "::" not in name or not path.endswith(".py"):
                continue
            klass = name.split("::")[0]
            with self.subTest(klass=klass):
                source = (REPO_ROOT / path).read_text(encoding="utf-8")
                self.assertIn(f"class {klass}", source)

    def test_proofs_are_spread_across_the_codebase(self):
        """A document proved entirely by one file is proving one thing."""
        self.assertGreaterEqual(len({path for path, _ in self.proofs}), 8)


class ParserTests(unittest.TestCase):
    """The checker itself: a parser that finds nothing passes everything."""

    def test_a_claim_without_a_proof_is_found(self):
        document = "## Capture\n\n- Anvil is fast.\n"
        self.assertEqual(proofs(document), [])
        self.assertEqual(len(_claim_blocks(document)), 1)

    def test_a_wrapped_claim_is_read_as_one_claim(self):
        document = ("## Capture\n\n- A claim that runs on\n"
                    "  for a while. — proof: `a/b.py::test_c`\n")
        self.assertEqual(proofs(document), [("a/b.py", "test_c")])

    def test_limitations_are_not_treated_as_claims(self):
        document = ("## Capture\n\n- A. — proof: `a/b.py::test_c`\n"
                    "\n## Not guaranteed\n\n- **Isolation.** None exists.\n")
        self.assertEqual(proofs(document), [("a/b.py", "test_c")])

    def test_a_class_qualified_proof_keeps_its_class(self):
        document = "## Capture\n\n- A. — proof: `a/b.py::Klass::test_c`\n"
        self.assertEqual(proofs(document), [("a/b.py", "Klass::test_c")])

    def test_a_non_python_proof_is_accepted(self):
        document = "## Capture\n\n- A. — proof: `web/test/browser/x.spec.ts::a name`\n"
        self.assertEqual(proofs(document),
                         [("web/test/browser/x.spec.ts", "a name")])


if __name__ == "__main__":
    unittest.main()
