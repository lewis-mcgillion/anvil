import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { ApiError, dashboardClient, listProjects, type Project } from "./client";
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

function Page({ title, children }: React.PropsWithChildren<{ title: string }>) {
  const heading = useRef<HTMLHeadingElement>(null);
  const location = useLocation();
  useEffect(() => {
    document.title = `${title} | Anvil`;
    heading.current?.focus();
  }, [title, location.pathname]);
  return <><h1 ref={heading} tabIndex={-1}>{title}</h1>{children}</>;
}

function ProjectOverview({ projects }: { projects: Project[] }) {
  const { projectId } = useParams();
  const project = projects.find((item) => item.id === projectId);
  if (!project) {
    return <Page title="Project unavailable"><p>This project is not in the loaded authorized project list. Load additional projects or choose a visible project.</p><Link to="/">Choose a project</Link></Page>;
  }
  return <Page title={project.name}>
    <p>Project ID: <code>{project.id}</code></p>
    <p>Retention: {project.retention_days} days</p>
    <h2>Investigation capabilities</h2>
    <p>Run explorer and playback are not available in this dashboard yet. The Python server must implement the published run-list, event-stream and recording-projection contracts first.</p>
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
        <Route path="/projects/:projectId" element={<ProjectOverview projects={projects} />} />
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
