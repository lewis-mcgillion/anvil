export type DivergenceEvidence = {
  baseline?: unknown;
  candidate?: unknown;
  alignment?: "exact" | "ambiguous" | "missing";
  judge?: { status: string; rationale?: string } | null;
};

export function divergenceDisplay(evidence: DivergenceEvidence) {
  const serialize = (value: unknown): string => {
    if (value === undefined) return "Evidence missing";
    try {
      return JSON.stringify(value) ?? "Evidence unavailable";
    } catch {
      return "Evidence unavailable";
    }
  };
  const baseline = serialize(evidence.baseline);
  const candidate = serialize(evidence.candidate);
  return {
    baseline,
    candidate,
    alignment: evidence.alignment ?? "missing",
    judge: evidence.judge?.status ?? "Not judged",
    judgeRationale: evidence.judge?.rationale ?? "",
  };
}
