export type ReplayConfig = {
  agentVersion: string;
  mode: "recorded" | "live";
  liveAuthorized: boolean;
  brokerReady: boolean;
  runnerReady: boolean;
  maxModelCalls: number;
};

export function validateReplayConfig(config: ReplayConfig): string {
  if (!config.agentVersion) throw new Error("Select a pinned agent version");
  if (!Number.isSafeInteger(config.maxModelCalls) || config.maxModelCalls < 1
      || config.maxModelCalls > 20) throw new Error("Model call budget exceeds policy");
  if (config.mode !== "recorded" && config.mode !== "live") throw new Error("Invalid model mode");
  if (config.runnerReady !== true) throw new Error("Isolated runner is unavailable");
  if (config.mode === "live" && (config.liveAuthorized !== true || config.brokerReady !== true)) {
    throw new Error("Live inference requires explicit authorization and a verified broker");
  }
  return config.mode === "live" ? "Live models; production tools remain mocked"
    : "Recorded fixtures only; models and production tools remain mocked";
}
