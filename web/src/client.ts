import createClient from "openapi-fetch";
import type { components, paths } from "./api-types";

export type Project = components["schemas"]["Project"];
export type Run = components["schemas"]["Run"];

export async function watchRunEvents(token: string, projectId: string, runId: string,
                                     signal: AbortSignal, onEvent: () => void): Promise<void> {
  const response = await fetch(`/v1/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/watch`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
    credentials: "omit", redirect: "error", signal,
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new ApiError(response.status, response.headers.get("X-Request-ID"), response.headers.get("Retry-After"));
  }
  if (!response.body) throw new Error("The event stream has no body.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let pendingCarriageReturn = false;
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      let chunk = decoder.decode(value, { stream: true });
      if (pendingCarriageReturn) chunk = "\r" + chunk;
      pendingCarriageReturn = chunk.endsWith("\r");
      if (pendingCarriageReturn) chunk = chunk.slice(0, -1);
      buffer += chunk.replace(/\r\n?/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (frame.length > 1024 * 1024) throw new Error("The event stream exceeded its frame limit.");
        const data = frame.split("\n").filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart()).join("\n");
        if (data && !signal.aborted) {
          try {
            const payload: unknown = JSON.parse(data);
            if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
              throw new Error("Invalid event payload");
            }
          }
          catch { throw new Error("The event stream contained invalid JSON evidence."); }
          onEvent();
        }
        boundary = buffer.indexOf("\n\n");
      }
      if (buffer.length > 1024 * 1024) throw new Error("The event stream exceeded its frame limit.");
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly requestId: string | null,
    public readonly retryAfter: string | null,
  ) {
    const messages: Record<number, string> = {
      401: "Your session is invalid or expired. Sign in again.",
      403: "Your key does not have permission to list projects (runs:read required).",
      404: "The project-list API is unavailable on this server.",
      429: "Too many requests. Wait before retrying.",
      503: "The API is not ready. Check persistence and authentication configuration.",
    };
    super(messages[status] ?? `The API request failed (HTTP ${status}).`);
  }
}

export function dashboardClient(token: string) {
  // A relative base URL keeps credentials on the dashboard origin.
  const client = createClient<paths>({
    baseUrl: "/v1",
    headers: { Authorization: `Bearer ${token}` },
    credentials: "omit",
    redirect: "error",
  });
  client.use({
    async onResponse({ response }) {
      if (!response.ok) {
        await response.body?.cancel();
        throw new ApiError(
          response.status,
          response.headers.get("X-Request-ID"),
          response.headers.get("Retry-After"),
        );
      }
    },
  });
  return client;
}

export async function listProjects(
  client: ReturnType<typeof dashboardClient>,
  signal: AbortSignal,
  cursor?: string,
) {
  const { data } = await client.GET("/projects", {
    params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
    signal,
  });
  if (
    !data ||
    !Array.isArray(data.items) ||
    !(data.next_cursor === null || (typeof data.next_cursor === "string" && data.next_cursor.length > 0)) ||
    data.items.some((project) =>
      !project ||
      typeof project.id !== "string" ||
      !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(project.id) ||
      typeof project.name !== "string" ||
      project.name.length === 0 ||
      project.name.length > 128 ||
      !Number.isInteger(project.retention_days) ||
      project.retention_days < 1 ||
      project.retention_days > 365 ||
      typeof project.created_at !== "string" ||
      !Number.isFinite(Date.parse(project.created_at)),
    ) ||
    new Set(data.items.map((project) => project.id)).size !== data.items.length
  ) {
    throw new Error("The API returned an invalid project page. No project data was accepted.");
  }
  return data;
}

export async function listProjectRuns(client: ReturnType<typeof dashboardClient>,
                                      signal: AbortSignal, projectId: string, cursor?: string) {
  const { data } = await client.GET("/runs", {
    params: { query: { project_id: projectId, limit: 100, ...(cursor ? { cursor } : {}) } },
    signal,
  });
  if (!data || !Array.isArray(data.items) ||
      !(data.next_cursor === null || (typeof data.next_cursor === "string" && data.next_cursor.length > 0)) ||
      data.items.some((run) => !run || run.project_id !== projectId || typeof run.id !== "string" ||
        typeof run.capture_state !== "string" || typeof run.execution_state !== "string" ||
        typeof run.agent_name !== "string") ||
      new Set(data.items.map((run) => run.id)).size !== data.items.length) {
    throw new Error("The API returned an invalid project run page.");
  }
  return data;
}
