"""Allowlisted CLI runner + document workspace for the in-console web terminal.

This is NOT a shell. It runs the platform's own data tools - chiefly the real certumalink_run doctor
importer (the bundled portable/certumalink-doctor-import.py) - and the active-campaign seeder, the
analytics rebuild, and the evidence export. A request is tokenized with shlex (never a shell), the
first token must match an allowlisted command, and every flag/value is validated (a ZIP is five
ASCII digits, a campaign is one of a fixed set, no path/`--out`/`--update` is accepted) before it is
passed as a plain argv element to subprocess - so there is no shell, no arbitrary command, and no
argument injection.

certumalink_run writes a BUNDLE of documents (doctors.csv, profile_drafts.csv, rox_outreach.csv,
summary.json, ...) into a per-run directory inside a visible workspace, so the console can list,
preview and import them. Stdlib only, so it imports on the no-DB path; the DB import of a document
lives in certuma.docimport.
"""
from __future__ import annotations

import csv
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Callable, List, Optional

__all__ = ["COMMANDS", "CommandResult", "run_command", "help_text", "ROOT", "DOCS_ROOT",
           "list_documents", "read_document", "safe_doc_path"]

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "portable" / "certumalink-doctor-import.py"
_FIXTURE = ROOT / "tests" / "fixtures" / "nppes_mixed_page.json"
# a per-user, listable workspace for the generated documents. Owned by this server, mode 0700, and
# never a world-shared path - _ensure_docs_root refuses to adopt a pre-existing symlink or a directory
# owned by someone else, so a co-tenant cannot plant or redirect it. Persists across restarts so a
# run's documents stay available to import.
DOCS_ROOT = Path(tempfile.gettempdir()) / f"certuma-docs-{os.getuid()}"
_MAX_OUTPUT = 20_000
_PREVIEW_ROWS = 200
_ZIP_RE = re.compile(r"\A[0-9]{5}\Z")
_SPECIALTY_RE = re.compile(r"\A[A-Za-z][A-Za-z &/-]{0,40}\Z")
_CAMPAIGN_PRESETS = ("primary-care", "dermatology", "cardiology", "urgent-care")
# one command at a time per process: bounds resource use and keeps the run-dir sequence collision-free
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


def _ensure_docs_root() -> None:
    """Create the workspace 0700 and owned by us; refuse to adopt a symlink or someone else's dir."""
    if DOCS_ROOT.exists() or DOCS_ROOT.is_symlink():
        st = DOCS_ROOT.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError("workspace path is not a directory owned by this server; refusing to use it")
        os.chmod(DOCS_ROOT, 0o700)
    else:
        DOCS_ROOT.mkdir(parents=True, mode=0o700)


def _claim_run_dir(label: str) -> Path:
    """Atomically claim a fresh, uniquely-numbered run directory in the workspace."""
    _ensure_docs_root()
    seq = 1
    while True:
        cand = DOCS_ROOT / f"run-{seq:03d}-{label}"
        try:
            cand.mkdir(mode=0o700)
            return cand
        except FileExistsError:
            seq += 1


def _build_certumalink_run(args: List[str], out_dir: Path) -> List[str]:
    """Validate certumalink_run flags and build its argv, forcing output into the workspace.

    Accepts: --zip <5 digits> (required), --specialty <word> (repeatable), --campaign <preset>,
    --max-pages <1-10>, --fixture (offline, bundled sample), --quiet, --publish-dry-run. Everything
    else - including --out, --update, --publish-to-certumalink, file paths - is rejected.
    """
    flags: List[str] = []
    zip_code = None
    use_fixture = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--zip":
            i += 1
            if i >= len(args) or not _ZIP_RE.match(args[i]):
                raise ValueError("--zip requires a 5-digit ZIP, e.g. --zip 78701")
            zip_code = args[i]
            flags += ["--zip", zip_code]
        elif a == "--specialty":
            i += 1
            if i >= len(args) or not _SPECIALTY_RE.match(args[i]):
                raise ValueError("--specialty requires a word, e.g. --specialty dermatology")
            flags += ["--specialty", args[i]]
        elif a == "--campaign":
            i += 1
            if i >= len(args) or args[i] not in _CAMPAIGN_PRESETS:
                raise ValueError("--campaign must be one of: " + ", ".join(_CAMPAIGN_PRESETS))
            flags += ["--campaign", args[i]]
        elif a == "--max-pages":
            i += 1
            if i >= len(args) or not re.fullmatch(r"[0-9]+", args[i]) or not (1 <= int(args[i]) <= 10):
                raise ValueError("--max-pages must be an integer 1-10")
            flags += ["--max-pages", args[i]]
        elif a == "--fixture":
            use_fixture = True
        elif a in ("--quiet", "--publish-dry-run"):
            pass  # accepted; the runner always adds these itself
        else:
            raise ValueError(
                f"flag {a!r} is not allowed. Usage: certumalink_run --zip 78701 "
                "[--specialty dermatology] [--campaign dermatology] [--max-pages 2] [--fixture]")
        i += 1
    if zip_code is None:
        raise ValueError("certumalink_run needs a --zip, e.g. certumalink_run --zip 78701")
    run_dir = _claim_run_dir(zip_code)
    argv = [sys.executable, str(PORTABLE), *flags, "--out", str(run_dir),
            "--publish-dry-run", "--quiet"]
    if use_fixture:
        argv += ["--fixture", str(_FIXTURE)]
    return argv


