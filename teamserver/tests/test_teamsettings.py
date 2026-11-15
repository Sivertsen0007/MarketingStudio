"""Team settings on the real server (teamserver/teamsettings.py): roles, versions, conflicts, SSE, activity, restart.

Run:  python3 teamserver/tests/test_teamsettings.py
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import test_server as base  # noqa: E402  (the shared server fixture; its tests are not re-run here)
from teamserver.app import TeamServer  # noqa: E402
from teamserver import teamsettings  # noqa: E402

RULES = {"rules": [{"id": "r1", "level": "warn", "pattern": "\\bcheap\\b", "message": "say good value", "areas": []}]}


class TeamSettings(base.Base):
    def setUp(self):
        super().setUp()
        self.admin = self.join_new("admin@example.com", "admin", "Ada Admin")
        self.editor = self.join_new("ed@example.com", "editor", "Ed Editor")
        self.viewer = self.join_new("vi@example.com", "viewer", "Vi Viewer")

    def test_everyone_reads_every_key(self):
        for who in (self.owner, self.editor, self.viewer):
            out = self.ok("GET", "/api/v1/settings", token=who["token"])
            self.assertEqual(sorted(out["settings"]), sorted(teamsettings.KEYS))
            self.assertEqual(out["settings"]["copy_rules"], {"key": "copy_rules", "value": None, "version": 0,
                                                             "updated_by": None, "updated_at": None})
        status, _ = self.call("GET", "/api/v1/settings")
        self.assertEqual(status, 401)

    def test_owner_and_admin_write_others_cannot(self):
        out = self.ok("PUT", "/api/v1/settings/copy_rules", {"value": RULES}, self.owner["token"])
        self.assertEqual((out["version"], out["value"], out["updated_by"]), (1, RULES, self.owner["member"]["id"]))
        out = self.ok("PUT", "/api/v1/settings/calendar_limits", {"value": {"max_per_channel_day": 3}}, self.admin["token"])
        self.assertEqual(out["version"], 1)
        for who in (self.editor, self.viewer):
            status, err = self.call("PUT", "/api/v1/settings/copy_rules", {"value": {"rules": []}}, who["token"])
            self.assertEqual(status, 403, err)
        got = self.ok("GET", "/api/v1/settings/copy_rules", token=self.viewer["token"])
        self.assertEqual((got["version"], got["value"]), (1, RULES))

    def test_versions_and_conflicts(self):
        self.ok("PUT", "/api/v1/settings/social", {"value": {"late_minutes": 5}, "base_version": 0}, self.owner["token"])
        out = self.ok("PUT", "/api/v1/settings/social", {"value": {"late_minutes": 10}, "base_version": 1}, self.admin["token"])
        self.assertEqual(out["version"], 2)
        status, err = self.call("PUT", "/api/v1/settings/social", {"value": {"late_minutes": 20}, "base_version": 1},
                                self.owner["token"])
        self.assertEqual(status, 409)
        self.assertEqual(err["setting"]["value"], {"late_minutes": 10})
        # without base_version the last write wins
        self.assertEqual(self.ok("PUT", "/api/v1/settings/social", {"value": {"late_minutes": 20}}, self.owner["token"])["version"], 3)

    def test_bad_input(self):
        tok = self.owner["token"]
        self.assertEqual(self.call("PUT", "/api/v1/settings/nope", {"value": {}}, tok)[0], 404)
        self.assertEqual(self.call("GET", "/api/v1/settings/nope", token=tok)[0], 404)
        self.assertEqual(self.call("PUT", "/api/v1/settings/copy_rules", {"value": [1, 2]}, tok)[0], 400)
        self.assertEqual(self.call("PUT", "/api/v1/settings/copy_rules", {}, tok)[0], 400)
        self.assertEqual(self.call("PUT", "/api/v1/settings/copy_rules", {"value": {}, "base_version": "1"}, tok)[0], 400)
        big = {"rules": [{"pattern": "x" * 1000}] * 70}
        self.assertEqual(self.call("PUT", "/api/v1/settings/copy_rules", {"value": big}, tok)[0], 400)
        self.assertEqual(self.call("POST", "/api/v1/settings/copy_rules", {"value": {}}, tok)[0], 405)

    def test_clear_with_delete_or_null(self):
        tok = self.owner["token"]
        self.ok("PUT", "/api/v1/settings/seo_sites", {"value": {"prod": "https://example.org"}}, tok)
        out = self.ok("DELETE", "/api/v1/settings/seo_sites", None, tok)
        self.assertEqual((out["value"], out["version"]), (None, 2))
        self.ok("PUT", "/api/v1/settings/seo_sites", {"value": {"prod": "https://example.org"}}, tok)
        out = self.ok("PUT", "/api/v1/settings/seo_sites", {"value": None}, tok)
        self.assertEqual((out["value"], out["version"]), (None, 4))
        self.assertEqual(self.call("DELETE", "/api/v1/settings/seo_sites", None, self.editor["token"])[0], 403)

    def test_sse_and_activity(self):
        q = self.open_stream(self.editor["token"])
        self.ok("PUT", "/api/v1/settings/email_test_addresses", {"value": {"addresses": ["t@example.com"]}}, self.admin["token"])
        seen, data = self.saw(q, "settings")
        self.assertTrue(seen)
        self.assertEqual(data, {"key": "email_test_addresses", "version": 1})
        ev = self.ok("GET", "/api/v1/events?since=0", token=self.editor["token"])["events"]
        last = [e for e in ev if e["kind"] == "settings"][-1]
        self.assertEqual((last["actor"], last["data"]["key"]), (self.admin["member"]["id"], "email_test_addresses"))
        self.assertIn("Ada Admin", last["summary"])

    def test_survives_a_restart(self):
        self.ok("PUT", "/api/v1/settings/copy_rules", {"value": RULES}, self.owner["token"])
        self.srv.stop()
        self.srv = TeamServer(self.dir, host="127.0.0.1", port=0, clock=self.clock, ping_interval=0.3).start()
        self.port = self.srv.port
        got = self.ok("GET", "/api/v1/settings/copy_rules", token=self.editor["token"])
        self.assertEqual((got["value"], got["version"]), (RULES, 1))


if __name__ == "__main__":
    unittest.main()
