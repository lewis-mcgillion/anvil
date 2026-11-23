import unittest

from anvil.otel import map_span


class OTelTests(unittest.TestCase):
    def test_nested_spans_preserve_correlation_without_replay_claim(self):
        trace_id = "a" * 32
        parent = map_span({"trace_id": trace_id, "span_id": "1" * 16, "name": "agent"})
        child = map_span({"context": {"trace_id": trace_id, "span_id": "2" * 16},
                          "parent": {"span_id": parent["span_id"]}, "name": "model",
                          "attributes": {"gen_ai.operation.name": "chat", "gen_ai.system": "test"}})
        self.assertEqual(child["trace_id"], parent["trace_id"])
        self.assertEqual(child["parent_span_id"], parent["span_id"])
        self.assertEqual(child["operation"], "chat")
        self.assertEqual(child["system"], "test")
        self.assertFalse(child["replay_safe"])
        self.assertEqual(child["replay_eligibility"], "ineligible")

    def test_malformed_span_ids_fail_closed(self):
        with self.assertRaises(ValueError):
            map_span({"trace_id": "not-a-trace", "span_id": "1" * 16})
