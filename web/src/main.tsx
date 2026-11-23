import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, dashboardClient, listProjects, listProjectRuns, readRecording, watchRunEvents, type Project, type Run } from "./client";
import { captureQualityLabel } from "./capture-quality";
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

function ProjectOverview({ projects, client, token }: { projects: Project[]; client: ReturnType<typeof dashboardClient>; token: string }) {
  const { projectId } = useParams();
  const project = projects.find((item) => item.id === projectId);
  const [runs, setRuns] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<string>();
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [runError, setRunError] = useState<string>();
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [runQuery, setRunQuery] = useState("");
  const [captureFilter, setCaptureFilter] = useState("all");
  const [watchedRun, setWatchedRun] = useState<string>();
  const [watchError, setWatchError] = useState<string>();
  const [verifiedRecording, setVerifiedRecording] = useState<string>();
  const [recordingEligibility, setRecordingEligibility] = useState<string>();
  const [recordingError, setRecordingError] = useState<string>();
  useEffect(() => {
    if (!project || !verifiedRecording) return;
    const controller = new AbortController();
    setRecordingEligibility(undefined);
    setRecordingError(undefined);
    readRecording(client, controller.signal, project.id, verifiedRecording).then((recording) => {
      if (!controller.signal.aborted) setRecordingEligibility(recording.replay_eligibility);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setRecordingError(error instanceof Error ? error.message : "Recording unavailable");
    });
    return () => controller.abort();
  }, [client, project?.id, verifiedRecording]);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!project || !watchedRun) return;
    const controller = new AbortController();
    setWatchError(undefined);
    watchRunEvents(token, project.id, watchedRun, controller.signal, () => {
      setCursor(undefined);
      setRevision((value) => value + 1);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setWatchError(error instanceof Error ? error.message : "Live updates unavailable");
    });
    return () => controller.abort();
  }, [token, project?.id, watchedRun]);
  const visibleRuns = runs.filter((run) =>
    (captureFilter === "all" || run.capture_state === captureFilter) &&
    (`${run.agent_name} ${run.id}`.toLowerCase().includes(runQuery.toLowerCase())));
  useEffect(() => {
    if (!project) return;
    const controller = new AbortController();
    setLoadingRuns(true);
    setRunError(undefined);
    listProjectRuns(client, controller.signal, project.id, cursor).then((page) => {
      if (controller.signal.aborted) return;
      setRuns((previous) => cursor ? [...previous, ...page.items.filter((run) =>
        !previous.some((item) => item.id === run.id))] : page.items);
      setNextCursor(page.next_cursor);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setRunError(failure instanceof Error ? failure.message : "Unable to load runs");
    }).finally(() => { if (!controller.signal.aborted) setLoadingRuns(false); });
    return () => controller.abort();
  }, [client, project?.id, cursor, revision]);
  if (!project) {
    return <Page title="Project unavailable"><p>This project is not in the loaded authorized project list. Load additional projects or choose a visible project.</p><Link to="/">Choose a project</Link></Page>;
  }
  return <Page title={project.name}>
    <p>Project ID: <code>{project.id}</code></p>
    <p>Retention: {project.retention_days} days</p>
    <h2>Runs</h2>
    <label>Filter runs by agent or ID <input value={runQuery} onChange={(event) => setRunQuery(event.target.value)} /></label>
    <label>Capture status <select value={captureFilter} onChange={(event) => setCaptureFilter(event.target.value)}>
      <option value="all">All captures</option>
      <option value="open">Open</option>
      <option value="sealed_complete">Sealed complete</option>
      <option value="sealed_incomplete">Sealed incomplete</option>
    </select></label>
    {loadingRuns && <p role="status">Loading runs...</p>}
    {runError && <p role="alert">{runError}</p>}
    {!loadingRuns && !runError && runs.length === 0 && <p>No recorded runs in this project.</p>}
    {!loadingRuns && !runError && runs.length > 0 && visibleRuns.length === 0 && <p>No runs match these filters.</p>}
    {watchError && <p role="alert">Live updates stopped: {watchError}</p>}
    <ul>{visibleRuns.map((run) => <li key={run.id}><strong>{run.agent_name}</strong> <code>{run.id}</code>
      <span> — {run.capture_state}; {run.execution_state}; {captureQualityLabel({
        capture_status: run.capture_state, execution_outcome: run.execution_state,
      })}</span> {run.recording_id && <span>Recording: <code>{run.recording_id}</code>
        <button onClick={() => setVerifiedRecording(run.recording_id ?? undefined)}>Check recording eligibility</button>
        {verifiedRecording === run.recording_id && recordingEligibility &&
          <span> Manifest replay eligibility: {recordingEligibility}</span>}
        {verifiedRecording === run.recording_id && recordingError && <span role="alert">{recordingError}</span>}
      </span>}
      <button onClick={() => setWatchedRun(watchedRun === run.id ? undefined : run.id)}>
        {watchedRun === run.id ? "Stop live updates" : `Watch ${run.agent_name}`}
      </button></li>)}</ul>
    {nextCursor && <button disabled={loadingRuns} onClick={() => setCursor(nextCursor)}>Load more runs</button>}
    <p role="note">Replay eligibility is not verified by run summaries. A sealed recording manifest is required before any run can be considered replay-eligible. This dashboard never executes agent code.</p>
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
        <Route path="/projects/:projectId" element={<ProjectOverview projects={projects} client={client} token={token} />} />
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
