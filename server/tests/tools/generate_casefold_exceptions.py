import pathlib
entries = []
for cp in range(0x110000):
    ch = chr(cp)
    folded = ch.casefold()
    if folded != ch.lower():
        entries.append((cp, folded))
lines = [
    "// GENERATED FILE - do not edit by hand.",
    "//",
    "// Code points whose Unicode full case folding differs from the lowercase",
    "// mapping that String.prototype.toLowerCase applies. JavaScript has no",
    "// casefold operation, so the normalization policy's casefold option would",
    "// otherwise disagree with Python across languages (U+00DF folds to \"ss\",",
    "// not to itself).",
    "//",
    "// Regenerate with: python server/tests/tools/generate_casefold_exceptions.py",
    "// A Python test fails if this table drifts from the running Unicode data.",
    "",
    "export const CASEFOLD_EXCEPTIONS: ReadonlyMap<number, string> = new Map([",
]
for cp, folded in entries:
    escaped = "".join(f"\\u{ord(c):04x}" if ord(c) > 0x7E or ord(c) < 0x20 else c for c in folded)
    lines.append(f'  [0x{cp:04x}, "{escaped}"],')
lines.append("]);")
lines.append("")
out = pathlib.Path("web/schemas/src/casefold-exceptions.ts")
out.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {len(entries)} exceptions to {out}")
