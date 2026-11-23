# Offline suite and fault testing cookbook

These examples deliberately use immutable recorded fixtures. They cannot
trigger production model or tool execution. From a clean Python 3.12 virtual
environment with `server[test]` installed, run:

```sh
PYTHONPATH=server python -m pytest -q server/tests/test_suites.py \
  server/tests/test_suite_assertions.py server/tests/test_fault_engine.py \
  server/tests/test_replay_fixtures.py
```

Scenarios exercised by these tests:

- Changed model prompt or tool arguments: strict fixture mismatch aborts
  rather than falling back to a provider.
- Missing or duplicate fixture: result is explicit error/inconclusive, never
  a passing assertion.
- Simulated tool timeout: injected logical delay and failure are recorded
  without waiting or contacting original tools.
- Truncated stream and prompt injection: unsafe content remains input data;
  payload mutation is bounded and replay-ineligible where appropriate.
- Judge uncertainty: optional semantic judgment must never replace the
  deterministic comparison verdict.

`docs/suites.md` defines the YAML schema, source paths, strict selectors
and supported assertions. `docs/faults.md` defines typed fault plans and
evidence. Cancellation and exceptions leave an explicit terminal outcome;
fixture sessions must be closed and discarded, never reused to claim a pass.

Full `anvil test run` workflows and CI gate/report examples remain dependent
on #69/#70/#71. The current parser/assertion examples validate without
executing current agent code, so they are not substitutes for a real suite run.
