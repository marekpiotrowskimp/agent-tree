from llm_client import LLMClient, _dbg
from db import SessionDB
from log import log
import json, re
from pathlib import PurePosixPath

UPDATER_SYSTEM = """Jesteś updaterem drzewa kontekstu.

Zasady:
1. Wyciągnij NOWE tematy/informacje z odpowiedzi
2. Dodaj je jako nowe gałęzie (dzieci aktualnego noda)
3. Jeśli istniejąca gałąź jest za długa, oznacz ją do kompresji
4. Jeśli prompt dotyczy konkretnej gałęzi, ustaw ją jako nowy current_node

Zwróć TYLKO czysty JSON bez znaczników:
{"new_branches":[{"label":"","full_text":"","summary":"","keywords":[]}],"compress":[],"current_node":"b_001","summary":""}"""

FILES_CACHE_LABEL = "files-cache"

# prompt wysyłany do LLM po każdym wykonaniu z plikami
FILE_SUMMARY_SYSTEM = """Jesteś asystentem opisującym plik kodu.
Na podstawie podanego kontekstu (nazwa pliku, akcja, fragment odpowiedzi) napisz JEDNO zdanie po angielsku opisujące plik.
Skup się na: co robi plik, kluczowe funkcje/klasy, ważne zmiany. Max 120 znaków.
Zwróć TYLKO to zdanie, bez żadnych dodatkowych słów."""


def _branch_path(branch_id, tree_map):
    path = []
    cur = branch_id
    while cur:
        path.append(cur)
        cur = tree_map[cur]["parent_id"] if cur in tree_map else None
    return set(path)


def _trimmed_preview(tree, current_id, max_chars):
    tree_map = {b["id"]: b for b in tree}
    path = _branch_path(current_id, tree_map) if current_id in tree_map else set()

    prioritized = []
    rest = []
    for b in tree:
        if b["id"] in path or b["id"] == current_id:
            prioritized.append(b)
        else:
            rest.append(b)
    rest.sort(key=lambda b: b["depth"])
    ordered = prioritized + rest

    lines = []
    chars = 0
    for b in ordered:
        line = f"[{b['id']}] {b['label']} (depth={b['depth']})"
        if chars + len(line) + 1 > max_chars:
            if chars > 0:
                lines.append(f"[...] ({len(ordered) - len(lines)} more branches omitted)")
            break
        lines.append(line)
        chars += len(line) + 1
    return "\n".join(lines)


def _parse_json(text):
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return None


def _ensure_files_cache(db: SessionDB) -> str:
    """Return id of the files-cache root node, creating it if needed."""
    node = db.get_branch_by_label(FILES_CACHE_LABEL, parent_id="root")
    if node:
        return node["id"]
    return db.add_branch(
        label=FILES_CACHE_LABEL,
        full_text="",
        summary="Index of files read or modified by the agent",
        keywords=["files", "cache", "index"],
        parent_id="root",
    )


def _ensure_dir_node(db: SessionDB, dir_label: str, parent_id: str) -> str:
    """Return id of a directory node under parent_id, creating it if needed."""
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


def _get_file_summary(path: str, actions: list, response_excerpt: str, client: LLMClient) -> tuple[str, list]:
    """Ask cheap LLM for a one-sentence file description. Returns (summary, keywords)."""
    actions_str = "/".join(actions)
    excerpt = response_excerpt[:400] if response_excerpt else ""
    prompt = (
        f"{FILE_SUMMARY_SYSTEM}\n\n"
        f"File: {path}\nAction: {actions_str}\n"
        f"Context excerpt:\n{excerpt}\n\nOne-sentence description:"
    )
    try:
        text, _ = client.ask(prompt)
        summary = text.strip().split("\n")[0][:200]
    except Exception:
        summary = f"{path} — {actions_str}"

    # keywords: filename + parent dir + actions
    parts = PurePosixPath(path).parts
    keywords = list({p.rstrip("/") for p in parts if p not in (".", "/")}) + actions
    return summary, keywords[:8]


def _upsert_file_node(db: SessionDB, path: str, actions: list, response: str, client: LLMClient, files_cache_id: str):
    """Create or update the hierarchical node for a touched file."""
    pure = PurePosixPath(path)
    # Build list of (segment, is_leaf)
    parts = pure.parts  # e.g. ('src', 'models', 'user.py')

    # Walk/create directory nodes
    parent_id = files_cache_id
    for i, segment in enumerate(parts):
        is_leaf = (i == len(parts) - 1)
        if is_leaf:
            # Check if file node already exists
            existing = db.get_branch_by_label(segment, parent_id=parent_id)
            summary, keywords = _get_file_summary(path, actions, response, client)
            if existing:
                db.update_branch_summary(existing["id"], summary, keywords)
            else:
                db.add_branch(
                    label=segment,
                    full_text="",
                    summary=summary,
                    keywords=keywords,
                    parent_id=parent_id,
                )
        else:
            dir_label = segment + "/"
            parent_id = _ensure_dir_node(db, dir_label, parent_id)


