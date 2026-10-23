/**
 * Cross-language contract tests for the frozen v1 schemas.
 *
 * Every fixture in `fixtures/schema/v1` is validated here; the Go and
 * Python suites iterate exactly the same files and must agree.
 */
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { SCHEMA_NAMES, ValidationError, validate } from "../src/index.js";

const FIXTURE_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "fixtures",
  "schema",
  "v1",
);

interface FixtureCase {
  schema: string;
  file: string;
  valid: boolean;
}

function fixtureCases(): FixtureCase[] {
  const cases: FixtureCase[] = [];
  for (const dir of readdirSync(FIXTURE_ROOT, { withFileTypes: true })) {
    if (!dir.isDirectory()) continue;
    const dirPath = path.join(FIXTURE_ROOT, dir.name);
    for (const file of readdirSync(dirPath)) {
      if (file.endsWith(".valid.json")) {
        cases.push({ schema: dir.name, file: path.join(dirPath, file), valid: true });
      } else if (file.endsWith(".invalid.json")) {
        cases.push({ schema: dir.name, file: path.join(dirPath, file), valid: false });
      } else if (file.endsWith(".json")) {
        throw new Error(`fixture ${file} must end in .valid.json or .invalid.json`);
      }
    }
  }
  if (cases.length === 0) {
    throw new Error("no schema fixtures found; the corpus must not be empty");
  }
  return cases;
}

const cases = fixtureCases();

describe("fixture corpus agreement", () => {
  for (const c of cases) {
    it(`${c.schema}/${path.basename(c.file)}`, () => {
      const data = readFileSync(c.file, "utf8");
      if (c.valid) {
        expect(() => validate(c.schema as never, data)).not.toThrow();
      } else {
        expect(() => validate(c.schema as never, data)).toThrow(ValidationError);
      }
    });
  }
});

describe("explicit failures", () => {
  it("rejects malformed JSON as a ValidationError", () => {
    expect(() => validate("event", '{"schema_version": "1",')).toThrow(/invalid JSON/);
  });

  it("rejects an unknown schema name", () => {
    expect(() => validate("bogus" as never, "{}")).toThrow(/unknown schema/);
  });

  it("names the schema and version in errors", () => {
    expect(() => validate("seal", JSON.stringify({ schema_version: "1" }))).toThrow(
      /anvil schema seal v1/,
    );
  });

  it("covers every frozen schema document", () => {
    const schemaDir = path.resolve(FIXTURE_ROOT, "..", "..", "..", "schemas", "v1");
    const onDisk = readdirSync(schemaDir)
      .filter((f: string) => f.endsWith(".schema.json") && f !== "defs.schema.json")
      .map((f: string) => f.replace(/\.schema\.json$/, ""));
    expect([...SCHEMA_NAMES].sort()).toEqual(onDisk.sort());
  });
});
