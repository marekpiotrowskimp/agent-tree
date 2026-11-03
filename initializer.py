"""
initializer.py — build a context tree from an existing project directory.

Called by /init command in repl.py.

3-phase approach:
  1. Triage  — 1 LLM request: given file list, pick key files worth deep analysis
  2. Quick   — config/doc/infra files indexed without LLM (heuristics)
  3. Deep    — LLM summaries only for key files identified in phase 1
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath

from db import SessionDB
from llm_client import LLMClient

# ── constants ────────────────────────────────────────────────────────────────

PROJECT_INIT_PREFIX = "project-init: "

IGNORED_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".next", "target", ".cargo", "vendor",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "coverage", "htmlcov", ".tox",
}

ALLOWED_EXTENSIONS = {
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".xml", ".csv", ".env",
    ".md", ".rst", ".txt",
    ".sh", ".bash", ".zsh", ".fish",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".rb", ".php", ".cs", ".fs", ".fsx",
    ".swift", ".m", ".lua",
    ".ex", ".exs", ".erl", ".hrl",
    ".hs", ".lhs", ".r", ".jl",
    ".sql",
}

ALLOWED_NOEXT_FILENAMES = {
    "Makefile", "makefile", "GNUmakefile",
    "Dockerfile", "dockerfile",
    "Jenkinsfile", "Procfile", "Vagrantfile",
    "Gemfile", "Rakefile",
}

# Extensions that get quick (no-LLM) summaries
QUICK_CONFIG_EXTS = {
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".xml", ".csv", ".env",
}
QUICK_DOC_EXTS = {".md", ".rst", ".txt"}
QUICK_INFRA_EXTS = {".sh", ".bash", ".zsh", ".fish"}
QUICK_INFRA_NAMES = {"Makefile", "makefile", "GNUmakefile", "Dockerfile",
                     "dockerfile", "Jenkinsfile", "Procfile", "Vagrantfile",
                     "Gemfile", "Rakefile"}

MAX_FILES = 200
MAX_FILE_BYTES = 100_000  # 100 KB

TRIAGE_SYSTEM = """You are a project analyst. Given a list of file paths from a software project,
identify which files are ESSENTIAL for understanding the project's architecture, purpose, and logic.

