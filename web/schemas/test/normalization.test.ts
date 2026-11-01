import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  applyPolicy,
  canonicalHash,
  canonicalize,
  casefold,
  CanonicalizationError,
  POLICY_VERSION,
  type PolicyDescriptor,
} from "../src/normalization.js";

interface Case {
  id: string;
  description: string;
  policy: PolicyDescriptor;
  input: unknown;
  expected_normalized: unknown;
  expected_canonical: string;
  expected_evidence: { removed: string[]; unmatched: string[] };
  expected_hash: string;
}

const corpusPath = fileURLToPath(
  new URL("../../../fixtures/normalization/v1/cases.json", import.meta.url),
);
const corpus = JSON.parse(readFileSync(corpusPath, "utf8")) as {
  version: string;
  policy_version: string;
  cases: Case[];
};

const sha256 = (input: Uint8Array): string =>
  createHash("sha256").update(input).digest("hex");

describe("normalization corpus agreement", () => {
  it("publishes cases", () => {
    expect(corpus.cases.length).toBeGreaterThan(0);
    expect(corpus.policy_version).toBe(POLICY_VERSION);
  });

  for (const testCase of corpus.cases) {
    describe(testCase.id, () => {
      it("normalizes to the published value", () => {
        const { normalized } = applyPolicy(testCase.policy, testCase.input);
        expect(normalized).toEqual(testCase.expected_normalized);
      });

      it("reports the published evidence", () => {
        const { evidence } = applyPolicy(testCase.policy, testCase.input);
        expect(evidence.removed).toEqual(testCase.expected_evidence.removed);
        expect(evidence.unmatched).toEqual(testCase.expected_evidence.unmatched);
      });

      it("produces the published canonical form", () => {
        const { normalized } = applyPolicy(testCase.policy, testCase.input);
        expect(canonicalize(normalized)).toBe(testCase.expected_canonical);
      });

      it("produces the published hash", () => {
        expect(canonicalHash(testCase.policy, testCase.input, sha256)).toBe(
          testCase.expected_hash,
        );
      });

      it("does not mutate the input", () => {
        const before = JSON.stringify(testCase.input);
        applyPolicy(testCase.policy, testCase.input);
        expect(JSON.stringify(testCase.input)).toBe(before);
      });
    });
  }
});

describe("hidden field removal is impossible", () => {
  it("keeps every field the policy does not name", () => {
    const descriptor: PolicyDescriptor = {
      version: POLICY_VERSION,
      ignore_pointers: ["/trace_id"],
      normalize_whitespace: false,
      casefold_strings: false,
    };
    const { normalized } = applyPolicy(descriptor, {
      trace_id: "t",
      kept: 1,
      nested: { trace_id: "also kept" },
    });
    expect(normalized).toEqual({ kept: 1, nested: { trace_id: "also kept" } });
  });

  it("reports a pointer that matched nothing instead of ignoring it", () => {
    const descriptor: PolicyDescriptor = {
      version: POLICY_VERSION,
      ignore_pointers: ["/absent"],
      normalize_whitespace: false,
      casefold_strings: false,
    };
    const { evidence } = applyPolicy(descriptor, { kept: 1 });
    expect(evidence).toEqual({ removed: [], unmatched: ["/absent"] });
  });
});

describe("canonicalization rejects ambiguous values", () => {
  it("rejects non-finite numbers", () => {
    expect(() => canonicalize({ a: Number.NaN })).toThrow(CanonicalizationError);
    expect(() => canonicalize({ a: Number.POSITIVE_INFINITY })).toThrow(
      CanonicalizationError,
    );
  });

  it("canonicalizes large doubles rather than refusing them", () => {
    // Python rejects `int` values beyond 2**53 because they round-trip
    // lossily. JavaScript has no distinct integer type, so the equivalent
    // value is already a double and JCS defines its form. This asymmetry is
    // documented in docs/normalization.md; it is unreachable from JSON input,
    // which cannot express a Python int distinctly.
    expect(canonicalize({ a: 1e21 })).toBe('{"a":1e+21}');
  });

  it("rejects cycles rather than recursing until the stack fails", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(() => canonicalize(cyclic)).toThrow(CanonicalizationError);
  });

  it("orders keys by UTF-16 code unit, not by locale", () => {
    expect(canonicalize({ "\u00e4": 1, b: 2, A: 3 })).toBe('{"A":3,"b":2,"ä":1}');
  });
});

describe("case folding matches Python's full folding", () => {
  it("folds sharp s to ss, which toLowerCase does not", () => {
    expect("\u00df".toLowerCase()).toBe("\u00df");
    expect(casefold("\u00df")).toBe("ss");
  });

  it("folds final sigma to lowercase sigma", () => {
    expect(casefold("\u03c2")).toBe("\u03c3");
  });

  it("folds ligatures", () => {
    expect(casefold("\ufb01")).toBe("fi");
  });

  it("falls back to lowercase where folding and lowercasing agree", () => {
    expect(casefold("HELLO")).toBe("hello");
  });
});
