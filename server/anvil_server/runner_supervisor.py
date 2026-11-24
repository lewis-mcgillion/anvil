"""Bounded, versioned protobuf handshake over the runner supervisor UDS."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from .runner_pb2 import RunnerMessage, SupervisorMessage, RUN_OUTCOME_UNSPECIFIED
from .runner_policy import IsolationUnavailable
from .runner_policy import RunnerLaunch, supervise_container, verify_local_runner_image
from .runner_fixtures import RecordedRunnerFixtures


MAX_WIRE_BYTES = 65_536


def _sandbox_owned(path: Path) -> bool:
    return path.stat().st_uid == 65534


async def read_runner_message(reader: asyncio.StreamReader) -> RunnerMessage:
    try:
        header = await reader.readexactly(4)
        length = int.from_bytes(header, "big")
        if not 0 < length <= MAX_WIRE_BYTES:
            raise IsolationUnavailable("Runner protobuf frame size is invalid")
        message = RunnerMessage()
        message.ParseFromString(await reader.readexactly(length))
        if message.WhichOneof("message") is None:
            raise IsolationUnavailable("Runner protobuf envelope is empty")
        return message
    except (asyncio.IncompleteReadError, ValueError) as error:
        raise IsolationUnavailable("Runner protobuf frame is invalid") from error


async def write_supervisor_message(writer: asyncio.StreamWriter, message: SupervisorMessage) -> None:
    if message.WhichOneof("message") is None:
        raise IsolationUnavailable("Supervisor protobuf envelope is empty")
    frame = message.SerializeToString()
    if not 0 < len(frame) <= MAX_WIRE_BYTES:
        raise IsolationUnavailable("Supervisor protobuf frame size is invalid")
    writer.write(len(frame).to_bytes(4, "big") + frame)
    await writer.drain()


async def handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    config: SupervisorMessage, *, adapter: str, timeout_seconds: float = 5) -> None:
    if timeout_seconds <= 0 or config.WhichOneof("message") != "config":
        raise IsolationUnavailable("Supervisor handshake configuration is invalid")
    async with asyncio.timeout(timeout_seconds):
        hello = await read_runner_message(reader)
        if (hello.WhichOneof("message") != "hello" or hello.hello.protocol_version != "1"
                or hello.hello.adapter != adapter or not hello.hello.runner_version
                or not hello.hello.adapter_version):
            raise IsolationUnavailable("Runner protocol or adapter handshake mismatch")
        await write_supervisor_message(writer, config)


async def receive_terminal_outcome(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                   *, timeout_seconds: float = 60) -> int:
    if timeout_seconds <= 0:
        raise IsolationUnavailable("Supervisor terminal deadline must be positive")
    async with asyncio.timeout(timeout_seconds):
        message = await read_runner_message(reader)
        if message.WhichOneof("message") != "finished":
            raise IsolationUnavailable("Runner requested unsupported supervisor action")
        if message.finished.outcome == RUN_OUTCOME_UNSPECIFIED:
            raise IsolationUnavailable("Runner reported unspecified terminal outcome")
        await write_supervisor_message(writer, SupervisorMessage(finished={}))
        return message.finished.outcome


async def supervise_recorded_messages(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                      *, run_id: str,
                                      persist_events: Callable[[object], Awaitable[None]],
                                      resolve_model: Callable[[object], Awaitable[SupervisorMessage]] | None = None,
                                      resolve_tool: Callable[[object], Awaitable[SupervisorMessage]] | None = None,
                                      finalize_fixtures: Callable[[], Awaitable[None]] | None = None,
                                      timeout_seconds: float = 60) -> int:
    if not run_id or timeout_seconds <= 0 or not callable(persist_events):
        raise IsolationUnavailable("Invalid recorded runner supervision configuration")
    sequences: dict[str, int] = {}
    async with asyncio.timeout(timeout_seconds):
        while True:
            message = await read_runner_message(reader)
            kind = message.WhichOneof("message")
            if kind == "heartbeat":
                await write_supervisor_message(writer, SupervisorMessage(heartbeat=message.heartbeat))
            elif kind in {"model_request", "tool_request"}:
                request = getattr(message, kind)
                resolver = resolve_model if kind == "model_request" else resolve_tool
                if not request.request_id or resolver is None:
                    raise IsolationUnavailable("Runner requested unavailable fixture mediation")
                response = await resolver(request)
                expected_kind = "model_response" if kind == "model_request" else "tool_response"
                if (not isinstance(response, SupervisorMessage) or response.WhichOneof("message") != expected_kind
                        or getattr(response, expected_kind).request_id != request.request_id):
                    raise IsolationUnavailable("Runner fixture response correlation mismatch")
                await write_supervisor_message(writer, response)
            elif kind == "emit_events":
                batch = message.emit_events
                if batch.run_id != run_id or not batch.stream_id or not batch.events:
                    raise IsolationUnavailable("Runner event batch has invalid identity")
                expected = sequences.get(batch.stream_id, 0)
                for event in batch.events:
                    if (event.run_id != run_id or event.stream_id != batch.stream_id
                            or event.sequence != expected):
                        raise IsolationUnavailable("Runner event sequence or identity mismatch")
                    expected += 1
                await persist_events(batch)
                sequences[batch.stream_id] = expected
                await write_supervisor_message(writer, SupervisorMessage(event_ack={
                    "run_id": run_id, "stream_id": batch.stream_id,
                    "acked_through_sequence": expected - 1,
                }))
            elif kind == "finished":
                if message.finished.outcome == RUN_OUTCOME_UNSPECIFIED:
                    raise IsolationUnavailable("Runner reported unspecified terminal outcome")
                if message.finished.outcome == 1:
                    if finalize_fixtures is None:
                        raise IsolationUnavailable("Successful runner requires fixture verification")
                    await finalize_fixtures()
                await write_supervisor_message(writer, SupervisorMessage(finished={}))
                return message.finished.outcome
            else:
                raise IsolationUnavailable("Runner requested unsupported supervisor action")


async def run_recorded_protocol(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                                *, adapter: str, config: SupervisorMessage,
                                fixtures: RecordedRunnerFixtures,
                                persist_events: Callable[[object], Awaitable[None]],
                                timeout_seconds: float = 60) -> int:
    try:
        async with asyncio.timeout(timeout_seconds):
            await handshake(reader, writer, config, adapter=adapter)
            return await supervise_recorded_messages(
                reader, writer, run_id=config.config.run_id,
                persist_events=persist_events,
                resolve_model=fixtures.resolve_model, resolve_tool=fixtures.resolve_tool,
                finalize_fixtures=fixtures.finalize, timeout_seconds=timeout_seconds,
            )
    finally:
        fixtures.fixtures.close()


async def run_isolated_recorded_session(
    launch: RunnerLaunch, socket_directory: Path, seccomp_profile: Path,
    *, adapter: str, config: SupervisorMessage, fixtures: RecordedRunnerFixtures,
    persist_events: Callable[[object], Awaitable[None]], timeout_seconds: float = 60,
) -> int:
    await verify_local_runner_image(launch)
    socket_path = socket_directory / "supervisor.sock"
    if socket_directory.is_symlink() or not socket_directory.is_dir():
        raise IsolationUnavailable("Runner socket directory is invalid")
    if not _sandbox_owned(socket_directory):
        raise IsolationUnavailable("Runner socket directory must belong to sandbox identity")
    if socket_path.exists() or socket_path.is_symlink():
        raise IsolationUnavailable("Runner supervisor socket path already exists")
    session_result = asyncio.get_running_loop().create_future()
    accepted = False
    connection_tasks: set[asyncio.Task] = set()

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accepted
        connection_tasks.add(asyncio.current_task())
        if accepted:
            writer.close()
            await writer.wait_closed()
            connection_tasks.discard(asyncio.current_task())
            return
        accepted = True
        try:
            result = await run_recorded_protocol(
                reader, writer, adapter=adapter, config=config, fixtures=fixtures,
                persist_events=persist_events, timeout_seconds=timeout_seconds,
            )
            if not session_result.done():
                session_result.set_result(result)
        except BaseException as error:
            if not session_result.done():
                session_result.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()
            connection_tasks.discard(asyncio.current_task())

    server = await asyncio.start_unix_server(accept, path=str(socket_path))
    try:
        os.chown(socket_path, 65534, 65534)
        socket_path.chmod(0o600)
        if not _sandbox_owned(socket_path):
            raise IsolationUnavailable("Runner socket must belong to sandbox identity")
    except BaseException:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        raise
    container_task = None
    try:
        async with server:
            container_task = asyncio.create_task(supervise_container(
                launch, socket_directory, seccomp_profile, timeout_seconds=timeout_seconds,
            ))
            done, _ = await asyncio.wait({container_task, session_result},
                                         return_when=asyncio.FIRST_COMPLETED,
                                         timeout=timeout_seconds)
            if not done:
                raise IsolationUnavailable("Runner session timed out")
            if container_task in done:
                await container_task
                if not session_result.done():
                    raise IsolationUnavailable("Runner exited without supervisor completion")
            outcome = await session_result
            if outcome != 1:
                raise IsolationUnavailable("Runner reported unsuccessful outcome")
            await container_task
            return outcome
    finally:
        if container_task is not None and not container_task.done():
            container_task.cancel()
            try:
                await container_task
            except asyncio.CancelledError:
                pass
        for task in connection_tasks:
            task.cancel()
        if connection_tasks:
            await asyncio.gather(*connection_tasks, return_exceptions=True)
        if not session_result.done():
            session_result.cancel()
        if session_result.done() and not session_result.cancelled():
            session_result.exception() if session_result.exception() is not None else None
        socket_path.unlink(missing_ok=True)
