# Event storage compatibility

The event store currently accepts `schema_version: "1"` only. Events are
identified by `(project_id, event_id)` and carry a SHA-256 hash of the
canonical event content. Retrying a batch with the same content is a no-op;
reusing an identity with different content is a conflict and the transaction
is rolled back.

Sequences are scoped to a stream. A batch must continue at the stream's
`next_sequence`; the stream row is locked for the duration of the transaction.
The store rejects batches larger than 500 events, larger than 4 MiB, or with an
event larger than 256 KiB. These limits match the capture contract in
[`architecture.md`](architecture.md).

Events are never updated or deleted through the storage schema. Playback reads
the immutable rows and does not invoke agents, models, tools, or the event
writer. A future schema version requires an explicit migration and a
compatibility entry here; it must not reinterpret version 1 rows in place.
