from llm_client import LLMClient, _dbg
from db import SessionDB
from log import log
import json, re

SELECTOR_SYSTEM = """Jesteś selektorem drzewa kontekstu.

Drzewo kontekstu zawiera gałęzie z polami: id, label, summary, keywords, depth.

Zasady:
1. Analizuj gałęzie po label, summary i keywords
2. Wybierz tylko te POWIĄZANE z promptem
3. Jeśli kontekst > {max_tokens} tokenów, podsumuj mniej istotne
4. Uwzględnij current_node i jego dzieci priorytetowo
5. current_node to zawsze prawdziwe ID gałęzi, np. "root" lub "b_abc123"
6. Jeśli prompt wymaga AKTUALNYCH danych z internetu (newsy, ceny, daty, wydarzenia), ustaw web_search na zapytanie. W przeciwnym razie zostaw puste.

Zwróć TYLKO czysty JSON bez żadnych znaczników:
{{"selected":[],"summarised":{{}},"current_node":"root","web_search":""}}"""

MAX_SELECT_RETRIES = 2


def _branch_path(branch_id, tree_map):
    path = []
    cur = branch_id
    while cur:
        path.append(cur)
        cur = tree_map[cur]["parent_id"] if cur in tree_map else None
    return set(path)


def _trimmed_tree(tree, current_id, max_chars):
    tree_map = {b["id"]: b for b in tree}
    if current_id in tree_map:
        path = _branch_path(current_id, tree_map)
    else:
        path = set()

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
        line = f"[{b['id']}] {b['label']} | depth={b['depth']}"
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

def select(prompt, db: SessionDB, client: LLMClient, max_tokens: int = 8000, max_tree_tokens: int = 16000):
    tree = db.full_tree()
    current = db.current_node_id
    current_branch = db.get_branch(current)

    log("select_start", tree_size=len(tree), current=current, prompt_len=len(prompt),
        model=client.model if hasattr(client, 'model') else '?')

    max_chars = max_tree_tokens * 4
    tree_text = _trimmed_tree(tree, current, max_chars)
    if current_branch:
        tree_text = f"[AKTUALNY NODE] {current_branch['id']}: {current_branch['label']}\n\n{tree_text}"

    system = SELECTOR_SYSTEM.format(max_tokens=max_tokens)
    full_prompt = f"{system}\n\nDrzewo:\n{tree_text}\n\nPrompt: {prompt}\n\nKtóre gałęzie wybrać?"

    _dbg("selector", f"tree_branches={len(tree)} current={current} prompt_chars={len(prompt)}")

    raw = None
    last_err = None
    for attempt in range(MAX_SELECT_RETRIES):
        try:
            raw, usage = client.ask(full_prompt)
            _dbg("selector", f"attempt={attempt+1} raw_chars={len(raw)} preview={raw[:200]!r}")
            log("select_attempt", attempt=attempt+1, raw_chars=len(raw), usage=usage)
            break
        except RuntimeError as e:
            last_err = e
            _dbg("selector", f"attempt={attempt+1} FAILED: {e}")
            log("select_attempt_failed", attempt=attempt+1, error=str(e))

    if raw is None:
        import sys
        sys.stderr.write(f"\033[93m⚠ Selector failed ({last_err}), running without context\033[0m\n")
        sys.stderr.flush()
        log("select_failed", error=str(last_err))
        return {
            "context": "",
            "selected_ids": [],
            "summarised_ids": [],
            "new_current": current,
            "web_search": "",
            "usage": {"sent": 0, "received": 0},
            "error": str(last_err),
        }

    parsed = _parse_json(raw)
    if parsed is None:
        _dbg("selector", f"JSON parse FAILED, raw={raw!r}")
        import sys
        sys.stderr.write(f"\033[93m⚠ Selector returned invalid JSON, running without context\033[0m\n")
        sys.stderr.flush()
        log("select_parse_failed", model=client.model if hasattr(client, 'model') else '?')
        return {
            "context": "",
            "selected_ids": [],
            "summarised_ids": [],
            "new_current": current,
            "web_search": "",
            "usage": usage,
            "error": "invalid JSON",
        }

    selected_ids = parsed.get("selected", [])
    summarised_map = parsed.get("summarised", {})
    new_current = parsed.get("current_node", current)

    _dbg("selector", f"selected={selected_ids} summarised={list(summarised_map.keys())} new_current={new_current}")
    log("select_done", selected=selected_ids, summarised=list(summarised_map.keys()),
        new_current=new_current, model=client.model if hasattr(client, 'model') else '?')

    subtree = db.get_subtree(selected_ids)

    # Collect branch labels that look like file paths (for enrichment)
    file_paths = []
    for bid, branch in subtree.items():
        label = branch.get("label", "")
        if label and not label.endswith("/") and label not in ("root", "files-cache", ""):
            file_paths.append(label)
    log("select_file_paths", count=len(file_paths), paths=file_paths[:20])

    context_parts = []
    for bid, branch in subtree.items():
        if bid in summarised_map:
            context_parts.append(f"[{bid}] {branch['label']} (podsumowane): {summarised_map[bid]}")
        else:
            text = branch["full_text"] or branch["summary"]
            context_parts.append(f"[{bid}] {branch['label']}: {text}")

    return {
        "context": "\n\n".join(context_parts),
        "selected_ids": selected_ids,
        "file_paths": file_paths,
        "summarised_ids": list(summarised_map.keys()) if isinstance(summarised_map, dict) else summarised_ids,
        "new_current": new_current,
        "web_search": parsed.get("web_search", ""),
        "usage": usage,
    }
