export type CaptureQuality = {
  capture_status?: string | null;
  execution_outcome?: string | null;
  replay_eligibility?: string | null;
};

export function captureQualityLabel(run: CaptureQuality): string {
  if (run.capture_status !== "sealed_complete") return "Capture incomplete or unknown";
  if (run.execution_outcome !== "succeeded") return "Execution failed or unknown";
  if (run.replay_eligibility !== "eligible") return "Replay eligibility unverified";
  return "Complete, succeeded and replay-eligible";
}
