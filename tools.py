import os
import re
import subprocess
from pathlib import Path
_workspace = None


def _get_workspace():
    global _workspace
    if _workspace is None:
        _workspace = Path(os.getenv("AGENT_TREE_WORKSPACE", ".")).resolve()
        _workspace.mkdir(parents=True, exist_ok=True)
    return _workspace


def set_workspace(path):
    global _workspace
    p = Path(path).resolve()
    p.mkdir(parents=True, exist_ok=True)
    _workspace = p


def get_workspace():
    return _get_workspace()


MAX_GREP_RESULTS = 200
MAX_GLOB_RESULTS = 200
MAX_WEBFETCH_CHARS = 10000
MAX_QUESTION_ROUNDS = 3

_TOOL_CALL_COUNTS: dict[tuple, int] = {}


def _reset_tool_counts():
    _TOOL_CALL_COUNTS.clear()


def _tool_call_key(name: str, args: dict) -> tuple:
    return (name, frozenset(sorted(args.items())))


def _resolve(path: str) -> Path:
    return (_get_workspace() / path).resolve()


def _validate(p: Path):
    try:
        p.relative_to(_get_workspace())
    except ValueError:
        raise PermissionError(f"Path outside workspace: {p}")


# ────────────────────────────── TOOLS ──────────────────────────────


def bash(command: str, timeout: int = 120000, workdir: str = ".") -> str:
    p = _resolve(workdir)
    _validate(p)
    ms = max(timeout, 1000) / 1000
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=ms, cwd=str(p),
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {ms:.0f}s"
    out = r.stdout.strip()
    err = r.stderr.strip()
    parts = [f"Exit: {r.returncode}"]
    if out:
        parts.append(out[:6000])
    if err:
        parts.append(f"[stderr]\n{err[:2000]}")
    return "\n".join(parts)


def grep(pattern: str, path: str = ".", include: str = "*") -> str:
    p = _resolve(path)
    _validate(p)
    results = []
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"
    for fp in sorted(p.rglob(include)):
        if not fp.is_file():
            continue
        try:
            rel = str(fp.relative_to(p))
        except ValueError:
            rel = str(fp)
        try:
            for lineno, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if compiled.search(line):
                    results.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(results) >= MAX_GREP_RESULTS:
                        results.append(f"[...] truncated at {MAX_GREP_RESULTS} matches")
                        return "\n".join(results)
        except Exception:
            pass
    return "\n".join(results) if results else "(no matches)"


def glob(pattern: str, path: str = ".") -> str:
    key = _tool_call_key("glob", {"pattern": pattern, "path": path})
    _TOOL_CALL_COUNTS.setdefault(key, 0)
    _TOOL_CALL_COUNTS[key] += 1
    if _TOOL_CALL_COUNTS[key] > 1:
        return "(already listed above, use grep or read_file for specific files)"
    p = _resolve(path)
    _validate(p)
    matches = sorted(str(m.relative_to(p)) for m in p.glob(pattern) if m.is_file() or m.is_dir())
    if len(matches) > MAX_GLOB_RESULTS:
        matches = matches[:MAX_GLOB_RESULTS] + [f"[...] truncated at {MAX_GLOB_RESULTS}"]
    return "\n".join(matches) if matches else "(no matches)"


def read_file(path: str, offset: int = 0, limit: int = None) -> str:
    p = _resolve(path)
    _validate(p)
    key = _tool_call_key("read_file", {"path": path, "offset": offset, "limit": limit})
    _TOOL_CALL_COUNTS.setdefault(key, 0)
    _TOOL_CALL_COUNTS[key] += 1
    if not p.is_file():
        if _TOOL_CALL_COUNTS[key] > 1:
            return "(already tried — file doesn't exist. Use write_file to create it)"
        return f"Error: file not found: {path}. Use write_file to create it."
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    if offset:
        lines = lines[offset:]
    if limit is not None:
        lines = lines[:limit]
    result = "\n".join(lines)
    if offset or limit is not None:
        start = offset if offset else 0
        end = start + len(lines) - 1
        result = f"[Lines {start}-{end} / {total}]\n{result}"
    return result


