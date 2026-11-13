"""Setup: .env import (aliases, service-account file, values never returned) and the MCP setup tools."""
import os as _os, tempfile as _tf
_os.environ.setdefault("MSTUDIO_HOME", _tf.mkdtemp(prefix="mstudio-test-"))
_os.environ["MSTUDIO_KEYCHAIN"] = "0"

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import appconfig  # noqa: E402


class EnvImport(unittest.TestCase):
    def test_import_aliases_and_service_account(self):
        d = Path(tempfile.mkdtemp())
        (d / "sa.json").write_text(json.dumps({"type": "service_account", "client_email": "x@y", "private_key": "k"}))
        env = d / "keys.env"
        env.write_text("# comment\nexport FB_ACCESS_TOKEN='tok-abc'\nMETA_AD_ACCOUNT_ID=act_1\nGOOGLE_SERVICE_ACCOUNT_FILE=sa.json\nSOMETHING_ELSE=1\nEMPTY=\n")
        r = appconfig.import_env_file(str(env))
        self.assertEqual(r["imported"], ["GOOGLE_SA_JSON", "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID"])
        self.assertEqual(r["ignored"], ["SOMETHING_ELSE"])
        self.assertNotIn("tok-abc", json.dumps(r))
        self.assertEqual(appconfig.get("META_ACCESS_TOKEN"), "tok-abc")
        self.assertTrue(appconfig.status()["google_service_account"])
        st = json.dumps(appconfig.status())
        self.assertNotIn("tok-abc", st)
        cfg = json.loads(appconfig.CONFIG.read_text())
        self.assertNotIn("META_ACCESS_TOKEN", cfg)  # secrets never in config.json

    def test_mcp_setup_tools(self):
        env = Path(tempfile.mkdtemp()) / ".env"
        env.write_text("GADS_DEVELOPER_TOKEN=dev-xyz\nGADS_LOGIN_CUSTOMER_ID=123\n")
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "setup_import_env_file", "arguments": {"path": str(env)}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "setup_status", "arguments": {}}}]
        p = subprocess.run([sys.executable, str(HERE / "mcp_server.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, timeout=120, env=dict(_os.environ))
        out = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "marketing-studio")
        txt = out[1]["result"]["content"][0]["text"] + out[2]["result"]["content"][0]["text"]
        self.assertNotIn("dev-xyz", txt)
        self.assertIn("GADS_DEVELOPER_TOKEN", txt)
        self.assertIn('"GADS_DEVELOPER_TOKEN": true', out[2]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
