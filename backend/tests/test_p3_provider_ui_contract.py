from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "web" / "index.html"


class P3ProviderUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INDEX.read_text(encoding="utf-8")

    def test_capabilities_are_backend_owned_and_fail_safe_to_api(self):
        text = self.text
        self.assertEqual(text.count("/app/provider/capabilities"), 1)
        self.assertIn('default_provider:"api"', text)
        self.assertIn('provider_immutable:true', text)
        self.assertIn('codex:{ create:false, text_only:true }', text)
        self.assertIn('d.contract_version !== 1', text)
        self.assertIn('providers.codex.text_only !== true', text)
        self.assertIn('resetProviderCapabilities();', text)

    def test_new_session_provider_is_explicit_and_server_response_is_verified(self):
        text = self.text
        self.assertIn('const provider = chooseNewSessionProvider();', text)
        self.assertIn('if (!providerCanCreate(provider))', text)
        self.assertIn('activate:true, provider', text)
        self.assertIn('d.created.provider !== provider', text)
        self.assertIn('provider === "codex" ? "Codex 对话" : "新对话"', text)
        self.assertNotIn('<div class="session-title">API 窗口</div>', text)

    def test_existing_session_provider_is_projection_not_ui_inference(self):
        text = self.text
        self.assertIn('s.provider === "codex" ? "Codex" : "API"', text)
        self.assertIn('row && row.provider === "codex" ? "codex" : "api"', text)
        self.assertNotIn('title.includes("Codex")', text)
        self.assertNotIn('title === "Codex canary"', text)
        self.assertNotRegex(text, r'JSON\.stringify\(\{[^}]*provider\s*:\s*"(?:api|codex)"[^}]*\}\)')

    def test_codex_stage_is_text_only_before_upload_and_at_send_boundary(self):
        text = self.text
        self.assertIn('currentSessionTextOnly()', text)
        self.assertIn('Codex 窗口当前仅支持文本', text)
        self.assertIn('throw new Error("codex_text_only")', text)
        send_file = text.index('async function sendOneFile(file)')
        upload = text.index('const up = await apiUpload(', send_file)
        guard = text.index('if (currentSessionTextOnly())', send_file)
        self.assertLess(guard, upload)

    def test_inline_javascript_is_syntax_valid_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", self.text, flags=re.S | re.I)
        self.assertTrue(scripts)
        source = "\n".join(scripts)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(source)
            path = Path(handle.name)
        try:
            completed = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
