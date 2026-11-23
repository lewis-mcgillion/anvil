export type DivergenceEvidence = {
  baseline?: unknown;
  candidate?: unknown;
  alignment?: "exact" | "ambiguous" | "missing";
  judge?: { status: string; rationale?: string } | null;
};

export function divergenceDisplay(evidence: DivergenceEvidence) {
  const baseline = evidence.baseline === undefined ? "Evidence missing" : JSON.stringify(evidence.baseline);
  const candidate = evidence.candidate === undefined ? "Evidence missing" : JSON.stringify(evidence.candidate);
  return {
    baseline,
    candidate,
    alignment: evidence.alignment ?? "missing",
    judge: evidence.judge?.status ?? "Not judged",
    judgeRationale: evidence.judge?.rationale ?? "",
  };
}
