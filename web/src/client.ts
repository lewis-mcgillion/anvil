import createClient from "openapi-fetch";
import type { components, paths } from "./api-types";

export type Project = components["schemas"]["Project"];
export type Comparison = components["schemas"]["Comparison"];
export type Divergence = components["schemas"]["Divergence"];

export async function readComparison(client: ReturnType<typeof dashboardClient>, signal: AbortSignal,
                                     projectId: string, comparisonId: string) {
  const { data } = await client.GET("/comparisons/{comparison_id}", {
    params: { path: { comparison_id: comparisonId }, query: { project_id: projectId } }, signal,
  });
  if (!data || data.id !== comparisonId || typeof data.deterministic !== "string" ||
      typeof data.judge !== "string" || typeof data.infrastructure !== "string") {
    throw new Error("Invalid comparison summary; no result was accepted.");
  }
  return data;
}

export async function readDivergences(client: ReturnType<typeof dashboardClient>, signal: AbortSignal,
                                      projectId: string, comparisonId: string, cursor?: string) {
  const { data } = await client.GET("/comparisons/{comparison_id}/divergences", {
    params: { path: { comparison_id: comparisonId }, query: {
      project_id: projectId, limit: 100, ...(cursor ? { cursor } : {}),
    } }, signal,
  });
  if (!data || !Array.isArray(data.items) ||
      !(data.next_cursor === null || (typeof data.next_cursor === "string" && data.next_cursor.length > 0)) ||
      data.items.some((item) => !item.id || typeof item.summary !== "string" ||
        typeof item.category !== "string" || !Array.isArray(item.evidence_event_ids))) {
    throw new Error("Invalid divergence page; no findings were accepted.");
  }
  return data;
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
