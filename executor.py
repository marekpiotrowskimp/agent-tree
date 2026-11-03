import json
import time
import httpx
from pathlib import Path
from llm_client import LLMClient, Chunk, estimate_messages_tokens
from tools import TOOL_DEFS, execute_tool, get_workspace, _reset_question_count, _reset_tool_counts
from log import log

NO_TOOLS_PROVIDERS = {"opencode"}

TOOL_SYSTEM = """You are a coding assistant. All necessary file contents are already in the context (Kontekst zadania). Do NOT use read_file, glob, list_dir, or grep to explore. Only use write_file to save your output.

Available tools:
- write_file(path, content) — write/overwrite a file (path relative to workspace)
- read_file(path, offset?, limit?) — read a file (use only if content missing from context)
- edit_file(path, old, new) — find and replace TEXT in a file
- bash(command, timeout?, workdir?) — run a shell command
- webfetch(url, format?) — fetch web page content as text
- question(text, options?) — ask the user a clarifying question

Rules:
- All project files are already in the context below — do NOT call read_file, glob, or list_dir
- After analyzing the context, use write_file to save your result
- If a file does NOT exist, use write_file to CREATE it
- Never guess file contents — use what's provided in the context
- IMPORTANT: all file paths must be relative — never use absolute paths"""


MAX_TOOL_ROUNDS = 3

FILE_TOOLS = {"read_file", "write_file", "edit_file"}


def _record_file_touch(touched: dict, func_name: str, args: dict):
    """Update touched_files dict based on a file tool call."""
    if func_name not in FILE_TOOLS:
        return
    path = args.get("path", "")
    if not path:
        return
    if func_name == "read_file":
        action = "read"
    elif func_name == "write_file":
        action = "write"
    else:
        action = "edit"
    if path not in touched:
        touched[path] = {"actions": [], "count": 0}
    if action not in touched[path]["actions"]:
        touched[path]["actions"].append(action)
    touched[path]["count"] += 1


def execute(prompt: str, context: str, client: LLMClient):
    context_block = f"Kontekst zadania:\n{context}\n\n" if context else ""
    return client.ask(f"{context_block}{prompt}")


def execute_stream(prompt: str, context: str, client: LLMClient):
    context_block = f"Kontekst zadania:\n{context}\n\n" if context else ""
    yield from client.ask_stream(f"{context_block}{prompt}")


