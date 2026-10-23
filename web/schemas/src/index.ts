/**
 * Validators for Anvil's frozen v1 event and recording JSON Schemas.
 *
 * This is the TypeScript implementation of the cross-language contract:
 * the fixtures in `fixtures/schema/v1` are validated by Go, Python and
 * TypeScript and all three must agree.
 *
 * Validation is pure and in-memory; there is nothing to cancel and no
 * resource to clean up. Compiled validators are immutable and safe to reuse.
 */
import { readFileSync } from "node:fs";
import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Ajv2020 from "ajv/dist/2020.js";
import type { ErrorObject, ValidateFunction } from "ajv";
import addFormats from "ajv-formats";

export const SCHEMA_NAMES = [
  "event",
  "run",
  "stream",
  "artifact",
  "seal",
  "capture-gap",
  "manifest",
] as const;

export type SchemaName = (typeof SCHEMA_NAMES)[number];

/** A document failed schema validation. Thrown explicitly; the validator
 * never silently accepts or coerces input. Malformed JSON is reported as a
 * ValidationError too. */
export class ValidationError extends Error {
  readonly schema: string;
  constructor(schema: string, details: string) {
    super(`anvil schema ${schema} v1: validation failed: ${details}`);
    this.name = "ValidationError";
    this.schema = schema;
  }
}

const BASE_URI = "https://anvil.dev/schemas/v1/";
const SCHEMA_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "schemas",
  "v1",
);

let validators: Map<SchemaName, ValidateFunction> | undefined;

function loadAll(): Map<SchemaName, ValidateFunction> {
  const ajv = new Ajv2020({ strict: true, allErrors: false });
  addFormats(ajv);
  for (const file of readdirSync(SCHEMA_DIR)) {
    if (!file.endsWith(".schema.json")) continue;
    const raw = readFileSync(path.join(SCHEMA_DIR, file), "utf8");
    ajv.addSchema(JSON.parse(raw) as object);
  }
  const map = new Map<SchemaName, ValidateFunction>();
  for (const name of SCHEMA_NAMES) {
    const validate = ajv.getSchema(`${BASE_URI}${name}.json`);
    if (!validate) {
      throw new Error(`anvil schema: failed to compile schema ${name}`);
    }
    map.set(name, validate);
  }
  return map;
}

function summarize(errors: ErrorObject[] | null | undefined): string {
  if (!errors || errors.length === 0) return "unknown validation error";
  return errors
    .slice(0, 4)
    .map((e) => `${e.message ?? "invalid"} at ${e.instancePath || "/"}`)
    .join("; ");
}

/** Validate `data` (JSON text) against the named v1 schema.
 *
 * Returns normally for valid documents and throws {@link ValidationError}
 * otherwise, including for malformed JSON or an unknown schema name. */
export function validate(schema: SchemaName, data: string | Uint8Array): void {
  if (!SCHEMA_NAMES.includes(schema)) {
    throw new ValidationError(String(schema), `unknown schema ${String(schema)}`);
  }
  validators ??= loadAll();
  const text = typeof data === "string" ? data : new TextDecoder().decode(data);
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    throw new ValidationError(
      schema,
      `invalid JSON: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  const validateFn = validators.get(schema);
  if (!validateFn) {
    throw new ValidationError(schema, `schema ${schema} is not compiled`);
  }
  if (!validateFn(doc)) {
    throw new ValidationError(schema, summarize(validateFn.errors));
  }
}