def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    _validate(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Written {len(content)} bytes to {path}"


def edit_file(path: str, old: str, new: str) -> str:
    p = _resolve(path)
    _validate(p)
    if not p.is_file():
        return f"Error: file not found: {path}"
    text = p.read_text(encoding="utf-8")
    if old not in text:
        return f"Error: string not found in {path}"
    text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    return f"Edited {path}"


def list_dir(path: str = ".") -> str:
    key = _tool_call_key("list_dir", {"path": path})
    _TOOL_CALL_COUNTS.setdefault(key, 0)
    _TOOL_CALL_COUNTS[key] += 1
    if _TOOL_CALL_COUNTS[key] > 1:
        return "(already listed above, use glob or read_file for details)"
    p = _resolve(path)
    _validate(p)
    if not p.is_dir():
        return f"Error: directory not found: {path}"
    entries = []
    for child in sorted(p.iterdir()):
        suffix = "/" if child.is_dir() else ""
        entries.append(f"{child.name}{suffix}")
    return "\n".join(entries) if entries else "(empty)"


def webfetch(url: str, format: str = "text", timeout: int = 30) -> str:
    import httpx
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type:
            text = resp.text
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = text.strip()
        else:
            text = resp.text
        return text[:MAX_WEBFETCH_CHARS]
    except Exception as e:
        return f"Error fetching {url}: {e}"


_question_callback = None
_question_count = 0


def set_question_callback(cb):
    global _question_callback
    _question_callback = cb


def _reset_question_count():
    global _question_count
    _question_count = 0


def question(text: str, options: list = None) -> str:
    global _question_count
    if not _question_callback:
        return "Error: question tool unavailable (no interactive session)"
    _question_count += 1
    if _question_count > MAX_QUESTION_ROUNDS:
        return "Error: max question rounds reached"
    return _question_callback(text, options or [])


def todowrite(todos: list) -> str:
    if not todos:
        return "(no tasks)"
    lines = []
    for t in todos:
        content = t.get("content", "")
        status = t.get("status", "pending")
        icon = {"completed": "[x]", "in_progress": "[>]", "cancelled": "[-]"}.get(status, "[ ]")
        lines.append(f"  {icon} {content}")
    return "\n".join(lines) if lines else "(no tasks)"


# ───────────────────────── TOOL DEFINITIONS ─────────────────────────

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command in the project workspace. Returns exit code, stdout, and stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in milliseconds (default 120000)"},
                    "workdir": {"type": "string", "description": "Working directory relative to workspace (default: .)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace. Returns full content or a slice (offset/limit).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (0-indexed)"},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file and parent directories if they don't exist. Overwrites if file exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Find and replace text in an existing file. Replaces only the FIRST occurrence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace"},
                    "old": {"type": "string", "description": "Existing text to find and replace"},
                    "new": {"type": "string", "description": "New text to insert in place of old text"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression pattern. Use to find code references, definitions, or patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in (default: .)"},
                    "include": {"type": "string", "description": "File pattern filter, e.g. '*.py' or '*.{ts,tsx}'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob pattern. Returns sorted list of relative paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. 'src/**/*.py' or '*.js'"},
                    "path": {"type": "string", "description": "Directory to search in (default: .)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace (default: .)"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": "Fetch content from a URL. Returns text content (strips HTML tags for web pages).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "format": {"type": "string", "enum": ["text", "html"], "description": "Output format (default: text)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "question",
            "description": "Ask the user a question during task execution. Use when you need clarification, a decision, or missing information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The question to ask the user"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "Optional list of predefined options"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todowrite",
            "description": "Create a task list to organize your work. Use for multi-step tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Task description"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                            },
                            "required": ["content", "status"],
                        },
                        "description": "Array of task items",
                    }
                },
                "required": ["todos"],
            },
        },
    },
]

TOOL_MAP = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "grep": grep,
    "glob": glob,
    "list_dir": list_dir,
    "webfetch": webfetch,
    "question": question,
    "todowrite": todowrite,
}

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf?\b", r"\bgit\s+push\b", r"\bcurl\b", r"\bwget\b",
    r"\bpip\s+(uninstall|uninstall)\b", r"\bnpm\s+unpublish\b",
    r"\bsudo\b", r"\bchmod\b", r"\bchown\b",
]


def is_dangerous(command: str) -> bool:
    return any(re.search(p, command) for p in DANGEROUS_PATTERNS)


def execute_tool(name: str, args: dict) -> str:
    func = TOOL_MAP.get(name)
    if not func:
        return f"Error: unknown tool: {name}"
    try:
        return func(**args)
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
