"""``anvil playback`` — inspect a recorded run without executing anything.

Recorded playback is a pure read. This command loads a recording from a file
or from the Anvil API, projects it, and prints the result. It never loads an
agent, calls a tool, contacts a model provider or schedules a job, and every
output carries the same explicit mode labels the REST API returns so a result
can be audited after the fact.

Exit codes are stable and distinguish failure kinds, so CI can branch on them:

====  ==========================================================
Code  Meaning
====  ==========================================================
0     Success.
1     Usage error (unreadable file, bad option, unknown cursor).
2     The recording could not be projected or reconstructed.
3     The API rejected the request or could not be reached.
130   Interrupted by the operator (SIGINT).
====  ==========================================================
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping

import typer

from .playback import (
    PROJECTION_VERSION,
    SUPPORTED_PROJECTION_VERSIONS,
    PlaybackError,
    ProjectionIndex,
    projection_hash,
)
from .streaming import ManualClock, PlaybackSession, StreamingError, reconstruct_streams

#: Mirrors ``anvil_server.app.PLAYBACK_MODE`` so CLI and REST output agree.
PLAYBACK_MODE = {
    "mode": "recorded_playback",
    "executes_agent_code": False,
    "executes_tools": False,
    "contacts_model_provider": False,
    "schedules_jobs": False,
}

EXIT_USAGE = 1
EXIT_UNPROJECTABLE = 2
EXIT_API = 3
EXIT_INTERRUPTED = 130

#: The list-events endpoint caps ``limit`` at 500.
PAGE_SIZE = 500
#: Bound the paging loop so a misbehaving server cannot spin forever.
MAX_EVENT_PAGES = 1000


class CliError(Exception):
    """A failure that should be reported to the operator, not a traceback."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


app = typer.Typer(
    name="anvil",
    help="Anvil recorded playback. Reading a recording never executes it.",
    no_args_is_help=True,
    add_completion=False,
)
playback_app = typer.Typer(
    help="Inspect a recorded run. Playback is read-only.",
    no_args_is_help=True,
)
app.add_typer(playback_app, name="playback")


