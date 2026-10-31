import unittest

from tenant_lifecycle import models


class TransitionTableTest(unittest.TestCase):
    def test_every_target_is_a_known_state(self):
        for targets in models.TRANSITIONS.values():
            self.assertLessEqual(targets, set(models.STATES))

    def test_terminal_states_are_the_three_allowed_end_states(self):
        self.assertEqual(models.TERMINAL_STATES,
                         {models.ACTIVE, models.ESCALATED, models.CLEANED_UP})

    def test_every_non_terminal_state_can_reach_a_terminal_state(self):
        for start in models.STATES:
            seen, frontier = {start}, [start]
            while frontier:
                for nxt in models.TRANSITIONS[frontier.pop()]:
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append(nxt)
            self.assertTrue(seen & models.TERMINAL_STATES, start)

    def test_legal_and_illegal_examples(self):
        self.assertTrue(models.is_legal_transition(models.PENDING, models.PROVISIONING))
        self.assertTrue(models.is_legal_transition(models.STEP_FAILED, models.PROVISIONING))
        self.assertFalse(models.is_legal_transition(models.PENDING, models.ACTIVE))
        self.assertFalse(models.is_legal_transition(models.PROVISIONING, models.ESCALATED))
        self.assertFalse(models.is_legal_transition(models.ACTIVE, models.COMPENSATING))
        self.assertFalse(models.is_legal_transition("unknown", models.ACTIVE))

    def test_escalation_only_after_compensation(self):
        sources = {s for s, nxt in models.TRANSITIONS.items() if models.ESCALATED in nxt}
        self.assertEqual(sources, {models.COMPENSATING})


class IdentityTest(unittest.TestCase):
    def test_env_id_is_deterministic_and_distinct(self):
        self.assertEqual(models.env_id_for("req-1"), models.env_id_for("req-1"))
        self.assertNotEqual(models.env_id_for("req-1"), models.env_id_for("req-2"))

    def test_database_kind_and_parent_depend_on_tier(self):
        self.assertEqual(models.resource_kind("database", "basic"), "logical_db")
        self.assertEqual(models.resource_parent("database", "basic", "eu-west-1"),
                         "shared-cluster-eu-west-1")
        self.assertEqual(models.resource_kind("database", "enterprise"), "dedicated_db")
        self.assertIsNone(models.resource_parent("database", "enterprise", "eu-west-1"))
        self.assertIsNone(models.resource_parent("dns", "basic", "eu-west-1"))

    def test_shared_cluster_is_region_specific(self):
        self.assertNotEqual(models.shared_cluster("eu-west-1"), models.shared_cluster("us-east-1"))
        self.assertEqual(models.resource_parent("database", "basic", "us-east-1"),
                         models.shared_cluster("us-east-1"))

    def test_payload_hash_ignores_key_order(self):
        self.assertEqual(models.payload_hash({"a": 1, "b": 2}),
                         models.payload_hash({"b": 2, "a": 1}))
        self.assertNotEqual(models.payload_hash({"a": 1}), models.payload_hash({"a": 2}))


if __name__ == "__main__":
    unittest.main()
