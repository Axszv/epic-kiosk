"""The captcha gateway is chosen as one provider, never field by field.

Agnes and Gemini are separate providers: Agnes serves `agnes-3.0-flash` at
`https://apihub.agnes-ai.com/v1` (OpenAI-compatible, `Bearer`), and the
Gemini-compatible gateway serves `gemini-3.5-flash-lite` under `v1beta/openai`.

Falling back one field at a time — `CAPTCHA_API_BASE_URL || API_BASE_URL` next
to a single global `API_VERSION` — lets a run take the base from one provider
and the version from the other. On 2026-10-30 that asked for

    POST /v1/v1beta/openai/chat/completions

and every captcha call answered 404. The provider now decides base, key,
version and model together, in one step.
"""

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "epic-claim.yml"


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.claim = self.doc["jobs"]["claim"]
        self.env = self.claim["env"]
        self.step = next(
            step
            for step in self.claim["steps"]
            if step.get("name") == "Select the captcha provider"
        )

    def test_the_job_does_not_choose_gateway_fields_one_by_one(self):
        # Each of these used to be a separate `||` fallback.
        for key in ("API_BASE_URL", "API_KEY", "API_VERSION", "CAPTCHA_MODEL"):
            self.assertNotIn(
                key,
                self.env,
                f"{key} must come from the provider step, not from a fallback",
            )

    def test_the_provider_is_the_single_switch(self):
        self.assertIn("CAPTCHA_PROVIDER", self.env)

    def test_every_field_of_a_provider_is_set_together(self):
        script = self.step["run"]

        for key in (
            "API_BASE_URL=$base",
            "API_KEY=$key",
            "API_VERSION=$version",
            "CAPTCHA_MODEL=$model",
            "CAPTCHA_MODEL_FALLBACK=$fallback",
        ):
            self.assertIn(key, script)

        # One version per provider, and each provider gets its own.
        self.assertIn('version="v1"', script)
        self.assertIn('version="v1beta/openai"', script)

    def test_a_provider_without_credentials_stops_the_run(self):
        # A missing key used to fall through to the other provider's gateway
        # and fail later with a 404 nobody could read.
        self.assertIn("has no base URL or key", self.step["run"])
        self.assertIn("exit 1", self.step["run"])


if __name__ == "__main__":
    unittest.main()
