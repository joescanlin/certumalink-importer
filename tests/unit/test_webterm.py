"""Web-terminal allowlist + validation + document workspace. Pure, no DB.

Asserts the safety properties: only allowlisted commands run, the real certumalink_run is invoked
with validated flags and a forced output path, nothing reaches a shell, document paths cannot escape
the workspace, and bad input is rejected before any process is spawned.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma import webterm


class _IsolatedDocs(unittest.TestCase):
    """Point the workspace at a throwaway temp dir so tests never touch the live /tmp/certuma-docs."""
    def setUp(self):
        self._real_docs = webterm.DOCS_ROOT
        webterm.DOCS_ROOT = Path(tempfile.mkdtemp(prefix="certuma-docs-test-"))

    def tearDown(self):
        shutil.rmtree(webterm.DOCS_ROOT, ignore_errors=True)
        webterm.DOCS_ROOT = self._real_docs

    @property
    def OUT(self):
        return webterm.DOCS_ROOT


class WebtermCommandTests(_IsolatedDocs):
    def test_help_lists_commands(self):
        r = webterm.run_command("help")
        self.assertTrue(r.ok)
        for key in webterm.COMMANDS:
            self.assertIn(key, r.output)
        self.assertIn("certumalink_run", r.output)

    def test_unknown_command_rejected_before_subprocess(self):
        for line in ("rm -rf /", "ls", "bash", "python evil.py", "certumalink_run.py", "import-zip 78701"):
            r = webterm.run_command(line)
            self.assertFalse(r.ok, line)
            self.assertIsNone(r.exit_code, line)
            self.assertIn("not an allowed command", r.error)

    def test_empty_and_parse_errors(self):
        self.assertFalse(webterm.run_command("   ").ok)
        r = webterm.run_command('certumalink_run --zip "78701')  # unbalanced quote
        self.assertFalse(r.ok)
        self.assertIn("parse", r.error.lower())

    def test_certumalink_run_requires_a_valid_zip(self):
        for bad in ("certumalink_run", "certumalink_run --zip", "certumalink_run --zip abc",
                    "certumalink_run --zip 123", "certumalink_run --zip ٧٨٧٠١"):
            r = webterm.run_command(bad)
            self.assertFalse(r.ok, bad)
            self.assertIsNone(r.exit_code, bad)  # never spawned a process

    def test_certumalink_run_rejects_dangerous_flags(self):
        for bad in ("certumalink_run --zip 78701 --out /etc/x", "certumalink_run --update",
                    "certumalink_run --zip 78701 --publish-to-certumalink",
                    "certumalink_run --zip 78701 --status-ledger /etc/passwd",
                    "certumalink_run --zip 78701; id", "certumalink_run --zip 78701 $(id)"):
            r = webterm.run_command(bad)
            self.assertFalse(r.ok, bad)
            self.assertIsNone(r.exit_code, bad)

    def test_certumalink_run_argv_is_validated_and_output_forced(self):
        argv = webterm.COMMANDS["certumalink_run"].build(
            ["--zip", "78701", "--specialty", "dermatology", "--campaign", "dermatology", "--fixture"],
            self.OUT)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("certumalink-doctor-import.py"))
        self.assertIn("--zip", argv)
        self.assertIn("78701", argv)
        self.assertIn("--specialty", argv)
        # output is server-forced into the workspace; no user --out, no --update
        out_idx = argv.index("--out")
        self.assertTrue(str(webterm.DOCS_ROOT) in argv[out_idx + 1])
        self.assertNotIn("--update", argv)
        self.assertIn("--fixture", argv)

    def test_certumalink_run_rejects_bad_specialty_and_campaign(self):
        for bad in (["--zip", "78701", "--specialty", "bad;rm"],
                    ["--zip", "78701", "--campaign", "nope"],
                    ["--zip", "78701", "--max-pages", "99"]):
            with self.assertRaises(ValueError):
                webterm.COMMANDS["certumalink_run"].build(bad, self.OUT)

    def test_seed_active_maps_to_the_module(self):
        argv = webterm.COMMANDS["seed-active"].build([], self.OUT)
        self.assertEqual(argv[:2], [sys.executable, "-m"])
        self.assertIn("certuma.active_seed", argv)

    def test_no_arg_commands_reject_extra_args(self):
        self.assertFalse(webterm.run_command("seed-active --force").ok)
        self.assertIsNone(webterm.run_command("seed-active --force").exit_code)

    def test_outbound_capable_tools_are_not_in_the_allowlist(self):
        self.assertNotIn("tick", webterm.COMMANDS)
        self.assertNotIn("parity", webterm.COMMANDS)

    def test_output_is_capped(self):
        real = webterm.subprocess.run

        class _Proc:
            returncode = 0
            stdout = "x" * 30_000
            stderr = ""
        webterm.subprocess.run = lambda *a, **k: _Proc()
        try:
            r = webterm.run_command("rebuild")
        finally:
            webterm.subprocess.run = real
        self.assertLess(len(r.output), 30_000)
        self.assertIn("truncated", r.output)

    def test_timeout_fails_safe(self):
        real = webterm.subprocess.run

        def _raise(*a, **k):
            raise webterm.subprocess.TimeoutExpired(cmd="x", timeout=1)
        webterm.subprocess.run = _raise
        try:
            r = webterm.run_command("rebuild")
        finally:
            webterm.subprocess.run = real
        self.assertFalse(r.ok)
        self.assertIn("timed out", r.error)

    def test_only_one_command_runs_at_a_time(self):
        webterm._RUN_LOCK.acquire()
        try:
            r = webterm.run_command("rebuild")
            self.assertFalse(r.ok)
            self.assertIn("already running", r.error)
        finally:
            webterm._RUN_LOCK.release()


class WebtermDocsTests(_IsolatedDocs):
    def _make_doc(self, run, name, content):
        d = webterm.DOCS_ROOT / run
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content, encoding="utf-8")
        return f"{run}/{name}"

    def test_list_and_read_documents(self):
        rel = self._make_doc("run-900-testzip", "doctors.csv",
                             "npi,first_name,last_name\n1234567890,Ada,Lovelace\n9876543210,Al,Khwarizmi\n")
        runs = {r["run"]: r for r in webterm.list_documents()}
        self.assertIn("run-900-testzip", runs)
        f = next(x for x in runs["run-900-testzip"]["files"] if x["name"] == "doctors.csv")
        self.assertEqual(f["rows"], 2)
        self.assertTrue(f["importable"])
        doc = webterm.read_document(rel)
        self.assertEqual(doc["kind"], "csv")
        self.assertEqual(doc["header"], ["npi", "first_name", "last_name"])
        self.assertEqual(len(doc["rows"]), 2)

    def test_path_traversal_is_refused(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "run-900-testzip/../../../etc/passwd", ""):
            with self.assertRaises(ValueError):
                webterm.safe_doc_path(bad)

    def test_symlink_out_of_workspace_is_refused_and_not_listed(self):
        import os
        run = webterm.DOCS_ROOT / "run-903-sym"
        run.mkdir(parents=True)
        target = Path(tempfile.mkdtemp(prefix="certuma-secret-")) / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        try:
            os.symlink(target, run / "evil.csv")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unsupported here")
        try:
            with self.assertRaises(ValueError):
                webterm.safe_doc_path("run-903-sym/evil.csv")  # resolves outside the workspace
            listed = [f["name"] for r in webterm.list_documents() for f in r["files"]]
            self.assertNotIn("evil.csv", listed)  # symlinks are never surfaced
        finally:
            shutil.rmtree(target.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
