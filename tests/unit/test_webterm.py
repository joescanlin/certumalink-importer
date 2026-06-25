"""Web-terminal command allowlist + validation (Operations terminal). Pure, no DB, no subprocess.

These assert the safety properties: only allowlisted keys run, arguments are validated, nothing is
ever passed to a shell, and bad input is rejected before any process is spawned.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from certuma import webterm

OUT = Path("/tmp/certuma-webterm-test")


class WebtermTests(unittest.TestCase):
    def test_help_lists_commands_without_running_anything(self):
        r = webterm.run_command("help")
        self.assertTrue(r.ok)
        for key in webterm.COMMANDS:
            self.assertIn(key, r.output)

    def test_empty_input_is_a_friendly_error(self):
        r = webterm.run_command("   ")
        self.assertFalse(r.ok)
        self.assertIsNone(r.exit_code)

    def test_unknown_command_is_rejected_before_subprocess(self):
        for line in ("rm -rf /", "ls", "bash", "python evil.py", "git push"):
            r = webterm.run_command(line)
            self.assertFalse(r.ok, line)
            self.assertIsNone(r.exit_code)  # never spawned a process
            self.assertIn("not an allowed command", r.error)

    def test_import_zip_validates_the_zip(self):
        for bad in ("import-zip", "import-zip abc", "import-zip 123", "import-zip 1234567",
                    "import-zip 78701 extra", "import-zip ../etc"):
            r = webterm.run_command(bad)
            self.assertFalse(r.ok, bad)
            self.assertIsNone(r.exit_code)  # validation fails before spawning

    def test_no_arg_commands_reject_extra_args(self):
        r = webterm.run_command("seed-active --force")
        self.assertFalse(r.ok)
        self.assertIsNone(r.exit_code)

    def test_build_uses_argv_list_never_a_shell_string(self):
        argv = webterm.COMMANDS["import-zip"].build(["78701"], OUT)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("certumalink_importer.cli", argv)
        self.assertIn("78701", argv)
        self.assertNotIn("--help", argv)  # only the validated zip becomes an argument

    def test_seed_active_maps_to_the_module(self):
        argv = webterm.COMMANDS["seed-active"].build([], OUT)
        self.assertEqual(argv[:2], [sys.executable, "-m"])
        self.assertIn("certuma.active_seed", argv)

    def test_every_command_has_a_label_and_description(self):
        for c in webterm.COMMANDS.values():
            self.assertTrue(c.label and c.description)

    def test_outbound_capable_tools_are_not_in_the_allowlist(self):
        # the terminal populates/derives data; it must not be able to dispatch outbound email
        self.assertNotIn("tick", webterm.COMMANDS)
        self.assertNotIn("parity", webterm.COMMANDS)

    def test_zip_validator_is_ascii_and_whole_string(self):
        self.assertFalse(webterm.run_command("import-zip ٧٨٧٠١").ok)   # arabic-indic digits
        self.assertFalse(webterm.run_command("import-zip १२३४५").ok)   # devanagari digits

    def test_shell_metacharacters_in_args_never_spawn(self):
        for line in ("import-zip 78701; id", "import-zip 78701 && id", "import-zip $(id)",
                     "import-zip `id`", "import-zip 78701|cat"):
            r = webterm.run_command(line)
            self.assertFalse(r.ok, line)
            self.assertIsNone(r.exit_code, line)  # validation rejected it before any process

    def test_shlex_parse_error_is_handled(self):
        r = webterm.run_command('import-zip "78701')  # unbalanced quote
        self.assertFalse(r.ok)
        self.assertIsNone(r.exit_code)
        self.assertIn("parse", r.error.lower())

    def test_prefix_case_and_empty_variants_are_rejected(self):
        for line in ("import", "seed", "IMPORT-DEMO", "import-demoX", '""', "  "):
            r = webterm.run_command(line)
            self.assertFalse(r.ok, repr(line))
            self.assertIsNone(r.exit_code, repr(line))

    def test_import_demo_is_offline_via_the_bundled_fixture(self):
        argv = webterm.COMMANDS["import-demo"].build([], OUT)
        self.assertIn("--fixture", argv)
        self.assertTrue(any("nppes_mixed_page.json" in a for a in argv))

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
        self.assertIsNone(r.exit_code)
        self.assertIn("timed out", r.error)

    def test_only_one_command_runs_at_a_time(self):
        webterm._RUN_LOCK.acquire()
        try:
            r = webterm.run_command("rebuild")  # build succeeds, but the lock is held
            self.assertFalse(r.ok)
            self.assertIn("already running", r.error)
        finally:
            webterm._RUN_LOCK.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
