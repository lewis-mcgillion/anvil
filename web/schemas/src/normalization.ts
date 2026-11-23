/**
 * Independent TypeScript implementation of Anvil's versioned comparison
 * normalization (ANVIL-043).
 *
 * This deliberately does not share code with the Python implementation in
 * `server/anvil_server/canonical.py`. Two implementations that agree on the
 * published fixture corpus in `fixtures/normalization/v1` are evidence that the
 * canonical form is a contract rather than an artefact of one runtime.
 *
 * The module performs no I/O and holds no resources, so there is nothing to
 * clean up and no cancellation semantics.
 */

import { CASEFOLD_EXCEPTIONS } from "./casefold-exceptions.js";

export class CanonicalizationError extends Error {}

export interface PolicyDescriptor {
  readonly version: string;
  readonly ignore_pointers: readonly string[];
  readonly normalize_whitespace: boolean;
  readonly casefold_strings: boolean;
}

export interface NormalizationEvidence {
  readonly removed: string[];
  readonly unmatched: string[];
}

export const POLICY_VERSION = "1";

/** Sort object keys by UTF-16 code unit, which is what JCS specifies. */
function sortedKeys(value: Record<string, unknown>): string[] {
  // Array.prototype.sort's default comparator orders by UTF-16 code unit.
  return Object.keys(value).sort();
}

/**
 * Serialize to RFC 8785 canonical JSON.
 *
 * JSON.stringify already emits the ECMAScript number formatting and the string
 * escaping that JCS requires, so the only additional work is ordering keys and
 * refusing values that have no unambiguous canonical form.
 */
export function canonicalize(value: unknown): string {
  return write(value, new Set<object>());
}

function write(value: unknown, seen: Set<object>): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new CanonicalizationError(`non-finite number: ${value}`);
    }
    // No safe-integer check here, deliberately. Python rejects `int` values
    // beyond 2**53 because they would round-trip lossily through a double;
    // JavaScript has no distinct integer type, so every JSON number is already
    // the double that JCS defines. Rejecting large doubles here would wrongly
    // refuse values such as 1e21 that Python canonicalizes without complaint.
    return JSON.stringify(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (seen.has(value)) throw new CanonicalizationError("cycle detected");
    seen.add(value);
    const body = value.map((item) => write(item, seen)).join(",");
    seen.delete(value);
    return `[${body}]`;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (seen.has(record)) throw new CanonicalizationError("cycle detected");
    seen.add(record);
    const body = sortedKeys(record)
      .map((key) => `${JSON.stringify(key)}:${write(record[key], seen)}`)
      .join(",");
    seen.delete(record);
    return `{${body}}`;
  }
  throw new CanonicalizationError(`unsupported value: ${typeof value}`);
}

/** Unicode full case folding, matching Python's str.casefold. */
export function casefold(text: string): string {
  let out = "";
  for (const character of text) {
    const replacement = CASEFOLD_EXCEPTIONS.get(character.codePointAt(0)!);
    out += replacement ?? character.toLowerCase();
  }
  return out;
}

function unescapeToken(token: string): string {
  return token.replace(/~1/g, "/").replace(/~0/g, "~");
}

/** Remove the pointer target, reporting whether anything was actually removed. */
function removePointer(document: unknown, pointer: string): boolean {
  const tokens = pointer.split("/").slice(1).map(unescapeToken);
  const last = tokens.pop();
  if (last === undefined) throw new Error("Refusing to ignore the document root");
  let cursor: unknown = document;
  for (const token of tokens) {
    if (Array.isArray(cursor)) {
      const index = Number(token);
      if (!/^\d+$/.test(token) || index >= cursor.length) return false;
      cursor = cursor[index];
    } else if (cursor !== null && typeof cursor === "object") {
      const record = cursor as Record<string, unknown>;
      if (!Object.hasOwn(record, token)) return false;
      cursor = record[token];
    } else {
      return false;
    }
  }
  if (Array.isArray(cursor)) {
    const index = Number(last);
    if (/^\d+$/.test(last) && index < cursor.length) {
      cursor.splice(index, 1);
      return true;
    }
    return false;
  }
  if (cursor !== null && typeof cursor === "object") {
    const record = cursor as Record<string, unknown>;
    if (Object.hasOwn(record, last)) {
      delete record[last];
      return true;
    }
  }
  return false;
}

function mapStrings(
  value: unknown,
  descriptor: PolicyDescriptor,
): unknown {
  if (typeof value === "string") {
    const text = descriptor.normalize_whitespace
      ? value.split(/\s+/).filter((part) => part.length > 0).join(" ")
      : value;
    return descriptor.casefold_strings ? casefold(text) : text;
  }
  if (Array.isArray(value)) return value.map((item) => mapStrings(item, descriptor));
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = mapStrings(item, descriptor);
    }
    return out;
  }
  return value;
}

/**
 * Apply a policy, returning the normalized value and what the policy did.
 *
 * Only the pointers named by the policy are removed. A pointer that matches
 * nothing is reported as unmatched rather than passing silently, so a report
 * can never imply a field was absent when the ignore was simply misdirected.
 */
export function applyPolicy(
  descriptor: PolicyDescriptor,
  value: unknown,
): { normalized: unknown; evidence: NormalizationEvidence } {
  const removed: string[] = [];
  const unmatched: string[] = [];
  let result: unknown = structuredClone(value);
  for (const pointer of [...descriptor.ignore_pointers].sort()) {
    if (removePointer(result, pointer)) removed.push(pointer);
    else unmatched.push(pointer);
  }
  if (descriptor.normalize_whitespace || descriptor.casefold_strings) {
    result = mapStrings(result, descriptor);
  }
  return { normalized: result, evidence: { removed, unmatched } };
}

/** The policy identity is hashed with the data, so policies cannot collide. */
export function canonicalHash(
  descriptor: PolicyDescriptor,
  value: unknown,
  sha256: (input: Uint8Array) => string,
): string {
  const { normalized } = applyPolicy(descriptor, value);
  const bytes = new TextEncoder().encode(
    canonicalize({ policy: descriptor, value: normalized }),
  );
  return sha256(bytes);
}
