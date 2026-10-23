import pytest

from anvil_server.sealing import IncompleteCapture, seal


def test_complete_capture_is_eligible_only_when_replay_safe():
    events = [{"event_id": "one", "stream_id": "main", "sequence": 0, "type": "run.completed", "replay_safe": True}]
    result = seal("run", "project", events, [{"stream_id": "main", "terminal": True}], [])
    assert result["replay_eligibility"] == "eligible"
    assert seal("run", "project", [{**events[0], "replay_safe": False}], [{"stream_id": "main", "terminal": True}], [])["replay_eligibility"] == "ineligible"


def test_missing_terminal_and_cycle_fail_closed():
    events = [{"event_id": "one", "stream_id": "main", "sequence": 0,
               "type": "run.completed", "parent_event_ids": ["one"], "replay_safe": True}]
    with pytest.raises(IncompleteCapture):
        seal("run", "project", events, [{"stream_id": "main", "terminal": False}], [])
    assert seal("run", "project", events, [{"stream_id": "main", "terminal": True}], [], True)["capture_completeness"] == "sealed_incomplete"


def test_missing_evidence_requires_acknowledgement():
    with pytest.raises(IncompleteCapture):
        seal("run", "project", [], [], [])


def test_sequence_must_begin_at_zero_and_gap_event_is_incomplete():
    terminal = [{"stream_id": "main", "terminal": True}]
    late = [{"event_id": "late", "stream_id": "main", "sequence": 1,
             "type": "run.completed", "replay_safe": True}]
    with pytest.raises(IncompleteCapture):
        seal("run", "project", late, terminal, [])
    assert seal("run", "project", late, terminal, [], True)["replay_eligibility"] == "ineligible"
    gap = [{"event_id": "gap", "stream_id": "main", "sequence": 0,
            "type": "capture.gap", "replay_safe": True}]
    with pytest.raises(IncompleteCapture):
        seal("run", "project", gap, terminal, [])