def _emit(payload: Mapping[str, Any]) -> None:
    """Write one JSON document to stdout with a trailing newline."""
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _read_file(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = path.read_text()
    except OSError as error:
        raise CliError(f"Cannot read {path}: {error.strerror or error}", EXIT_USAGE)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CliError(f"{path} is not valid JSON: {error}", EXIT_USAGE)
    if not isinstance(document, dict):
        raise CliError(f"{path} must contain a JSON object", EXIT_USAGE)
    return document


def _events_from(document: Mapping[str, Any], source: str) -> list[Mapping[str, Any]]:
    """Extract events from a recording file or an API page.

    Recording files carry ``events``; the API's paginated list carries
    ``items``. Accept both rather than silently projecting nothing.
    """
    events = document.get("events")
    if events is None:
        events = document.get("items")
    if not isinstance(events, list):
        raise CliError(f"{source} has no 'events' array", EXIT_USAGE)
    for event in events:
        if not isinstance(event, dict):
            raise CliError(f"{source} contains a non-object event", EXIT_USAGE)
    return events


def _run_id_from(document: Mapping[str, Any]) -> str | None:
    run = document.get("run")
    if isinstance(run, dict) and isinstance(run.get("run_id"), str):
        return run["run_id"]
    value = document.get("run_id")
    return value if isinstance(value, str) else None


def _fetch(url: str, token: str | None, timeout: float) -> dict[str, Any]:
    """GET a recording from the API. Only reads; never POSTs or schedules."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()
        raise CliError(
            f"API returned {error.code} for {url}" + (f": {detail}" if detail else ""),
            EXIT_API,
        )
    except urllib.error.URLError as error:
        raise CliError(f"Cannot reach {url}: {error.reason}", EXIT_API)
    except TimeoutError:
        raise CliError(f"Timed out after {timeout}s contacting {url}", EXIT_API)
    try:
        document = json.loads(body)
    except json.JSONDecodeError as error:
        raise CliError(f"API returned invalid JSON: {error}", EXIT_API)
    if not isinstance(document, dict):
        raise CliError("API returned a non-object response", EXIT_API)
    return document


def _load(
    recording: pathlib.Path | None,
    api: str | None,
    project: str | None,
    run: str | None,
    token: str | None,
    timeout: float,
) -> tuple[list[Mapping[str, Any]], str | None]:
    """Resolve the two mutually exclusive sources into events plus a run id."""
    if recording is not None and api is not None:
        raise CliError("Use either --recording or --api, not both", EXIT_USAGE)
    if recording is not None:
        document = _read_file(recording)
        return _events_from(document, str(recording)), _run_id_from(document)
    if api is not None:
        if not project or not run:
            raise CliError("--api requires --project and --run", EXIT_USAGE)
        if urllib.parse.urlparse(api).scheme not in {"http", "https"}:
            raise CliError("--api must be an http or https URL", EXIT_USAGE)
        base = (
            f"{api.rstrip('/')}/v1/projects/{urllib.parse.quote(project)}"
            f"/runs/{urllib.parse.quote(run)}/events"
        )
        # The list endpoint is paginated. Projecting only the first page would
        # silently truncate the recording, so follow every cursor to the end.
        events: list[Mapping[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        run_id: str | None = None
        for _ in range(MAX_EVENT_PAGES):
            url = base + f"?limit={PAGE_SIZE}"
            if cursor is not None:
                url += "&cursor=" + urllib.parse.quote(cursor)
            document = _fetch(url, token, timeout)
            events.extend(_events_from(document, url))
            run_id = run_id or _run_id_from(document)
            cursor = document.get("next_cursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise CliError(
                    "API returned a repeated or invalid pagination cursor", EXIT_API
                )
            seen.add(cursor)
        else:
            raise CliError(
                f"Recording exceeds {MAX_EVENT_PAGES} pages; project it server-side",
                EXIT_API,
            )
        return events, run_id or run
    raise CliError("Provide --recording PATH or --api URL", EXIT_USAGE)


def _index(
    events: Iterable[Mapping[str, Any]], run_id: str | None, version: str
) -> ProjectionIndex:
    if version not in SUPPORTED_PROJECTION_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_PROJECTION_VERSIONS))
        raise CliError(
            f"Projection version {version!r} is not supported (have: {supported})",
            EXIT_USAGE,
        )
    try:
        return ProjectionIndex(events, version=version, run_id=run_id)
    except PlaybackError as error:
        raise CliError(f"Recording cannot be projected: {error}", EXIT_UNPROJECTABLE)


_RECORDING = typer.Option(
    None, "--recording", "-r", help="Path to a JSON recording.", exists=False
)
_API = typer.Option(None, "--api", help="Base URL of the Anvil API.")
_PROJECT = typer.Option(None, "--project", help="Project id, required with --api.")
_RUN = typer.Option(None, "--run", help="Run id, required with --api.")
_TOKEN = typer.Option(
    None,
    "--token",
    help="Bearer token. Defaults to ANVIL_TOKEN. Prefer the environment variable.",
)
_TIMEOUT = typer.Option(30.0, "--timeout", min=0.1, help="API timeout in seconds.")
_VERSION = typer.Option(
    PROJECTION_VERSION, "--projection-version", help="Projection version to apply."
)


def _token_or_env(token: str | None) -> str | None:
    return token or os.environ.get("ANVIL_TOKEN")


@playback_app.command("projection")
def projection_command(
    recording: pathlib.Path = _RECORDING,
    api: str = _API,
    project: str = _PROJECT,
    run: str = _RUN,
    token: str = _TOKEN,
    timeout: float = _TIMEOUT,
    projection_version: str = _VERSION,
    cursor: str = typer.Option(
        None, "--cursor", help="Event id to stop at. Defaults to the whole recording."
    ),
) -> None:
    """Print the projection of a recording and its canonical hash."""
    events, run_id = _load(recording, api, project, run, _token_or_env(token), timeout)
    index = _index(events, run_id, projection_version)
    if cursor is None:
        position = len(index)
    else:
        try:
            position = index.position_of(cursor)
        except PlaybackError:
            raise CliError(
                "Cursor does not identify an event in this recording", EXIT_USAGE
            )
    projection = index.at(position)
    _emit(
        {
            **PLAYBACK_MODE,
            "run_id": run_id,
            "projection_version": projection_version,
            "position": position,
            "event_count": len(index),
            "cursor": index.cursor(position),
            "next_cursor": index.cursor(position + 1) if position < len(index) else None,
            "checkpoint_positions": index.checkpoint_positions,
            "projection_hash": projection_hash(projection),
            "projection": projection,
        }
    )


@playback_app.command("streams")
def streams_command(
    recording: pathlib.Path = _RECORDING,
    api: str = _API,
    project: str = _PROJECT,
    run: str = _RUN,
    token: str = _TOKEN,
    timeout: float = _TIMEOUT,
    projection_version: str = _VERSION,
    transcript: bool = typer.Option(
        False, "--transcript", help="Print the reconstructed text instead of JSON."
    ),
) -> None:
    """Reconstruct what a user saw streamed, without replaying the model."""
    events, run_id = _load(recording, api, project, run, _token_or_env(token), timeout)
    index = _index(events, run_id, projection_version)
    try:
        records = reconstruct_streams(index.events)
    except StreamingError as error:
        raise CliError(f"Stream cannot be reconstructed: {error}", EXIT_UNPROJECTABLE)
    if transcript:
        for record in records:
            sys.stdout.write(record["text"] + "\n")
        return
    _emit(
        {
            **PLAYBACK_MODE,
            "run_id": run_id,
            "projection_version": projection_version,
            "streams": [
                {**record, "frames": [frame.as_dict() for frame in record["frames"]]}
                for record in records
            ],
        }
    )


@playback_app.command("play")
def play_command(
    recording: pathlib.Path = _RECORDING,
    api: str = _API,
    project: str = _PROJECT,
    run: str = _RUN,
    token: str = _TOKEN,
    timeout: float = _TIMEOUT,
    projection_version: str = _VERSION,
    speed: float = typer.Option(
        1.0, "--speed", min=0.0, help="Playback speed. 0 emits frames immediately."
    ),
) -> None:
    """Replay the recorded stream frame by frame to stdout.

    Timing is simulated, never slept in real time, so the command is safe in
    CI and cannot hang. Frame identity is independent of ``--speed``.
    """
    events, run_id = _load(recording, api, project, run, _token_or_env(token), timeout)
    index = _index(events, run_id, projection_version)
    try:
        records = reconstruct_streams(index.events)
    except StreamingError as error:
        raise CliError(f"Stream cannot be reconstructed: {error}", EXIT_UNPROJECTABLE)
    # ManualClock keeps the command deterministic: the recorded offsets are
    # reported, not waited on.
    for record in records:
        session = PlaybackSession(
            record["frames"], clock=ManualClock(), speed=speed or 1.0
        )
        # Always release the session, including on interrupt or broken pipe.
        with session:
            for frame in session.play():
                sys.stdout.write(frame.delta)
                sys.stdout.flush()
        sys.stdout.write("\n")


@playback_app.command("verify")
def verify_command(
    recording: pathlib.Path = _RECORDING,
    api: str = _API,
    project: str = _PROJECT,
    run: str = _RUN,
    token: str = _TOKEN,
    timeout: float = _TIMEOUT,
    projection_version: str = _VERSION,
    expect: str = typer.Option(
        None, "--expect", help="Projection hash this recording must produce."
    ),
) -> None:
    """Check that a recording still projects to an expected hash.

    Intended for CI: a drifted hash exits non-zero with both values reported.
    """
    events, run_id = _load(recording, api, project, run, _token_or_env(token), timeout)
    index = _index(events, run_id, projection_version)
    actual = projection_hash(index.full())
    matched = expect is None or actual == expect
    _emit(
        {
            **PLAYBACK_MODE,
            "run_id": run_id,
            "projection_version": projection_version,
            "event_count": len(index),
            "projection_hash": actual,
            "expected_hash": expect,
            "matched": matched,
        }
    )
    if not matched:
        raise CliError(
            f"Projection hash mismatch: expected {expect}, got {actual}",
            EXIT_UNPROJECTABLE,
        )


def _click_modules() -> tuple[Any, ...]:
    """Return every Click implementation whose exceptions Typer might raise.

    Typer >= 0.27 vendors Click as ``typer._click`` and raises *its* exception
    classes, but a standalone ``click`` may also be installed (uvicorn pulls
    one in). The two define distinct classes with identical names, so catching
    only one silently misses the other.
    """
    modules = []
    try:
        from typer import _click  # type: ignore[attr-defined]

        modules.append(_click)
    except ImportError:  # pragma: no cover - older Typer layout
        pass
    try:
        import click  # type: ignore[import-not-found]

        modules.append(click)
    except ModuleNotFoundError:  # pragma: no cover - Typer vendors Click
        pass
    return tuple(modules)


def click_exceptions() -> tuple[type[BaseException], ...]:
    """Return Click's exit and usage exceptions, tolerating layout drift."""
    found: list[type[BaseException]] = []
    for module in _click_modules():
        namespace = getattr(module, "exceptions", module)
        for name in ("Exit", "Abort", "UsageError", "ClickException"):
            candidate = getattr(namespace, name, None) or getattr(module, name, None)
            if (
                isinstance(candidate, type)
                and issubclass(candidate, BaseException)
                and candidate not in found
            ):
                found.append(candidate)
    return tuple(found) or (Exception,)


def _exit_code_of(error: BaseException) -> int | None:
    """Return the exit code Click attached to a control-flow exception."""
    code = getattr(error, "exit_code", None)
    return int(code) if isinstance(code, int) else None


def main(argv: list[str] | None = None) -> int:
    """Entry point returning an exit code rather than raising."""
    try:
        app(args=argv, standalone_mode=False)
    except CliError as error:
        typer.echo(f"error: {error}", err=True)
        return error.code
    except KeyboardInterrupt:  # pragma: no cover - operator action
        typer.echo("error: interrupted", err=True)
        return EXIT_INTERRUPTED
    except BrokenPipeError:  # pragma: no cover - downstream closed the pipe
        return 0
    except typer.Exit as exit_signal:
        return int(exit_signal.exit_code)
    except typer.Abort:  # pragma: no cover - operator action
        return EXIT_INTERRUPTED
    except click_exceptions() as error:  # type: ignore[misc]
        # ``--help`` and ``no_args_is_help`` arrive here as control flow rather
        # than failure: Click has already written the help text, so report
        # success instead of echoing a spurious error line.
        if type(error).__name__ == "NoArgsIsHelpError":
            # Click uses exit code 2 here, which this CLI reserves for an
            # unprojectable recording. Report it as a usage error instead.
            return EXIT_USAGE
        if type(error).__name__ == "Exit":
            code = _exit_code_of(error)
            return code if code is not None else 0
        if type(error).__name__ == "Abort":  # pragma: no cover - operator action
            return EXIT_INTERRUPTED
        message = getattr(error, "format_message", lambda: str(error))()
        typer.echo(f"error: {message}", err=True)
        return EXIT_USAGE
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