def update(prompt: str, response: str, db: SessionDB, client: LLMClient,
           selected_ids=None, summarised_ids=None, max_tree_tokens=8000,
           orchestrator_tokens=None, touched_files: dict | None = None):
    current = db.current_node_id
    current_branch = db.get_branch(current)

    log("update_start", current=current, prompt_len=len(prompt), response_len=len(response),
        model=client.model if hasattr(client, 'model') else '?')

    max_chars = max_tree_tokens * 4
    tree_preview = _trimmed_preview(db.full_tree(), current, max_chars)

    full_prompt = (
        f"{UPDATER_SYSTEM}\n\nAktualne drzewo:\n{tree_preview}\n\n"
        f"Current node: {current} ({current_branch['label'] if current_branch else 'root'})\n\n"
        f"Prompt: {prompt}\n\nOdpowiedź: {response}\n\nZaktualizuj drzewo."
    )

    _dbg("updater", f"current={current} prompt_chars={len(prompt)} response_chars={len(response)}")

    try:
        raw, usage = client.ask(full_prompt)
        _dbg("updater", f"raw_chars={len(raw)} preview={raw[:200]!r}")
        log("update_ask_done", raw_chars=len(raw), usage=usage)
    except RuntimeError as e:
        _dbg("updater", f"ask() FAILED: {e}")
        log("update_ask_failed", error=str(e))
        import sys
        sys.stderr.write(f"\033[90m  ⚠ Updater failed ({e}), tree not updated\033[0m\n")
        sys.stderr.flush()
        db.log(prompt, selected_ids or [], summarised_ids or [], [],
               tokens_sent=orchestrator_tokens.get("select_sent", 0) + orchestrator_tokens.get("execute_sent", 0) if orchestrator_tokens else 0,
               tokens_received=orchestrator_tokens.get("select_received", 0) + orchestrator_tokens.get("execute_received", 0) if orchestrator_tokens else 0)
        return {"new_branch_ids": [], "compressed": [], "current_node": current, "summary": "", "usage": {"sent": 0, "received": 0}}

    result = _parse_json(raw)
    if result is None:
        _dbg("updater", f"JSON parse FAILED, raw={raw!r}")
        log("update_parse_failed", raw_preview=raw[:200])
        import sys
        sys.stderr.write(f"\033[90m  ⚠ Updater returned invalid JSON, tree not updated\033[0m\n")
        sys.stderr.flush()
        result = {"new_branches": [], "compress": [], "current_node": current}

    new_ids = []
    for branch_data in result.get("new_branches", []):
        bid = db.add_branch(
            label=branch_data.get("label", "Node"),
            full_text=branch_data.get("full_text", ""),
            summary=branch_data.get("summary", ""),
            keywords=branch_data.get("keywords", []),
        )
        new_ids.append(bid)

    for compress_id in result.get("compress", []):
        branch = db.get_branch(compress_id)
        if branch and branch["full_text"]:
            db.compress_branch(compress_id, branch["summary"])

    new_current = result.get("current_node", current)
    if new_current != current and db.get_branch(new_current):
        db.current_node_id = new_current

    # Upsert files-cache entries for touched files
    if touched_files:
        files_cache_id = _ensure_files_cache(db)
        log("update_touch_files", count=len(touched_files), files=list(touched_files.keys()))
        for path, info in touched_files.items():
            try:
                _upsert_file_node(db, path, info["actions"], response, client, files_cache_id)
            except Exception as e:
                log("update_touch_error", path=path, error=str(e))

    db.log(prompt, selected_ids or [], summarised_ids or [], new_ids,
           tokens_sent=usage["sent"] + (orchestrator_tokens.get("select_sent", 0) + orchestrator_tokens.get("execute_sent", 0) if orchestrator_tokens else 0),
           tokens_received=usage["received"] + (orchestrator_tokens.get("select_received", 0) + orchestrator_tokens.get("execute_received", 0) if orchestrator_tokens else 0))

    log("update_done", new_branches=len(new_ids), compressed=len(result.get("compress", [])),
        new_current=db.current_node_id, usage=usage)
    return {
        "new_branch_ids": new_ids,
        "compressed": result.get("compress", []),
        "current_node": db.current_node_id,
        "summary": result.get("summary", ""),
        "usage": usage,
    }

