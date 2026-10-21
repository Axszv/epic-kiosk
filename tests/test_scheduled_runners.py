"""The weekly schedule gives every account its own runner."""

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/epic-claim.yml"


class ScheduledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_every_account_runs_on_its_own_runner(self):
        # A runner address that Epic has flagged would otherwise refuse every
        # account of that week.
        self.assertIn("matrix:", self.workflow)
        self.assertIn("account: [1, 2, 3, 4, 5, 6, 7, 8]", self.workflow)
        self.assertIn("fail-fast: false", self.workflow)

    def test_the_matrix_account_reaches_the_claim(self):
        self.assertIn(
            "EPIC_ACCOUNT_INDEX: ${{ github.event.inputs.account_index || matrix.account }}",
            self.workflow,
        )

    def test_every_account_keeps_its_own_browser_profile(self):
        # The cached profile holds the account session, so two accounts must
        # never share one cache entry.
        self.assertIn(
            "key: epic-state-v2-${{ github.run_id }}-${{ matrix.account }}-${{ github.run_attempt }}",
            self.workflow,
        )
        self.assertIn(
            "key: epic-state-v2-${{ github.run_id }}-${{ matrix.account }}", self.workflow
        )
        self.assertIn("epic-state-v2-${{ matrix.account }}-", self.workflow)

    def test_the_concurrency_group_has_no_matrix_context(self):
        # `matrix` only exists inside a job, so the workflow level group would
        # not evaluate.
        start = self.workflow.index("concurrency:")
        end = self.workflow.index("jobs:")
        self.assertNotIn("matrix", self.workflow[start:end])


if __name__ == "__main__":
    unittest.main()
