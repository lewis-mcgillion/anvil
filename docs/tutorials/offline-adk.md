# Offline Google ADK capture walkthrough

Requires Python 3.12 and the pinned `google-adk==2.13.0` test environment.
This example uses an in-memory ADK Runner and fake model, not a production
provider or tool. From a fresh checkout, install the adapter dependencies
listed in `adapters/google-adk/requirements.lock`, then run:

```sh
PYTHONPATH=adapters/google-adk/src python -m pytest -q \
  adapters/google-adk/tests/test_integration.py \
  -k real_capture_preserves_outcome_and_payload_schema
```

The test compares instrumented and uninstrumented outcomes and validates each
emitted payload against the frozen Anvil v1 schema. The capture plugin is
passive: the caller must redact and transport captured envelopes. Always
close the ADK Runner in a `finally` block; cancellation releases pending
model/tool state. A missing or ambiguous replay fixture is fatal and must
never execute the original tool or contact a model provider.

For a separate, entirely offline demonstration of read-only replay, install
the Python server package and inspect the bundled synthetic recording:

```sh
PYTHONPATH=server python -m anvil_server.cli playback projection \
  --recording fixtures/conformance/v1/retries.json
PYTHONPATH=server python -m anvil_server.cli playback verify \
  --recording fixtures/conformance/v1/retries.json
```

Both commands read local evidence and do not connect to a provider, execute
agent code or invoke production tools. The bundled recording is not the output
of the ADK capture command above; importing that capture into a durable server
requires a separate authenticated ingestion workflow.

Replay with current agent code, registered OCI images and brokered live
inference are not available until the isolated runner and orchestration
issues (#45, #48, #50) are implemented. This tutorial intentionally does
not claim a full capture/import/re-execution deployment path.
