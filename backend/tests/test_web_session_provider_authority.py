from __future__ import annotations

import unittest

from backend.web_session_provider_authority import (
    API_PROVIDER,
    CODEX_PROVIDER,
    WebSessionProviderAuthority,
    WebSessionProviderAuthorityError,
)


class FakeLegacy:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.saved = []
        self.counter = 0

    def load_config(self):
        cfg = dict(self.cfg)
        if "sessions" in cfg:
            cfg["sessions"] = [dict(row) for row in cfg["sessions"]]
        return cfg

    def save_config(self, cfg):
        self.cfg = dict(cfg)
        if "sessions" in self.cfg:
            self.cfg["sessions"] = [dict(row) for row in self.cfg["sessions"]]
        self.saved.append(self.load_config())

    def now_iso(self):
        self.counter += 1
        return f"2026-08-29T00:00:{self.counter:02d}+00:00"


class WebSessionProviderAuthorityTest(unittest.TestCase):
    def test_pre_p3_rows_default_to_api_without_title_or_pin_inference(self):
        legacy = FakeLegacy({
            "sessions": [{
                "id": "api-old",
                "title": "Codex canary",
                "since_id": 3,
                "created_at": "2026-08-28T00:00:00+00:00",
                "pinned": True,
            }]
        })
        authority = WebSessionProviderAuthority(legacy)
        row = authority.session_rows()[0]
        self.assertEqual(row["provider"], API_PROVIDER)
        self.assertTrue(row["pinned"])
        self.assertEqual(authority.provider_for_session("api-old"), API_PROVIDER)

    def test_pre_p3_row_with_durable_codex_history_bootstraps_to_codex(self):
        legacy = FakeLegacy({
            "sessions": [{
                "id": "api-old-canary",
                "title": "Anything",
                "since_id": 0,
                "created_at": "2026-08-28T00:00:00+00:00",
            }]
        })
        authority = WebSessionProviderAuthority(
            legacy,
            historical_provider=lambda sid: CODEX_PROVIDER if sid == "api-old-canary" else None,
        )
        self.assertEqual(
            authority.provider_for_session("api-old-canary"),
            CODEX_PROVIDER,
        )
        self.assertEqual(authority.session_rows()[0]["provider"], CODEX_PROVIDER)

    def test_explicit_provider_wins_and_is_not_overwritten_by_history(self):
        legacy = FakeLegacy({
            "sessions": [{
                "id": "api-explicit",
                "title": "API",
                "since_id": 0,
                "created_at": "",
                "provider": API_PROVIDER,
            }]
        })
        authority = WebSessionProviderAuthority(
            legacy,
            historical_provider=lambda _sid: CODEX_PROVIDER,
        )
        self.assertEqual(authority.provider_for_session("api-explicit"), API_PROVIDER)

    def test_explicit_codex_provider_is_durable_across_reopen(self):
        legacy = FakeLegacy()
        authority = WebSessionProviderAuthority(legacy)
        row = authority.new_row(
            title="Codex chat",
            provider=CODEX_PROVIDER,
            session_id="api-codex",
        )
        authority.publish_row(row, activate=True)
        reopened = WebSessionProviderAuthority(legacy)
        self.assertEqual(reopened.provider_for_session("api-codex"), CODEX_PROVIDER)
        self.assertEqual(reopened.active_session_id(), "api-codex")
        self.assertEqual(reopened.session_rows()[0]["provider"], CODEX_PROVIDER)

    def test_unknown_non_web_session_remains_api(self):
        authority = WebSessionProviderAuthority(FakeLegacy())
        self.assertEqual(authority.provider_for_session("telegram-conversation"), API_PROVIDER)
        self.assertEqual(authority.provider_for_session(""), API_PROVIDER)

    def test_provider_is_immutable(self):
        legacy = FakeLegacy()
        authority = WebSessionProviderAuthority(legacy)
        row = authority.create_api_session(title="API", activate=True)
        with self.assertRaisesRegex(
            WebSessionProviderAuthorityError, "web_session_provider_immutable"
        ):
            authority.patch_session(row["id"], {"provider": CODEX_PROVIDER})
        self.assertEqual(authority.provider_for_session(row["id"]), API_PROVIDER)

    def test_invalid_persisted_provider_fails_closed(self):
        authority = WebSessionProviderAuthority(FakeLegacy({
            "sessions": [{
                "id": "api-bad",
                "title": "bad",
                "since_id": 0,
                "created_at": "",
                "provider": "title-derived-codex",
            }]
        }))
        with self.assertRaisesRegex(
            WebSessionProviderAuthorityError, "web_session_provider_invalid"
        ):
            authority.session_rows()

    def test_creation_order_is_preserved_for_wake_selector_compatibility(self):
        legacy = FakeLegacy()
        authority = WebSessionProviderAuthority(legacy)
        first = authority.create_api_session(title="first", activate=False)
        second = authority.new_row(
            title="second",
            provider=CODEX_PROVIDER,
            session_id="api-second",
        )
        authority.publish_row(second, activate=False)
        self.assertEqual(
            [row["id"] for row in authority.session_rows()],
            [first["id"], "api-second"],
        )

    def test_patch_preserves_provider_and_only_changes_ui_metadata(self):
        legacy = FakeLegacy()
        authority = WebSessionProviderAuthority(legacy)
        row = authority.new_row(
            title="before",
            provider=CODEX_PROVIDER,
            session_id="api-codex",
        )
        authority.publish_row(row, activate=False)
        public = authority.patch_session(
            "api-codex",
            {"title": "after", "pinned": True, "active": True},
        )
        updated = public["sessions"][0]
        self.assertEqual(updated["title"], "after")
        self.assertTrue(updated["pinned"])
        self.assertEqual(updated["provider"], CODEX_PROVIDER)
        self.assertEqual(public["active_session"], "api-codex")


if __name__ == "__main__":
    unittest.main()
