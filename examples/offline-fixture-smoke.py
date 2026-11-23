"""Provider-free baseline and deliberate-regression demonstration."""

import sys

from anvil_server.replay import Fixture, FixtureSession, ReplayError


REQUEST = {"model": "recorded", "messages": [{"role": "user", "content": "baseline"}],
           "tools": [], "system_instruction": "", "generation": {}}


def verify(*, changed: bool) -> bool:
    fixture = Fixture.create(kind="model", path=("root",), ordinal=0,
                             request=REQUEST, response={"status": "completed",
                                                        "response": "approved", "input_tokens": 1,
                                                        "output_tokens": 1})
    session = FixtureSession([fixture])
    request = {**REQUEST, "messages": [{"role": "user", "content": "regression"}]} if changed else REQUEST
    try:
        session.resolve(request_id="model-1", kind="model", path=("root",), ordinal=0,
                        request=request)
        return session.finish()["provider_calls"] == 0
    except ReplayError:
        return False
    finally:
        session.close()


if __name__ == "__main__":
    mode = sys.argv[1:] or ["baseline"]
    if mode not in (["baseline"], ["regression"]):
        raise SystemExit(2)
    raise SystemExit(0 if verify(changed=mode == ["regression"]) else 1)
