# Retention, deletion and storage

Projects retain data for **30 days by default**. A project policy may choose a
different non-negative duration; setting a policy to zero restores the default.
Protected resources are not removed by the retention sweep.

Deleting a run is a graph operation. The request's project is checked before
anything is changed, then the run and every project-owned recording, artifact,
re-execution result, comparison or other derived resource that references it
(directly or transitively) are removed. A missing root, cross-project reference,
quota violation or cancelled request fails without a partial deletion.

Project quotas count active resource payload bytes and are checked before an
insert. Deletion writes immutable audit evidence containing the project, roots,
all removed resource IDs, byte total, reason and timestamp. Audit records do not
contain raw prompts or payloads.

Backups are independent retention objects. They default to the same 30-day
retention but are not resurrected by restoring an expired active resource.
Operators must run backup pruning; an expired backup remains accounted for until
that operation succeeds. A cancelled prune leaves the unvisited backups in
place.
