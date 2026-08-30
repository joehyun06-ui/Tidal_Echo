from __future__ import annotations

import unittest

from backend.codex_canary_loop_integration import _authority_status


class P3D3TombstoneStatusTests(unittest.TestCase):
    def test_deleted_web_session_is_gone_not_service_unavailable(self):
        self.assertEqual(_authority_status("web_session_deleted"), 410)

    def test_existing_authority_status_contract_is_preserved(self):
        self.assertEqual(_authority_status("web_session_not_found"), 404)
        self.assertEqual(_authority_status("web_session_provider_immutable"), 409)
        self.assertEqual(_authority_status("web_session_patch_invalid"), 400)
        self.assertEqual(_authority_status("web_session_provider_authority_unavailable"), 503)


if __name__ == "__main__":
    unittest.main()