def execute_with_tools(prompt: str, context: str, client: LLMClient, display=None, on_tool_call=None, on_round_stats=None, abort_event=None):
    provider_id = getattr(client, 'provider_id', '')
    if provider_id in NO_TOOLS_PROVIDERS:
        log("exec_skip_tools", provider=provider_id, reason="bridge_only")
        return execute(prompt, context, client) + ({},)

    context_block = f"Kontekst zadania:\n{context}\n\n" if context else ""
    workspace_line = f"\nCurrent workspace: {get_workspace()}\nAll file paths are relative to this directory."
    messages = [
        {"role": "system", "content": TOOL_SYSTEM + workspace_line},
        {"role": "user", "content": f"{context_block}{prompt}"},
    ]

    _reset_question_count()
    _reset_tool_counts()

    total_tool_calls = 0
    cumulative_sent = 0
    cumulative_received = 0
    touched_files = {}

    log("exec_start", prompt_len=len(prompt), context_len=len(context), model=client.model if hasattr(client, 'model') else '?')

    # Quick file listing (limit 50, exclude generated dirs)
    try:
        ws = Path(get_workspace())
        EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "build", "dist"}
        candidates = []
        for f in ws.rglob('*'):
            if not f.is_file():
                continue
            rel = str(f.relative_to(ws))
            parts = rel.split("/")
            if any(p in EXCLUDE_DIRS for p in parts):
                continue
            if any(p.startswith(".") and p != "." for p in parts):
                continue
            candidates.append(rel)
        candidates.sort()
        file_list = "\n".join(candidates[:50])
        if len(candidates) > 50:
            file_list += f"\n... and {len(candidates) - 50} more"
        log("exec_workspace_files", total=len(candidates), listed=min(50, len(candidates)))
        if candidates:
            messages.insert(1, {"role": "system",
                "content": f"Project files ({min(50, len(candidates))} of {len(candidates)}):\n{file_list}"})
    except Exception as e:
        log("exec_workspace_scan_error", error=str(e))

    tool_calls = None
    received = 0
    text_parts = []
    last_call_keys = []
    repeat_count = 0

    for round_idx in range(MAX_TOOL_ROUNDS):
        if abort_event and abort_event.is_set():
            log("exec_aborted", round=round_idx, text_gathered=sum(len(t) for t in text_parts))
            return "".join(text_parts), {"sent": cumulative_sent, "received": cumulative_received}, touched_files

        sent_est = estimate_messages_tokens(messages)
        cumulative_sent += sent_est

        if on_round_stats:
            on_round_stats(round_idx + 1, MAX_TOOL_ROUNDS, sent_est, None)

        log("exec_round", round=round_idx+1, max_rounds=MAX_TOOL_ROUNDS, sent_est=sent_est, msg_count=len(messages))

        text_parts = []

        if hasattr(client, 'workspace'):
            client.workspace = str(get_workspace())
        try:
            for chunk in client.chat_stream(messages, TOOL_DEFS):
                if chunk.type == "text":
                    text_parts.append(chunk.content)
                    if display:
                        display(chunk.content)

                elif chunk.type == "tool_calls":
                    tool_calls = chunk.tool_calls

                elif chunk.type == "tool_call_start":
                    if on_tool_call:
                        on_tool_call(chunk.name, None, None)

                elif chunk.type == "done":
                    usage = chunk.usage or {}
                    received = usage.get("received", 0)
        except httpx.ReadTimeout:
            log("exec_round_timeout", round=round_idx+1, text_gathered=sum(len(t) for t in text_parts))

        cumulative_received += received

        if on_round_stats:
            on_round_stats(round_idx + 1, MAX_TOOL_ROUNDS, sent_est, received)

        log("exec_round_end", round=round_idx+1, tool_calls=len(tool_calls) if tool_calls else 0,
            text_chars=sum(len(t) for t in text_parts), received=received)

        if not tool_calls:
            result_text = "".join(text_parts)
            log("exec_done", total_tool_calls=total_tool_calls, rounds=round_idx+1,
                sent=cumulative_sent, received=cumulative_received)
            return result_text, {"sent": cumulative_sent, "received": cumulative_received}, touched_files

        for tc in tool_calls:
            total_tool_calls += 1
            func_id = tc.id or f"call_{round_idx}"
            func_name = tc.name
            try:
                args = json.loads(tc.arguments)
            except json.JSONDecodeError:
                args = {}

            log("exec_tool_call", name=func_name, args=args)

            _record_file_touch(touched_files, func_name, args)

            result = execute_tool(func_name, args)

            if on_tool_call:
                on_tool_call(func_name, args, result[:200])

            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": func_id,
                "content": result,
            })

        # Detect repeated identical tool call patterns (loop detection)
        current_keys = sorted((tc.name, tc.arguments.strip()) for tc in tool_calls)
        if current_keys == last_call_keys:
            repeat_count += 1
        else:
            repeat_count = 0
            last_call_keys = current_keys

        if repeat_count >= 3:
            log("exec_loop_detected", round=round_idx+1, tools=[tc.name for tc in tool_calls], repeats=repeat_count)
            result_text = "".join(text_parts)
            return result_text, {"sent": cumulative_sent, "received": cumulative_received}, touched_files

    log("exec_max_rounds", total_tool_calls=total_tool_calls, rounds=MAX_TOOL_ROUNDS)
    return "", {"sent": cumulative_sent, "received": cumulative_received}, touched_files
