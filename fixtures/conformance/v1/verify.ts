import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

type Entry = { id: string; file: string; sha256: string };
const root = new URL(".", import.meta.url);
const read = (file: string) => JSON.parse(readFileSync(new URL(file, root), "utf8"));
const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value as object).sort().map(
      key => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
};

const manifest = read("manifest.json") as { fixtures: Entry[] };
for (const entry of manifest.fixtures) {
  const digest = createHash("sha256").update(canonical(read(entry.file))).digest("hex");
  if (digest !== entry.sha256) throw new Error(`${entry.id}: hash mismatch`);
}
console.log(`verified ${manifest.fixtures.length} fixtures`);
