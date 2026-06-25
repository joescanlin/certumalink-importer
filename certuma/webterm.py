"""Allowlisted CLI runner for the in-console web terminal.

This is NOT a shell. It exposes a fixed registry of the platform's own data-population tools (the
NPPES doctor importer, the active-campaign seeder, the scheduler tick, the analytics rebuild, the
parity pipeline, the evidence export). A request is tokenized with shlex (never run through a shell),
the first token must match an allowlisted command key, and any argument is validated (e.g. a ZIP must
be five digits) before it is passed as a plain argv element to subprocess - so there is no shell, no
arbitrary command, and no argument injection. Stdlib only, so it imports on the no-DB path.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

__all__ = ["COMMANDS", "CommandResult", "run_command", "help_text", "ROOT"]

ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = ROOT / "tests" / "fixtures" / "nppes_mixed_page.json"
# a private, per-process, 0700 temp dir (mkdtemp) - never a predictable shared path, so there is no
# pre-creation / symlink race on the import output files
_OUT_DIR = Path(tempfile.mkdtemp(prefix="certuma-webterm-"))
_MAX_OUTPUT = 20_000
_ZIP_RE = re.compile(r"\A[0-9]{5}\Z")  # ASCII digits only, whole-string (no unicode digit / newline)
# only one command runs at a time per process: bounds resource use and prevents two DB-mutating tools
# (seed-active / rebuild) from racing on the operational DB
_RUN_LOCK = threading.Lock()


@dataclass(frozen=True)
class Command:
    key: str
    label: str
    description: str
    build: Callable          # (args, out_dir) -> argv list; raises ValueError on bad arguments
    timeout: int = 180


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: str
    exit_code: Optional[int]
    output: str
    error: str = ""


def _py(*module_args: str) -> List[str]:
    return [sys.executable, "-m", *module_args]


def _no_args(argv: List[str]) -> Callable:
    def build(args: List[str], out_dir: Path) -> List[str]:
        if args:
            raise ValueError("this command takes no arguments")
        return argv
    return build


def _import_zip(args: List[str], out_dir: Path) -> List[str]:
    if len(args) != 1 or not _ZIP_RE.match(args[0]):
        raise ValueError("usage: import-zip <5-digit-zip>  (e.g. import-zip 78701)")
    zip_code = args[0]
    out = out_dir / f"nppes_{zip_code}.json"
    return _py("certumalink_importer.cli", "import-zip", "--zip", zip_code, "--out", str(out),
               "--max-pages", "2")


def _import_demo(args: List[str], out_dir: Path) -> List[str]:
    if args:
        raise ValueError("this command takes no arguments")
    out = out_dir / "nppes_demo.json"
    return _py("certumalink_importer.cli", "import-zip", "--zip", "78701", "--out", str(out),
               "--fixture", str(_FIXTURE))


COMMANDS = {
    "import-demo": Command("import-demo", "Import demo doctors",
                           "Offline NPPES import of the bundled Austin sample (no network).",
                           _import_demo, 60),
    "import-zip": Command("import-zip", "Import doctors by ZIP",
                          "Live NPPES physician import for a ZIP code, e.g. import-zip 78701.",
                          _import_zip, 120),
    "seed-active": Command("seed-active", "Seed active campaign",
                           "Populate the Reach database with a full active-campaign dataset.",
                           _no_args(_py("certuma.active_seed")), 180),
    "rebuild": Command("rebuild", "Rebuild analytics",
                       "Rebuild the analytics reporting schema from the operational tables.",
                       _no_args(_py("certuma.reporting.elt")), 120),
    "evidence": Command("evidence", "Export evidence",
                        "Export the governed evidence datasets (CSV).",
                        _no_args(_py("certuma.reporting.export")), 120),
}
# NOTE: outbound-capable tools (the scheduler tick, the parity pipeline) are deliberately NOT in this
# allowlist - the terminal is for populating and deriving data, never for dispatching email. Run those
# from the real CLI (make tick / make parity) where the operator is fully in control.


def help_text() -> str:
    lines = ["Available commands:"]
    for c in COMMANDS.values():
        lines.append(f"  {c.key:13} {c.description}")
    lines.append("  help          Show this list.")
    return "\n".join(lines)


def _env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [str(ROOT), str(ROOT / "src")] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def run_command(line: str) -> CommandResult:
    """Parse, validate and run one allowlisted command. Never invokes a shell."""
    line = (line or "").strip()
    if not line:
        return CommandResult(False, "", None, "", "type a command, or 'help'")
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        return CommandResult(False, line, None, "", f"could not parse: {exc}")
    key, args = parts[0], parts[1:]
    if key in ("help", "?", "h"):
        return CommandResult(True, "help", 0, help_text())
    cmd = COMMANDS.get(key)
    if cmd is None:
        return CommandResult(False, key, None, "",
                             f"'{key}' is not an allowed command. Type 'help' for the list.")
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        argv = cmd.build(args, _OUT_DIR)
    except ValueError as exc:
        return CommandResult(False, key, None, "", str(exc))
    # only one command at a time per process; refuse (do not queue) if one is already running
    if not _RUN_LOCK.acquire(blocking=False):
        return CommandResult(False, key, None, "", "a command is already running, please wait")
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), env=_env(), capture_output=True, text=True,
                              timeout=cmd.timeout)
    except subprocess.TimeoutExpired:
        return CommandResult(False, key, None, "", f"timed out after {cmd.timeout}s")
    except Exception:  # pragma: no cover - defensive; do not leak internals to the client
        return CommandResult(False, key, None, "", "failed to run command")
    finally:
        _RUN_LOCK.release()
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + "\n... (output truncated)"
    return CommandResult(proc.returncode == 0, key, proc.returncode, out)
