# Audit trail, deletion and privacy

Anvil stores other people's agent transcripts. Two obligations follow: an
operator must be able to say who touched that evidence, and a subject must be
able to have it destroyed. This document describes both, and what they
deliberately do not record.

Implementation: `server/anvil_server/audit.py` and
`server/anvil_server/deletion.py`.

## What is audited

| Kind | Recorded for |
| --- | --- |
| `read` | authenticated access to a stored recording |
| `export` | queueing an archive export, and downloading an archive |
| `replay` | building a playback projection, and queueing a comparison |
| `delete` | carrying out a deletion request |

`replay` covers re-derivation from a recording. A comparison is audited as
replay even though it executes no production tool: it reconstructs what an
agent did, and that is the thing worth knowing someone asked for.

Each row is written in the same transaction as the action it describes. An
audit row written afterwards is a row that survives a rolled-back deletion or
is lost to a crash between two commits — in both cases the trail disagrees
with reality, which is worse than having no trail at all.

## What an audit row may contain

Identifiers, counts and a reason. Nothing else.

- `kind` and `reason` both come from closed vocabularies. `reason` is not a
  free-text field, because a free-text field is eventually given a payload.
- `root_ids` and `resource_ids` are parsed as UUIDs rather than pattern
  matched. Anything that is not a UUID is rejected, which is the shape a
  leaked prompt or token would arrive in.
- `bytes` is a non-negative integer measured from the rows actually affected.

The audit trail is the record of who touched the evidence; it must not become
a second, longer-lived copy of it. `AuditError` messages name the rule that
failed and never quote the input, for the same reason the
[capture policy](capture-policy.md) does not.

There is no API for reading audit rows. The only published surface is
`audit_event_id` on a deletion request, which lets a caller prove their
deletion was carried out. Adding a read endpoint would need its own
authorization story, and the absence of one is not an oversight.

## Deletion

`POST /v1/projects/{id}/deletions` queues a job; the work happens in
`process_deletion`. Suites are rejected at the route, because they have no
storage yet and a worker must not invent a meaning for a target it cannot
delete.

Deleting a **run** also destroys its streams, events, artifacts, seal request,
any recording sealed from it, any archive export of that recording, and any
comparison in which it took part. Deleting a **recording** destroys the
recording, its exports and its comparisons, and leaves the run.

This is cascade-by-construction rather than cascade-by-constraint:
`recordings` references `runs` with `ON DELETE RESTRICT` precisely so that a
careless `DELETE` cannot silently destroy sealed evidence. The worker states
the order explicitly, from derived data back to its source.

### Errors, cancellation and cleanup

- The whole deletion — the graph removal, the audit row, and the link from the
  deletion request to that row — happens in one transaction. A failure rolls
  all of it back, the job is marked `failed` with an error, and nothing claims
  the data was destroyed.
- A retried lease on an already-completed request does nothing: the presence
  of `audit_event_id` is the completion marker, so a second run cannot delete
  a second graph or write a second audit row.
- A lease lost to expiry or cancellation cannot complete the job; the queue
  refuses the terminal update, and the worker returns `False` rather than
  reporting a success it could not commit.
- Nothing is held open across the transaction: the connection is scoped to the
  `project_connection` context manager, which also sets and clears the
  row-level security project.

## Limits

Deletion removes rows from the live database. Backups taken before the
deletion still contain them until those backups expire under their own
retention; the [retention rules](retention.md) govern that, and an operator
honouring a deletion request must account for it.
