import pytest

from anvil_server.metric_labels import metric_counter, resource_gauge


def test_metrics_have_fixed_nonsecret_label_sets():
    assert metric_counter(phase="ingest", outcome="failure", count=2) == (
        'anvil_operations_total{phase="ingest",outcome="failure"} 2\n')


def test_resource_gauges_reject_dynamic_labels_and_invalid_values():
    assert resource_gauge(resource="events", count=42) == 'anvil_resources{resource="events"} 42\n'
    for resource, count in (("user-secret", 1), ("events", -1), ("events", True),
                            ("events", 1.5), (None, 1)):
        with pytest.raises(ValueError):
            resource_gauge(resource=resource, count=count)


@pytest.mark.parametrize("field,value", [
    ("phase", 'ingest",token="secret'), ("outcome", "user-123"), ("count", -1),
    ("phase", None), ("outcome", None), ("count", True), ("count", 1.5),
])
def test_unbounded_metrics_labels_fail_closed(field, value):
    values = {"phase": "queue", "outcome": "success", "count": 1}
    values[field] = value
    with pytest.raises(ValueError):
        metric_counter(**values)
