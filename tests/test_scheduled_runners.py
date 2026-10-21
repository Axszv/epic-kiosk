"""The weekly schedule runs the accounts one after another."""


import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/epic-claim.yml"


class ScheduledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_the_accounts_run_one_after_another(self):
        # The accounts share one model gateway key, and the free tier of that
        # gateway takes only a few requests a minute. Accounts that run at the
        # same time answer their captchas into a rate limit, and a week then
        # has one chance only. The claim succeeded on 2026-10-14 with all
        # eight accounts in one job.
        self.assertNotIn("matrix:", self.workflow)
        self.assertNotIn("matrix.account", self.workflow)
        self.assertNotIn("fail-fast: false", self.workflow)

    def test_the_whole_profile_set_is_cached_once(self):
        # Every account keeps its own profile under app/volumes, so the whole
        # directory is what the next run restores.
        self.assertIn("key: epic-state-v2-${{ github.run_id }}-${{ github.run_attempt }}", self.workflow)
        self.assertIn("key: epic-state-v2-${{ github.run_id }}", self.workflow)
        self.assertIn("epic-state-v2-", self.workflow)

    def test_a_manual_run_can_name_one_account(self):
        self.assertIn(
            "EPIC_ACCOUNT_INDEX: ${{ github.event.inputs.account_index || '' }}",
            self.workflow,
        )

    def test_the_concurrency_group_has_no_matrix_context(self):
        # `matrix` only exists inside a job, so the workflow level group would
        # not evaluate.
        start = self.workflow.index("concurrency:")
        end = self.workflow.index("jobs:")
        self.assertNotIn("matrix", self.workflow[start:end])

    def test_a_fresh_profile_keeps_the_session_it_established(self):
        # A fresh profile is the only shape that Epic accepts a login with, so
        # the session it establishes has to be copied into the cached profile.
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts/github_actions_claim_once.py"
        ).read_text(encoding="utf-8")
        self.assertIn("BROWSER_PROFILE_MODE:session_cached", script)
        self.assertIn('"app", "volumes", "user_data", email', script)
        self.assertIn("if fresh_profile and not failed:", script)


if __name__ == "__main__":
    unittest.main()
