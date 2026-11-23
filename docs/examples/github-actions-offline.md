# Offline regression GitHub Actions template

The current Python `anvil` CLI supports read-only playback, not `anvil test
run`. The workflow below is intentionally a **preflight example**, not a
complete regression executor. It runs schema and fixture checks without
provider credentials, runner networking or secret injection.

```yaml
name: anvil-offline-preflight
on: [pull_request]
permissions:
  contents: read
jobs:
  validate:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: python -m pip install -e './server[test]'
      - run: python -m pytest -q server/tests/test_replay_fixtures.py server/tests/test_suite_assertions.py
      - run: PYTHONPATH=server python examples/offline-fixture-smoke.py baseline
      - run: |
          if PYTHONPATH=server python examples/offline-fixture-smoke.py regression; then
            echo 'Deliberate fixture regression unexpectedly passed' >&2
            exit 1
          fi
```

For a consuming repository, pin the Anvil source revision and third-party
action versions by immutable SHA, and replace repository-internal test paths
with its own recorded fixture and suite inputs. No production model API key
is needed for recorded fixture tests. Do not claim that this template has
executed the current agent version: isolated suite jobs, JUnit reports and
PR summaries require ANVIL-058/059 and a verified sandbox. A deliberate
fixture mismatch should fail the offline matcher, never fall back to a
production tool or provider.