COMMANDS = {
    "certumalink_run": Command("certumalink_run", "Import doctors (certumalink_run)",
                               "Run the real CMS NPPES importer for a ZIP and write a document bundle. "
                               "e.g. certumalink_run --zip 78701 --specialty dermatology",
                               _build_certumalink_run, 180),
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
# allowlist - the terminal populates and derives data, it never dispatches email.


def help_text() -> str:
    lines = ["Available commands:"]
    for c in COMMANDS.values():
        lines.append(f"  {c.key:16} {c.description}")
    lines.append("  help             Show this list.")
    lines.append("")
    lines.append("certumalink_run writes a document bundle you can preview and import below.")
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
        argv = cmd.build(args, DOCS_ROOT)
    except ValueError as exc:
        return CommandResult(False, key, None, "", str(exc))
    if not _RUN_LOCK.acquire(blocking=False):
        return CommandResult(False, key, None, "", "a command is already running, please wait")
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), env=_env(), capture_output=True, text=True,
                              timeout=cmd.timeout)
    except subprocess.TimeoutExpired:
        return CommandResult(False, key, None, "", f"timed out after {cmd.timeout}s")
    except Exception:  # pragma: no cover - defensive; never leak internals to the client
        return CommandResult(False, key, None, "", "failed to run command")
    finally:
        _RUN_LOCK.release()
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > _MAX_OUTPUT:
        out = out[:_MAX_OUTPUT] + "\n... (output truncated)"
    return CommandResult(proc.returncode == 0, key, proc.returncode, out)


# ---- generated-document workspace ------------------------------------------------------------

def safe_doc_path(rel: str) -> Path:
    """Resolve a workspace-relative document path, refusing anything outside DOCS_ROOT."""
    root = DOCS_ROOT.resolve()
    p = (root / (rel or "")).resolve()
    if p == root or root not in p.parents:
        raise ValueError("no such document")
    if not p.is_file():
        raise ValueError("no such document")
    return p


def _row_count(path: Path) -> Optional[int]:
    if path.suffix.lower() != ".csv":
        return None
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            n = sum(1 for _ in fh)
        return max(0, n - 1)  # minus the header
    except OSError:
        return None


def list_documents() -> list:
    """The generated runs (newest first), each with its bundle files + row counts."""
    if not DOCS_ROOT.exists():
        return []
    runs = sorted([p for p in DOCS_ROOT.iterdir()
                   if p.is_dir() and not p.is_symlink() and p.name.startswith("run-")],
                  key=lambda p: p.name, reverse=True)
    out = []
    for run in runs:
        files = []
        for f in sorted(run.iterdir()):
            if not f.is_file() or f.is_symlink():  # never surface a planted symlink's target metadata
                continue
            files.append({"name": f.name, "rel": f"{run.name}/{f.name}",
                          "size": f.stat().st_size, "rows": _row_count(f),
                          "kind": f.suffix.lstrip(".").lower(),
                          "importable": f.suffix.lower() == ".csv"})
        if files:  # skip empty run dirs left by a rejected / failed run
            out.append({"run": run.name, "files": files})
    return out


def read_document(rel: str, *, max_rows: int = _PREVIEW_ROWS) -> dict:
    """Parse a document for preview: CSV -> header + capped rows; JSON/text -> capped text."""
    path = safe_doc_path(rel)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(islice(csv.reader(fh), max_rows + 1))
        header = rows[0] if rows else []
        body = rows[1:]
        total = _row_count(path) or 0
        return {"kind": "csv", "name": path.name, "header": header, "rows": body,
                "total_rows": total, "truncated": total > len(body)}
    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > _MAX_OUTPUT
    return {"kind": "text", "name": path.name, "text": text[:_MAX_OUTPUT], "truncated": truncated}