Rules:
- Focus on: entry points, core modules, key algorithms, important configs, main docs
- Skip: test files (unless the project IS tests), generated files, minor configs, lock files
- Return between 1 and 30 files — only as many as truly matter
- Return ONLY valid JSON, no markdown fences:
{"key": ["path/to/file.py", ...], "reason": "brief note"}"""

FILE_SUMMARY_SYSTEM = (
    "You are a code file summariser. "
    "Given a file path and its contents write ONE concise English sentence (max 150 chars) "
    "describing what the file does, its key classes/functions, and purpose. "
    "Return ONLY that sentence, nothing else."
)


# ── file collection ──────────────────────────────────────────────────────────

def _collect_files(root: Path) -> list[Path]:
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext:
                if ext not in ALLOWED_EXTENSIONS:
                    continue
            else:
                if fname not in ALLOWED_NOEXT_FILENAMES:
                    continue
            full = Path(dirpath) / fname
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            collected.append(full.relative_to(root))
            if len(collected) >= MAX_FILES:
                return collected
    return collected


def _read_snippet(root: Path, rel: Path, max_bytes: int = MAX_FILE_BYTES) -> str:
    try:
        with open(root / rel, "rb") as fh:
            raw = fh.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        if text.count("\ufffd") / max(len(text), 1) > 0.1:
            return ""  # binary
        return text
    except OSError:
        return ""


# ── phase 1: triage ──────────────────────────────────────────────────────────

def _triage(files: list[Path], project_name: str, client: LLMClient) -> set[str]:
    """
    Ask LLM which files are essential. Returns set of str paths (relative, posix).
    Falls back to empty set on failure (caller will deep-analyse nothing extra).
    """
    file_list = "\n".join(str(f) for f in files)
    prompt = (
        f"{TRIAGE_SYSTEM}\n\n"
        f"Project: {project_name}\n"
        f"Files:\n{file_list}\n\n"
        f"Return JSON with key files:"
    )
    try:
        text, _ = client.ask(prompt)
        # strip markdown fences if present
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text)
        data = json.loads(text)
        return set(data.get("key", []))
    except Exception:
        return set()


# ── phase 2: quick summaries (no LLM) ────────────────────────────────────────

def _quick_summary(rel: Path, content: str) -> tuple[str, list[str]]:
    """Heuristic summary without LLM. Returns (summary, keywords)."""
    fname = rel.name
    ext = rel.suffix.lower()

    if ext in QUICK_CONFIG_EXTS or fname in QUICK_INFRA_NAMES and ext == "":
        summary = f"Configuration file: {fname}"

    elif ext in QUICK_DOC_EXTS:
        # first 2 non-empty lines
        lines = [l.strip().lstrip("#").strip() for l in content.splitlines() if l.strip()]
        summary = " — ".join(lines[:2])[:150] or f"Documentation: {fname}"

    elif ext in QUICK_INFRA_EXTS or fname in QUICK_INFRA_NAMES:
        # first non-comment, non-empty line
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                summary = f"Script: {stripped[:120]}"
                break
        else:
            summary = f"Script: {fname}"

    else:
        summary = f"Source file: {fname}"

    parts = PurePosixPath(str(rel)).parts
    keywords = list({p.rstrip("/") for p in parts if p not in (".", "/")})
    suf = rel.suffix.lstrip(".")
    if suf:
        keywords.append(suf)
    return summary, keywords[:8]


def _is_quick(rel: Path) -> bool:
    ext = rel.suffix.lower()
    fname = rel.name
    return (
        ext in QUICK_CONFIG_EXTS
        or ext in QUICK_DOC_EXTS
        or ext in QUICK_INFRA_EXTS
        or fname in QUICK_INFRA_NAMES
    )


# ── phase 3: deep summary (LLM) ──────────────────────────────────────────────

def _deep_summary(rel: Path, content: str, client: LLMClient) -> tuple[str, list[str]]:
    snippet = content[:3000]
    prompt = (
        f"{FILE_SUMMARY_SYSTEM}\n\n"
        f"File: {rel}\n\n---\n{snippet}\n---\n\nOne-sentence description:"
    )
    try:
        text, _ = client.ask(prompt)
        summary = text.strip().split("\n")[0][:200]
    except Exception:
        summary = f"{rel} — source file"

    parts = PurePosixPath(str(rel)).parts
    keywords = list({p.rstrip("/") for p in parts if p not in (".", "/")})
    suf = rel.suffix.lstrip(".")
    if suf:
        keywords.append(suf)
    return summary, keywords[:8]


# ── tree helpers ─────────────────────────────────────────────────────────────

def _ensure_project_root(db: SessionDB, project_name: str) -> str | None:
    label = f"{PROJECT_INIT_PREFIX}{project_name}/"
    existing = db.get_branch_by_label(label, parent_id="root")
    if existing:
        return None
    return db.add_branch(
        label=label,
        full_text="",
        summary=f"Project '{project_name}' — initialised by /init",
        keywords=[project_name, "project", "init"],
        parent_id="root",
    )


def _ensure_dir_node(db: SessionDB, dir_label: str, parent_id: str) -> str:
    node = db.get_branch_by_label(dir_label, parent_id=parent_id)
    if node:
        return node["id"]
    return db.add_branch(
        label=dir_label,
        full_text="",
        summary=f"Directory: {dir_label}",
        keywords=[dir_label.rstrip("/")],
        parent_id=parent_id,
    )


def _insert_file(db: SessionDB, project_root_id: str, rel: Path,
                 summary: str, keywords: list[str]):
    pure = PurePosixPath(str(rel))
    parts = pure.parts
    parent_id = project_root_id
    for j, segment in enumerate(parts):
        if j == len(parts) - 1:
            db.add_branch(
                label=segment,
                full_text="",
                summary=summary,
                keywords=keywords,
                parent_id=parent_id,
            )
        else:
            parent_id = _ensure_dir_node(db, segment + "/", parent_id)


# ── public API ────────────────────────────────────────────────────────────────

def init_project(
    project_path: Path,
    db: SessionDB,
    client: LLMClient,
    phase_cb=None,
) -> dict:
    """
    Build context tree from project_path.

    phase_cb(phase, current, total, label) — progress callback:
      phase: "triage" | "quick" | "deep"
      current, total: int
      label: str (current file or status)

    Returns:
      {"status": "ok", "added": int, "skipped": int, "truncated": bool, "project": str}
      {"status": "already_init", "project": str}
      {"status": "error", "message": str}
    """
    project_path = project_path.resolve()
    if not project_path.is_dir():
        return {"status": "error", "message": f"Not a directory: {project_path}"}

    project_name = project_path.name
    project_root_id = _ensure_project_root(db, project_name)
    if project_root_id is None:
        return {"status": "already_init", "project": project_name}

    # collect
    files = _collect_files(project_path)
    truncated = len(files) >= MAX_FILES

    # split into quick vs candidate-for-deep
    quick_files = [f for f in files if _is_quick(f)]
    code_files  = [f for f in files if not _is_quick(f)]

    # ── phase 1: triage ──────────────────────────────────────────────────────
    if phase_cb:
        phase_cb("triage", 0, 1, f"asking LLM to pick key files from {len(code_files)} candidates...")

    key_paths: set[str] = set()
    if code_files:
        key_paths = _triage(code_files, project_name, client)

    # fallback: if triage returned nothing, treat all code files as key
    if not key_paths and code_files:
        key_paths = {str(f) for f in code_files}

    key_files  = [f for f in code_files if str(f) in key_paths]
    skip_files = [f for f in code_files if str(f) not in key_paths]

    if phase_cb:
        phase_cb("triage", 1, 1, f"identified {len(key_files)} key files, {len(skip_files)} skipped")

    added = 0
    skipped = 0

    # ── phase 2: quick index ─────────────────────────────────────────────────
    total_quick = len(quick_files) + len(skip_files)
    for i, rel in enumerate(quick_files + skip_files):
        if phase_cb:
            phase_cb("quick", i + 1, total_quick, str(rel))
        content = _read_snippet(project_path, rel, max_bytes=4000)
        if not content:
            skipped += 1
            continue
        summary, keywords = _quick_summary(rel, content)
        _insert_file(db, project_root_id, rel, summary, keywords)
        added += 1

    # ── phase 3: deep analysis ────────────────────────────────────────────────
    for i, rel in enumerate(key_files):
        if phase_cb:
            phase_cb("deep", i + 1, len(key_files), str(rel))
        content = _read_snippet(project_path, rel)
        if not content:
            skipped += 1
            continue
        summary, keywords = _deep_summary(rel, content, client)
        _insert_file(db, project_root_id, rel, summary, keywords)
        added += 1

    return {
        "status": "ok",
        "added": added,
        "skipped": skipped,
        "truncated": truncated,
        "project": project_name,
        "key_count": len(key_files),
    }
