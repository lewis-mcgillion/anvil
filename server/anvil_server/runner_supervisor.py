"""Bounded, versioned protobuf handshake over the runner supervisor UDS."""

import asyncio

from .runner_pb2 import RunnerMessage, SupervisorMessage, RUN_OUTCOME_UNSPECIFIED
from .runner_policy import IsolationUnavailable


MAX_WIRE_BYTES = 65_536


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
