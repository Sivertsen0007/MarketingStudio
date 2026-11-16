"""Admin commands on the host: `member` (role, approver, active) with the same rules as the app.

Run:  python3 teamserver/tests/test_cli.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from teamserver.store import Store  # noqa: E402


def cli(*args):
    return subprocess.run([sys.executable, "-m", "teamserver", *args], cwd=REPO, capture_output=True, text=True,
                          timeout=60)


class MemberCommand(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="teamserver-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        st = Store(os.path.join(self.dir, "team.db"))
        try:
            self.owner = st.join(st.create_invite("o@example.com", "owner", "Olive")["token"], "Mac")[1]
            self.ed = st.join(st.create_invite("e@example.com", "editor", "Ed")["token"], "Mac")[1]
        finally:
            st.close()

    def run_ok(self, *args):
        r = cli(*args, "--data", self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_role_approver_active(self):
        out = self.run_ok("member", str(self.ed["id"]), "--approver", "yes")
        self.assertIn("editor, approver", out)
        out = self.run_ok("member", str(self.ed["id"]), "--role", "admin", "--approver", "no")
        self.assertIn("admin", out)
        self.assertNotIn("approver", out)
        out = self.run_ok("member", str(self.ed["id"]), "--active", "no")
        self.assertIn("inactive", out)

    def test_last_owner_stays(self):
        r = cli("member", str(self.owner["id"]), "--role", "editor", "--data", self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("at least one active owner", r.stderr)

    def test_nothing_to_change_and_unknown_member(self):
        r = cli("member", str(self.ed["id"]), "--data", self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing to change", r.stderr)
        r = cli("member", "999", "--role", "viewer", "--data", self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
