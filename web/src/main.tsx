import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, dashboardClient, listProjects, listRunEvents, type CaptureEvent, type Project } from "./client";
import { timelineWindow } from "./timeline-window";
import { timelineBranches } from "./timeline-branches";
import "./style.css";

class ErrorBoundary extends React.Component<React.PropsWithChildren, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed
      ? <main id="main" tabIndex={-1} role="alert"><h1>Dashboard error</h1><p>The dashboard could not render. Reload to start a new session.</p><button onClick={() => window.location.reload()}>Reload dashboard</button></main>
      : this.props.children;
  }
}

// Which route has already been announced to assistive technology. Module
// scope rather than a ref, because a route whose content swaps (placeholder to
// loaded) remounts `Page`, and a per-instance ref would forget.
let announced: string | null = null;

function Page({ title, children }: React.PropsWithChildren<{ title: string }>) {
  const heading = useRef<HTMLHeadingElement>(null);
  const location = useLocation();
  useEffect(() => {
    document.title = `${title} | Anvil`;
  }, [title]);
  useEffect(() => {
    // Moving focus to the heading is right on navigation, and wrong when a
    // slow fetch resolves afterwards: by then the reader may have tabbed to
    // the project switcher, and yanking focus back loses their place.
    //
    // So claim focus when the route actually changed, or when focus has been
    // lost to the body because the element holding it was unmounted. A reader
    // who has moved focus to something still on the page keeps it.
    const active = document.activeElement;
    const orphaned = !active || active === document.body;
    if (announced === location.pathname && !orphaned) return;
    announced = location.pathname;
    heading.current?.focus();
  }, [title, location.pathname]);
  return <><h1 ref={heading} tabIndex={-1}>{title}</h1>{children}</>;
}

