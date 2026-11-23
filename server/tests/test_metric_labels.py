import pytest

from anvil_server.metric_labels import metric_counter


def test_metrics_have_fixed_nonsecret_label_sets():
    assert metric_counter(phase="ingest", outcome="failure", count=2) == (
        'anvil_operations_total{phase="ingest",outcome="failure"} 2\n')


@pytest.mark.parametrize("field,value", [
    ("phase", 'ingest",token="secret'), ("outcome", "user-123"), ("count", -1),
])
def test_unbounded_metrics_labels_fail_closed(field, value):
    values = {"phase": "queue", "outcome": "success", "count": 1}
    values[field] = value
    with pytest.raises(ValueError):
        metric_counter(**values)
