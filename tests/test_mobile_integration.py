import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

# The production workflow installs requests. Keep these pure unit tests runnable
# in minimal Python environments by supplying the fake session used below.
if importlib.util.find_spec("requests") is None:
    sys.modules["requests"] = types.SimpleNamespace()

import github_actions_claim_once as claim_once
import mobile_offer_discovery as discovery
import serverchan_notify


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append((url, params, headers, timeout))
        return FakeResponse(self.payloads[params["platform"]])


def offer(title, slug, *, end_date=None, categories=None, purchases=None):
    if purchases is None:
        purchases = [
            {
                "purchaseType": "Claim",
                "purchasePayload": {
                    "offerId": f"offer-{slug}",
                    "sandboxId": f"sandbox-{slug}",
                },
                "discount": {"discountEndDate": end_date},
            }
        ]
    return {
        "content": {
            "title": title,
            "mapping": {"slug": slug},
            "categories": categories or ["freegames"],
            "purchase": purchases,
        }
    }


class MobileDiscoveryTests(unittest.TestCase):
    def test_discovers_each_platform_and_filters_non_weekly_offers(self):
        future = "2026-07-20T00:00:00Z"
        past = "2026-07-01T00:00:00Z"
        payload = {
            "data": [
                {
                    "type": "freeGame",
                    "topicId": "mobile-android-free-game",
                    "offers": [
                        offer("Android Weekly", "android-weekly", end_date=future),
                        offer("Expired", "expired", end_date=past),
                        offer(
                            "Permanent Free",
                            "permanent",
                            end_date=None,
                        ),
                    ],
                }
            ]
        }
        ios_payload = {
            "data": [
                {
                    "type": "freeGame",
                    "topicId": "mobile-ios-free-game",
                    "offers": [offer("iOS Weekly", "ios-weekly", end_date=future)],
                }
            ]
        }
        fake = FakeSession({"android": payload, "ios": ios_payload})

        result = discovery.discover_mobile_offers(
            session=fake,
            now=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [(item["platform"], item["title"]) for item in result["offers"]],
            [("Android", "Android Weekly"), ("iOS", "iOS Weekly")],
        )
        self.assertEqual(len(fake.calls), 2)


class MobileEvidenceTests(unittest.TestCase):
    def test_parses_platform_results_and_titles_with_colons(self):
        output = "\n".join(
            [
                "MOBILE_RESULT:Android:Game: Deluxe Edition:verified_owned",
                "MOBILE_RESULT:iOS:Second Game:region_unavailable",
            ]
        )

        evidence = claim_once.parse_mobile_evidence(output)

        self.assertEqual(
            evidence["Android"]["Game: Deluxe Edition"]["status"],
            "verified_owned",
        )
        self.assertEqual(
            evidence["iOS"]["Second Game"]["status"],
            "region_unavailable",
        )

    def test_unverified_checkout_is_missing_strict_evidence(self):
        offers = [{"platform": "Android", "title": "Weekly Game"}]
        evidence = {
            "Android": {
                "Weekly Game": {"status": "checkout_submitted_unverified"}
            }
        }

        self.assertEqual(
            claim_once.missing_mobile_evidence(offers, evidence),
            ["Android:Weekly Game"],
        )


class DesktopEvidenceTests(unittest.TestCase):
    def test_parses_verified_result_for_title_with_colon(self):
        output = "\n".join(
            [
                '发现: {"title": "Echo Generation: Midnight Edition", "url": "https://example.com"}',
                "DESKTOP_RESULT:Echo Generation: Midnight Edition:verified_owned",
            ]
        )

        evidence = claim_once.parse_claim_evidence(output)

        self.assertEqual(
            evidence["Echo Generation: Midnight Edition"]["status"],
            "verified_owned",
        )

    def test_success_text_without_machine_marker_is_not_evidence(self):
        output = "\n".join(
            [
                '发现: {"title": "Weekly Game", "url": "https://example.com"}',
                "商品页确认已入库: Weekly Game",
            ]
        )

        evidence = claim_once.parse_claim_evidence(output)

        self.assertEqual(evidence["Weekly Game"]["status"], "unknown")