function EventInspector({ client, projectId, runId }: { client: ReturnType<typeof dashboardClient>; projectId: string; runId: string }) {
  const [events, setEvents] = useState<CaptureEvent[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [scrollTop, setScrollTop] = useState(0);
  const [selected, setSelected] = useState<string>();
  const [streamFilter, setStreamFilter] = useState("");
  const seenCursors = useRef(new Set<string>());
  const viewport = useRef<HTMLDivElement>(null);
  const rowHeight = 56;
  const streams = [...new Set(events.map((event) => event.stream_id))];
  const branches = timelineBranches(events);
  const filtered = streamFilter ? events.filter((event) => event.stream_id === streamFilter) : events;
  const windowed = timelineWindow(filtered.length, scrollTop, rowHeight, 420);
  const seekTo = (eventId: string) => {
    const position = filtered.findIndex((event) => event.event_id === eventId);
    if (position < 0) return;
    viewport.current?.scrollTo({ top: position * rowHeight });
    setScrollTop(position * rowHeight);
    setSelected(eventId);
  };
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    listRunEvents(client, controller.signal, projectId, runId, cursor).then((page) => {
      if (controller.signal.aborted) return;
      if (page.next_cursor && (page.next_cursor === cursor || seenCursors.current.has(page.next_cursor))) {
        throw new Error("The API repeated an event cursor; pagination stopped.");
      }
      if (cursor && page.items.some((event) => events.some((item) => item.event_id === event.event_id))) {
        throw new Error("The API repeated an event across pages; pagination stopped.");
      }
      if (cursor) seenCursors.current.add(cursor);
      setEvents((previous) => cursor ? [...previous, ...page.items.filter((event) =>
        !previous.some((item) => item.event_id === event.event_id))] : page.items);
      setNextCursor(page.next_cursor);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Events unavailable");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [client, projectId, runId, cursor]);
  const active = events.find((event) => event.event_id === selected);
  return <section aria-label="Read-only event inspector">
    <p>Events are read-only. Playback and seeking never execute agents, models or tools.</p>
    <h3>Observed causal branches</h3>
    <p role="note">Only loaded events are analyzed. Missing parent references may be on unloaded pages;
      this view does not establish capture completeness or replay eligibility.</p>
    <ul>{branches.map((branch) => <li key={branch.streamId}>
      <button onClick={() => { setStreamFilter(branch.streamId); setScrollTop(0); viewport.current?.scrollTo({ top: 0 }); }}>
        Stream {branch.streamId}
      </button>: {branch.events} loaded events; upstream streams: {branch.parentStreams.join(", ") || "none observed"};
      {branch.missingParents ? ` ${branch.missingParents} parent references unavailable in loaded pages` : " all referenced parents loaded"}
    </li>)}</ul>
    <label>Stream <select value={streamFilter} onChange={(event) => {
      setStreamFilter(event.target.value); setScrollTop(0); viewport.current?.scrollTo({ top: 0 });
    }}><option value="">All streams</option>{streams.map((stream) =>
      <option key={stream} value={stream}>{stream}</option>)}</select></label>
    <label>Seek to event <input type="number" min={1} max={filtered.length} placeholder="Event number"
      onChange={(event) => {
        const position = Number(event.target.value) - 1;
        if (Number.isInteger(position) && position >= 0 && position < filtered.length) seekTo(filtered[position].event_id);
      }} /></label>
    {error && <p role="alert">{error}</p>}
    {loading && <p role="status">Loading events...</p>}
    <div ref={viewport} className="event-viewport" onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
      <div style={{ height: windowed.paddingTop }} />
      {filtered.slice(windowed.start, windowed.end).map((event) => <button key={event.event_id}
        className="event-row" style={{ height: rowHeight }} onClick={() => setSelected(event.event_id)}>
        <span>{event.sequence}: {event.type}</span> <small>Stream {event.stream_id}</small>
      </button>)}
      <div style={{ height: windowed.paddingBottom }} />
    </div>
    <p>{events.length} events loaded{nextCursor ? "; more available" : ""}.</p>
    {nextCursor && <button disabled={loading} onClick={() => setCursor(nextCursor)}>Load more events</button>}
    {active && <section><h3>Event payload</h3><p>Event ID: <code>{active.event_id}</code></p>
      <p>Parent events: {active.parent_event_ids?.length ? active.parent_event_ids.map((parent) =>
        <button key={parent} disabled={!filtered.some((event) => event.event_id === parent)}
          onClick={() => seekTo(parent)}>{parent}</button>) : "None"}</p>
      <pre>{JSON.stringify(active.payload, null, 2)}</pre></section>}
  </section>;
}

function ProjectOverview({ projects, client }: { projects: Project[]; client: ReturnType<typeof dashboardClient> }) {
  const { projectId } = useParams();
  const project = projects.find((item) => item.id === projectId);
  const [runId, setRunId] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  if (!project) {
    return <Page title="Project unavailable"><p>This project is not in the loaded authorized project list. Load additional projects or choose a visible project.</p><Link to="/">Choose a project</Link></Page>;
  }
  return <Page title={project.name}>
    <p>Project ID: <code>{project.id}</code></p>
    <p>Retention: {project.retention_days} days</p>
    <h2>Event inspector</h2>
    <form onSubmit={(event) => { event.preventDefault(); setSelectedRun(runId); }}>
      <label>Run ID <input required pattern="[0-9a-fA-F-]{36}" value={runId} onChange={(event) => setRunId(event.target.value)} /></label>
      <button type="submit">Inspect run</button>
    </form>
    {selectedRun && <EventInspector key={selectedRun} client={client} projectId={project.id} runId={selectedRun} />}
    <p>No capture completeness, execution outcome or replay eligibility is inferred from missing evidence. This page never schedules execution.</p>
  </Page>;
}

function Workspace({ token, onSignOut }: { token: string; onSignOut: (message?: string) => void }) {
  const client = useMemo(() => dashboardClient(token), [token]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error>();
  const [attempt, setAttempt] = useState(0);
  const seenCursors = useRef(new Set<string>());
  const navigate = useNavigate();
  const location = useLocation();
  const selected = /^\/projects\/([^/]+)$/.exec(location.pathname)?.[1] ?? "";

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    listProjects(client, controller.signal, cursor).then((page) => {
      if (controller.signal.aborted) return;
      if (page.next_cursor && (page.next_cursor === cursor || seenCursors.current.has(page.next_cursor))) {
        throw new Error("The API repeated a pagination cursor. Project loading stopped.");
      }
      if (cursor) seenCursors.current.add(cursor);
      setProjects((previous) => {
        const merged = new Map((cursor ? previous : []).map((project) => [project.id, project]));
        page.items.forEach((project) => merged.set(project.id, project));
        return [...merged.values()];
      });
      setNextCursor(page.next_cursor);
    }).catch((failure: unknown) => {
      if (controller.signal.aborted) return;
      if (failure instanceof ApiError && failure.status === 401) {
        onSignOut(failure.message);
        return;
      }
      setError(failure instanceof Error ? failure : new Error("The API request failed."));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [client, cursor, attempt, onSignOut]);

  return <>
    <header>
      <nav aria-label="Main"><Link to="/">Anvil projects</Link></nav>
      <button onClick={() => onSignOut()}>Sign out</button>
    </header>
    <nav className="project-switcher" aria-label="Project selection">
      <label htmlFor="project">Project</label>
      <select id="project" value={projects.some((project) => project.id === selected) ? selected : ""} onChange={(event) => navigate(event.target.value ? `/projects/${event.target.value}` : "/")}>
        <option value="">Choose a project</option>
        {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
      </select>
      {nextCursor && <button disabled={loading} onClick={() => setCursor(nextCursor)}>Load more projects</button>}
    </nav>
    <main id="main" tabIndex={-1}>
      {loading && <p role="status">Loading projects...</p>}
      {error && <section role="alert">
        <h2>Projects could not be loaded</h2><p>{error.message}</p>
        {error instanceof ApiError && error.requestId && <p>Request ID: <code>{error.requestId}</code></p>}
        {error instanceof ApiError && error.retryAfter && <p>Retry-After: {error.retryAfter}</p>}
        <button onClick={() => setAttempt((value) => value + 1)}>Retry project request</button>
      </section>}
      {!loading && !error && <Routes>
        <Route path="/" element={<Page title="Projects">
          {projects.length === 0 ? <p>No projects are visible to this key. Ask an administrator for a project grant.</p> : <ul>{projects.map((project) => <li key={project.id}><Link to={`/projects/${project.id}`}>{project.name}</Link></li>)}</ul>}
        </Page>} />
        <Route path="/projects/:projectId" element={<ProjectOverview projects={projects} client={client} />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Page title="Page not found"><Link to="/">Return to projects</Link></Page>} />
      </Routes>}
    </main>
  </>;
}

function App() {
  const [token, setToken] = useState("");
  const [notice, setNotice] = useState("");
  const navigate = useNavigate();
  const signOut = React.useCallback((message = "") => {
    setToken("");
    setNotice(message);
    navigate("/login", { replace: true });
  }, [navigate]);
  if (token) return <ErrorBoundary><Workspace token={token} onSignOut={signOut} /></ErrorBoundary>;
  return <main id="main" tabIndex={-1}><Page title="Sign in to Anvil">
    {notice && <p role="alert">{notice}</p>}
    <p>Use a scoped API key with runs:read permission. It stays in memory and is cleared on sign out or reload.</p>
    <form onSubmit={(event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const value = new FormData(form).get("key");
      if (typeof value === "string" && value.trim()) {
        form.reset();
        setNotice("");
        setToken(value.trim());
      }
    }}>
      <label htmlFor="key">API key</label>
      <input id="key" name="key" type="password" autoComplete="off" required pattern={String.raw`\S+`} title="Enter an API key without whitespace." />
      <button type="submit">Sign in</button>
    </form>
  </Page></main>;
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode><HashRouter><a className="skip-link" href="#main" onClick={(event) => {
    event.preventDefault();
    document.getElementById("main")?.focus();
  }}>Skip to main content</a><ErrorBoundary><App /></ErrorBoundary></HashRouter></React.StrictMode>,
);
