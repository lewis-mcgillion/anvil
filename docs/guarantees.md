# What Anvil guarantees

A guarantee is a sentence someone will later rely on, so every claim below
names the test that holds it up. The citation is the point of the document:
a claim without one is a marketing sentence, and `tests/test_guarantees.py`
fails the build if any appears here.

Claims are written as:

```
- Claim. — proof: `path/to/test_file.py::test_name`
```

A proof is a single test, not a suite, so a reader can run it:

```
cd server && python -m pytest -q \
  tests/test_playback.py::TestCausalOrdering::test_wall_clock_order_is_never_used
```

Section [Not guaranteed](#not-guaranteed) lists what Anvil does *not*
promise. It carries no citations, because absence of a guarantee is not
something a test can demonstrate, and it is the more useful half of this
document for anyone deciding whether to depend on Anvil today.

## Capture

- A captured event is copied at the boundary, so mutating the object an
  agent passed in afterwards cannot change what was recorded. — proof:
  `sdk/python/tests/test_capture.py::test_nested_runs_spans_and_copy_on_capture`
- Capture never swallows the agent's own failure: the original exception and
  its context survive, including when capture's own cleanup fails. — proof:
  `sdk/python/tests/test_capture.py::test_capture_cleanup_error_does_not_replace_agent_exception`
- A cancelled async capture context releases its task without leaking state
  into a sibling task. — proof:
  `sdk/python/tests/test_capture.py::test_async_context_cancellation_and_task_isolation`
- An event the client rejects is never exported, so a contract violation
  fails at the boundary rather than becoming a stored recording. — proof:
  `sdk/python/tests/test_capture.py::test_rejected_events_never_export`
- Dropping events under pressure is visible: the buffer is bounded, never
  blocks the agent, and emits a gap marker rather than silently losing
  history. — proof:
  `sdk/python/tests/test_capture.py::test_overflow_is_nonblocking_bounded_and_emits_gap`
- A retried export keeps its identity and its exact request bytes, so a
  retry cannot be mistaken for a second event. — proof:
  `sdk/python/tests/test_capture.py::test_retries_preserve_identity_and_request_bytes`
- A duplicate event in a batch is rejected rather than stored twice. —
  proof: `server/tests/test_events.py::test_batch_duplicate_rejected`

## Redaction and capture policy

- A field named for a credential never reaches storage, whatever the client
  declared. — proof:
  `server/tests/test_capture_policy.py::TestCredentialKeys::test_a_credential_named_field_never_survives`
- A credential embedded in prose is removed without destroying the
  surrounding prose. — proof:
  `server/tests/test_capture_policy.py::TestCredentialValues::test_a_credential_inside_prose_is_removed_without_losing_the_prose`
- The policy can only ever remove eligibility for replay; it cannot grant
  it. — proof:
  `server/tests/test_capture_policy.py::TestReplayEligibility::test_the_policy_can_never_grant_eligibility`
- A stored event's content hash commits to what was actually stored, not to
  what was submitted. — proof:
  `server/tests/test_capture_policy.py::TestBatchEnforcement::test_the_content_hash_commits_to_what_was_stored`
- A policy error never quotes the payload that caused it. — proof:
  `server/tests/test_capture_policy.py::TestErrorsDoNotLeak::test_a_policy_error_never_quotes_the_payload`
- Client-side redaction happens before the value reaches HTTP, not after. —
  proof:
  `sdk/python/tests/test_capture.py::test_run_creation_is_typed_and_redacted_before_http`

See [capture-policy.md](capture-policy.md) for what the policy matches and
what redaction costs a recording.

## Determinism

- Canonical form follows RFC 8785, so two agents that produced the same
  value produce the same bytes. — proof:
  `server/tests/test_canonical.py::test_rfc8785_reference_object`
- A value whose canonical form would be ambiguous is refused rather than
  guessed at. — proof:
  `server/tests/test_canonical.py::test_ambiguous_values_fail_closed`
- A normalization policy cannot hide a field without saying so: removals are
  reported as evidence. — proof:
  `server/tests/test_canonical.py::TestNormalizationPolicy::test_hidden_field_removal_is_impossible`
- The policy's identity is part of the hash, so a recording compared under a
  different policy cannot look identical. — proof:
  `server/tests/test_canonical.py::TestNormalizationPolicy::test_policy_identity_is_part_of_the_hash`

See [normalization.md](normalization.md).

## Playback

- Order comes from recorded causality, never from wall-clock timestamps. —
  proof:
  `server/tests/test_playback.py::TestCausalOrdering::test_wall_clock_order_is_never_used`
- Projection is independent of the order events were ingested in. — proof:
  `server/tests/test_playback.py::TestCausalOrdering::test_order_is_independent_of_ingestion_order`
- A missing parent fails closed rather than producing a partial order. —
  proof: `server/tests/test_playback.py::TestCausalOrdering::test_missing_parent_fails_closed`
- A cycle is detected rather than followed. — proof:
  `server/tests/test_playback.py::TestCausalOrdering::test_cycle_across_streams_is_detected`
- A gap in a stream's sequence is rejected. — proof:
  `server/tests/test_playback.py::TestCausalOrdering::test_sequence_gap_is_rejected`
- Seeking to a position yields exactly the projection of that prefix, for
  every position. — proof:
  `server/tests/test_playback.py::TestProjectionIndex::test_every_seek_equals_the_full_projection_of_that_prefix`
- A cursor round-trips to the position it names, and an unknown cursor is
  rejected. — proof:
  `server/tests/test_playback.py::TestProjectionIndex::test_unknown_cursor_is_rejected`
- Projection does not mutate the events it was given. — proof:
  `server/tests/test_playback.py::TestProjection::test_projection_does_not_mutate_input_events`

See [playback.md](playback.md).

## Incomplete recordings

- A recording is eligible for sealing only when its capture is complete and
  replay-safe. — proof:
  `server/tests/test_sealing.py::test_complete_capture_is_eligible_only_when_replay_safe`
- A missing terminal event or a cycle fails closed. — proof:
  `server/tests/test_sealing.py::test_missing_terminal_and_cycle_fail_closed`
- A capture gap blocks replay and is reported as the reason. — proof:
  `server/tests/test_playback.py::TestProjection::test_capture_gap_blocks_replay_and_is_reported`
- A payload the policy could not make safe blocks replay. — proof:
  `server/tests/test_playback.py::TestProjection::test_replay_unsafe_payload_blocks_replay`
- An unresolved tool invocation blocks replay rather than being skipped. —
  proof: `server/tests/test_playback.py::TestProjection::test_unresolved_invocation_blocks_replay`
- An event type the projection does not understand is a blocker, not silent
  success. — proof:
  `server/tests/test_playback.py::TestProjection::test_unknown_event_type_is_a_blocker_not_silent_success`

## Re-execution

- A tool call is matched on its canonical name, schema and arguments, and a
  fixture is consumed exactly once. — proof:
  `server/tests/test_replay_fixtures.py::test_strict_tool_canonical_keys_and_single_consumption`
- A missing, reordered, changed or invalid call aborts the session instead
  of falling through to a live tool. — proof:
  `server/tests/test_replay_fixtures.py::test_missing_reordered_changed_and_invalid_calls_abort`
- Two concurrent requests for the same fixture produce one response, not
  two. — proof:
  `server/tests/test_replay_fixtures.py::test_racing_same_consumption_only_one_response`
- A finished session rejects unused fixtures, so a partial replay cannot
  report success. — proof:
  `server/tests/test_replay_fixtures.py::test_missing_and_unconsumed_fixtures_never_succeed`
- The replay package has no provider, file, network or subprocess
  capability: the no-production-execution invariant is a property of what it
  can import, not of how it is called. — proof:
  `server/tests/test_replay_fixtures.py::test_no_provider_tool_files_network_or_subprocess_capability`
- Cancelling a session releases its payloads and rejects reuse. — proof:
  `server/tests/test_replay_fixtures.py::test_terminal_cleanup_releases_payloads_and_rejects_reuse`

See [recorded-reexecution.md](recorded-reexecution.md).

## Fault injection

- Fixture matching happens before any fault is applied, so a fault cannot
  smuggle in an unmatched call. — proof:
  `server/tests/test_fault_engine.py::test_fixture_matching_precedes_faults_even_for_errors`
- A simulated timeout is a typed simulation, not a real sleep. — proof:
  `server/tests/test_fault_engine.py::test_timeout_is_typed_simulation_not_a_wall_clock_sleep`
- Injected text is data: it is never treated as an instruction to execute. —
  proof: `server/tests/test_fault_engine.py::test_malicious_text_is_data_not_an_execution_request`
- A mutation that would exceed the output budget is refused before it is
  applied. — proof:
  `server/tests/test_fault_engine.py::test_combined_mutation_cannot_exceed_output_budget`

See [faults.md](faults.md).

## Comparison and gating

- Every combination of underlying statuses produces a published outcome, and
  every published outcome is reachable. — proof:
  `server/tests/test_divergence_gating.py::TestCoverage::test_every_combination_produces_a_published_outcome`
- Partial evidence never passes. — proof:
  `server/tests/test_divergence_gating.py::TestNoFalsePasses::test_partial_evidence_never_passes`
- An infrastructure fault never passes, and outranks a deterministic
  failure. — proof:
  `server/tests/test_divergence_gating.py::TestInfrastructurePrecedence::test_infrastructure_outranks_a_deterministic_failure`
- Judges are off by default. — proof:
  `server/tests/test_divergence_gating.py::TestJudgeGating::test_judges_are_off_by_default`
- An enabled judge can only withhold a pass; it can never rescue a
  deterministic or infrastructure failure. — proof:
  `server/tests/test_divergence_gating.py::TestJudgeGating::test_a_gating_judge_can_only_withhold_a_pass`
- No judge verdict, however confident, can rescue a deterministic
  failure. — proof:
  `server/tests/test_divergence_judge.py::TestNeverOverridesDeterminism::test_no_verdict_can_rescue_a_deterministic_failure`
- An invalid, refused or off-schema judge response is inconclusive, never a
  verdict. — proof:
  `server/tests/test_divergence_judge.py::TestMalformedResponses::test_a_response_that_is_not_an_object_is_inconclusive`
- An unknown field in a judge response is refused rather than ignored. —
  proof:
  `server/tests/test_divergence_judge.py::TestMalformedResponses::test_an_unknown_field_is_inconclusive_rather_than_ignored`
- A judge that cites evidence it was never shown is inconclusive. — proof:
  `server/tests/test_divergence_judge.py::TestMalformedResponses::test_a_fabricated_citation_invalidates_the_answer`
- A verdict below the rubric's required confidence is inconclusive. —
  proof:
  `server/tests/test_divergence_judge.py::TestUncertainty::test_a_verdict_below_the_rubric_threshold_is_inconclusive`
- A broker that fails, times out or is cancelled cannot break the
  comparison, and cannot be retried past its budget. — proof:
  `server/tests/test_divergence_judge.py::TestBrokerFailures::test_the_budget_counts_failed_calls`
- A judge obeying an instruction injected into the evidence still cannot
  produce a pass. — proof:
  `server/tests/test_divergence_judge.py::TestAdversarialInput::test_a_judge_that_obeys_the_injected_instruction_still_cannot_pass`
- Every assessment carries the rubric digest, comparison hash, request
  digest and model it came from. — proof:
  `server/tests/test_divergence_judge.py::TestProvenance::test_a_verdict_carries_the_rubric_and_evidence_that_produced_it`

See [comparison.md](comparison.md).

## Suites

- A suite file is data: compiling it executes nothing and resolves no remote
  reference. — proof:
  `server/tests/test_suites.py::test_no_execution_or_remote_resolution`
- Malformed or unsafe YAML is rejected rather than partially interpreted. —
  proof: `server/tests/test_suites.py::test_malformed_and_unsafe_yaml`
- Compilation is deterministic for a pinned corpus. — proof:
  `server/tests/test_suites.py::test_compile_deterministic_canonical_snapshot`

See [suites.md](suites.md).

## Audit, deletion and privacy

- An audit row carries identifiers, counts and a closed-vocabulary reason;
  free text is refused. — proof:
  `server/tests/test_audit.py::TestRejectedRows::test_a_free_text_reason_is_refused`
- A prompt cannot be smuggled into the trail as an identifier. — proof:
  `server/tests/test_audit.py::TestRejectedRows::test_a_prompt_cannot_be_smuggled_in_as_an_identifier`
- A rejected audit row writes nothing. — proof:
  `server/tests/test_audit.py::TestRejectedRows::test_a_rejected_row_writes_nothing`
- A refusal never quotes what it refused. — proof:
  `server/tests/test_audit.py::TestErrorsDoNotLeak::test_a_refusal_never_quotes_what_it_refused`

See [audit-and-deletion.md](audit-and-deletion.md).

## Dashboard

- The dashboard never holds a key beyond the session and cannot leak one
  project's scope into another key's view. — proof:
  `web/test/browser/shell.spec.ts::session is memory-only and old project scope cannot leak to a new key`
- Empty, malformed or inaccessible data is shown as such and never claimed
  as complete evidence. — proof:
  `web/test/browser/shell.spec.ts::empty, malformed and inaccessible project data never claims complete evidence`
- The dashboard issues no execution requests and has no axe-detectable
  accessibility violations. — proof:
  `web/test/browser/shell.spec.ts::safe text rendering, no execution requests and automated accessibility`

See [dashboard.md](dashboard.md).

## Not guaranteed

These are the honest gaps. They are listed because a reader who assumes the
opposite will be wrong in a way that matters.

- **Isolation.** Re-execution has no sandboxed runner. The replay package is
  capability-free, which is a strong property for the replay path itself,
  but there is no OS-level isolation boundary around an agent under test.
- **Live models.** There is no model broker, so no budget, no provider
  credential handling, and no live-model re-execution.
- **Judges.** A schema-constrained judge exists and is proven against
  malformed, refused and adversarial responses, but nothing calls it: no
  broker is implemented, and the comparison job does not run one, so a
  stored comparison is still `not_run` in practice.
- **End-to-end runs.** Components are tested against contract fixtures. A
  full agent-to-dashboard run against the real server is not yet covered by
  an automated test.
- **Deployment.** There is no packaged installation, no backup/restore
  procedure and no published benchmark; nothing here is a statement about
  behaviour under production load.
- **Redaction completeness.** The capture policy removes what it recognises.
  It is a reduction in exposure, not a proof that no secret can ever reach
  storage; see [capture-policy.md](capture-policy.md).