class ProfileRefreshTests(unittest.TestCase):
    def test_preserves_profile_for_cloudflare_block(self):
        result = {
            "error_markers": [
                {"kind": "ERROR_TYPE", "value": "cloudflare_blocked"}
            ]
        }

        self.assertFalse(claim_once.should_refresh_account_profile(result))

    def test_refreshes_profile_for_invalid_cookie(self):
        result = {
            "error_markers": [{"kind": "ERROR_TYPE", "value": "cookie_invalid"}]
        }

        self.assertTrue(claim_once.should_refresh_account_profile(result))

    def test_backs_off_after_transient_login_failures(self):
        cloudflare = {
            "error_markers": [
                {"kind": "ERROR_TYPE", "value": "cloudflare_blocked"}
            ]
        }
        captcha = {
            "error_markers": [{"kind": "ERROR_TYPE", "value": "captcha_failed"}]
        }

        self.assertEqual(claim_once.account_retry_delay(cloudflare), 45)
        self.assertEqual(claim_once.account_retry_delay(captcha), 20)
        self.assertEqual(claim_once.account_retry_delay({"error_markers": []}), 0)

    def test_does_not_refresh_profile_for_claim_or_captcha_failure(self):
        for value in ("captcha_failed", "login_timeout", "unknown"):
            result = {"error_markers": [{"kind": "ERROR_TYPE", "value": value}]}
            self.assertFalse(claim_once.should_refresh_account_profile(result))

    def test_refresh_removes_only_the_selected_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "account1@example.com"
            other = root / "account2@example.com"
            selected.mkdir()
            other.mkdir()
            (selected / "cookies.sqlite").write_text("stale", encoding="utf-8")
            (other / "cookies.sqlite").write_text("valid", encoding="utf-8")

            refreshed = claim_once.refresh_account_profile(
                "account1@example.com", user_data_root=root
            )

            self.assertTrue(refreshed)
            self.assertFalse(selected.exists())
            self.assertTrue(other.exists())


class CumulativeEvidenceTests(unittest.TestCase):
    def test_keeps_strict_desktop_evidence_when_later_attempt_is_unknown(self):
        current = {
            "Desktop Weekly": {
                "status": "verified_owned",
                "evidence": "strict proof",
            }
        }
        incoming = {
            "Desktop Weekly": {"status": "unknown", "evidence": ""}
        }

        merged = claim_once.merge_desktop_evidence(current, incoming)

        self.assertEqual(merged["Desktop Weekly"]["status"], "verified_owned")

    def test_completes_when_mobile_proof_is_split_across_attempts(self):
        offers = [
            {"platform": "Android", "title": "Mobile Weekly"},
            {"platform": "iOS", "title": "Mobile Weekly"},
        ]
        first = {
            "Android": {
                "Mobile Weekly": {"status": "verified_owned", "evidence": "first"}
            }
        }
        second = {
            "iOS": {
                "Mobile Weekly": {"status": "already_owned", "evidence": "second"}
            }
        }
        merged = claim_once.merge_mobile_evidence(first, second)

        self.assertTrue(
            claim_once.has_complete_account_evidence(
                {"Desktop Weekly": {"status": "already_owned"}},
                merged,
                offers,
            )
        )

    def test_unverified_checkout_never_completes_account(self):
        offers = [{"platform": "Android", "title": "Mobile Weekly"}]

        self.assertFalse(
            claim_once.has_complete_account_evidence(
                {"Desktop Weekly": {"status": "verified_owned"}},
                {
                    "Android": {
                        "Mobile Weekly": {
                            "status": "checkout_submitted_unverified"
                        }
                    }
                },
                offers,
            )
        )

    def test_previous_desktop_proof_does_not_hide_empty_current_attempt(self):
        offers = [{"platform": "Android", "title": "Mobile Weekly"}]
        accumulated_desktop = {
            "Desktop Weekly": {"status": "verified_owned"}
        }
        current_desktop = {}
        mobile = {
            "Android": {
                "Mobile Weekly": {"status": "already_owned"}
            }
        }

        self.assertTrue(accumulated_desktop)
        self.assertFalse(
            claim_once.has_complete_account_evidence(
                current_desktop,
                mobile,
                offers,
            )
        )


class NotificationTests(unittest.TestCase):
    def test_notification_separates_desktop_android_and_ios(self):
        summary = {
            "mobile_discovery": {
                "status": "completed",
                "offers": [{"platform": "Android", "title": "Mobile Weekly"}],
            },
            "accounts": [
                {
                    "index": 1,
                    "email": "ac***@example.com",
                    "status": "completed",
                    "attempts": [
                        {
                            "attempt": 1,
                            "failed": True,
                            "failure_reasons": ["temporary_failure"],
                        },
                        {"attempt": 2, "failed": False},
                    ],
                    "desktop_evidence": {
                        "Desktop Weekly": {"status": "verified_owned"}
                    },
                    "mobile_evidence": {
                        "Android": {
                            "Mobile Weekly": {"status": "already_owned"}
                        },
                        "iOS": {
                            "iOS Weekly": {"status": "region_unavailable"}
                        },
                    },
                }
            ],
        }

        title, body = serverchan_notify.build_message(summary)

        self.assertIn("成功：1/1", title)
        self.assertIn("电脑端：", body)
        self.assertIn("Android：", body)
        self.assertIn("iOS：", body)
        self.assertIn("锁区跳过（未领取）", body)
        self.assertIn(
            "电脑端：\n- Desktop Weekly：已入库\n\n"
            "Android：\n- Mobile Weekly：已在库\n\n"
            "iOS：\n- iOS Weekly：锁区跳过（未领取）",
            body,
        )
        self.assertIn(
            "iOS：\n- iOS Weekly：锁区跳过（未领取）\n\n"
            "重试记录：\n- 第 1 次尝试失败：temporary_failure",
            body,
        )


if __name__ == "__main__":
    unittest.main()
