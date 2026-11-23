import type { CaptureEvent } from "./client";

export type TimelineBranch = {
  streamId: string;
  events: number;
  parentStreams: string[];
  missingParents: number;
};

export function timelineBranches(events: CaptureEvent[]): TimelineBranch[] {
  const owners = new Map(events.map((event) => [event.event_id, event.stream_id]));
  const branches = new Map<string, { events: number; parents: Set<string>; missing: number }>();
  for (const event of events) {
    const branch = branches.get(event.stream_id) ?? { events: 0, parents: new Set<string>(), missing: 0 };
    branch.events++;
    for (const parentId of event.parent_event_ids ?? []) {
      const owner = owners.get(parentId);
      if (!owner) branch.missing++;
      else if (owner !== event.stream_id) branch.parents.add(owner);
    }
    branches.set(event.stream_id, branch);
  }
  return [...branches].map(([streamId, branch]) => ({
    streamId, events: branch.events, parentStreams: [...branch.parents].sort(),
    missingParents: branch.missing,
  })).sort((left, right) => left.streamId.localeCompare(right.streamId));
}
