import os
import sys

if "-l" in sys.argv[1:] or "--log" in sys.argv[1:]:
    os.environ["AGENT_TREE_LOG"] = "1"

import signal

from dotenv import load_dotenv, set_key as dotenv_set_key, find_dotenv

load_dotenv()

from log import log as agent_log, close as log_close, init as log_init
from orchestrator import Orchestrator
from tools import set_question_callback, is_dangerous, set_workspace, get_workspace
from llm_client import estimate_tokens
from bridge_client import BRIDGE_URL
import httpx
import subprocess
import itertools
import json
import os
import readline
import sys
import shutil
import threading
import time
from pathlib import Path

REPL_HISTORY = Path(os.getenv("AGENT_TREE_DIR", "~/.config/agent-tree")).expanduser() / "repl_history.txt"
try:
    REPL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
try:
    readline.read_history_file(str(REPL_HISTORY))
except OSError:
    pass
readline.set_history_length(1000)

HEADER = "\033[95magent-tree\033[0m — Agent z Drzewem Kontekstu\n"

HELP = """\033[1mCommands:\033[0m
  /new <name> <description>     Create new session and switch to it
  /switch <name>                Switch active session
  /tree                         Show current session tree
  /list                         List all sessions
  /history [n]                  Show last n prompts (default 15)
  /tokens                       Show total token usage for this session
  /websearch <query>            Search the web (results not saved to tree)
  /workspace [path]             Show or change workspace directory
  /models [selector|executor] [provider]  List or change models, filter by provider
  /connect [provider [url] [key]]  Configure LLM provider (Ollama, DeepSeek, etc.)
  /init [path]                  Build context tree from existing project
  /alias <alias> <target>       Set session alias
  /delete <name>                Delete session
  /help                         Show this help
  /quit, /exit                  Exit

  Anything else is sent as a task prompt to the active session.
"""

PHASE_LABELS = {
    "select": "Selecting context",
    "websearch": "Searching the web",
    "execute": "Executing task",
}
PHASE_COLORS = {
    "select": "\033[95m",
    "websearch": "\033[96m",
    "execute": "\033[92m",
}
C_GRAY = "\033[90m"
C_YELLOW = "\033[93m"
C_RESET = "\033[0m"
SPINNER_FRAMES = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u280f"


class RotatingSpinner:
    def __init__(self):
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._phase = ""
        self._start_time = 0.0
        self._frames = itertools.cycle(SPINNER_FRAMES)
        self._extra = ""

    def start(self, phase: str, extra: str = ""):
        with self._lock:
            if self._running:
                self._stop_thread()
            self._phase = phase
            self._start_time = time.monotonic()
            self._extra = extra
            self._running = True
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def update(self, extra: str):
        with self._lock:
            self._extra = extra

    def stop(self):
        with self._lock:
            if self._running:
                self._stop_thread()
                self._clear_line()

    def _stop_thread(self):
        self._running = False

    def _clear_line(self):
        w = shutil.get_terminal_size().columns
        sys.stderr.write(f"\r{' ' * w}\r")
        sys.stderr.flush()

    def _spin(self):
        while True:
            with self._lock:
                if not self._running:
                    break
                elapsed = time.monotonic() - self._start_time
                frame = next(self._frames)
                label = PHASE_LABELS.get(self._phase, self._phase)
                color = PHASE_COLORS.get(self._phase, "")
                extra = f"  {self._extra}" if self._extra else ""
                line = f"\r  {frame} {color}{label}{C_RESET} ({elapsed:.0f}s){extra}  "
            sys.stderr.write(line)
            sys.stderr.flush()
            time.sleep(0.15)


_spinner = RotatingSpinner()
_round_info = ""
_stream_started = False
_stream_char_count = 0


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def _trunc(s, n):
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _format_args(args, max_len=60):
    parts = []
    for k, v in list(args.items())[:3]:
        vs = str(v)
        if len(vs) > max_len:
            vs = vs[:max_len - 1] + "\u2026"
        parts.append(f"{k}={vs}")
    return ", ".join(parts)


def _format_result_preview(result, max_len=70):
    first_line = result.split("\n")[0].strip()
    return first_line[:max_len]


def on_round_stats(round_idx: int, total: int, sent: int, received: int | None):
    global _round_info, _stream_started
    if received is not None:
        _round_info = f"R{round_idx}/{total}, \u2197{_fmt_tokens(sent)}/\u2198{_fmt_tokens(received)}"
    else:
        _round_info = f"R{round_idx}/{total}, \u2197{_fmt_tokens(sent)}"
        if not _spinner._running and not _stream_started:
            _spinner.start("execute", _round_info)
            return
    _spinner.update(_round_info)


def show_tool_call(name, args, result_preview):
    global _stream_started
    _stream_started = False
    if _spinner._running:
        _spinner.stop()
    icon = ""
    if name == "bash" and args and is_dangerous(args.get("command", "")):
        icon = f" {C_YELLOW}\u26a0{C_RESET}"
    elif name == "question":
        icon = " ?"
    elif name == "todowrite":
        icon = " #"
    elif name.startswith("read") or name == "grep" or name == "glob" or name == "list_dir":
        icon = f" {C_GRAY}\u25b6{C_RESET}"
    elif name in ("write_file", "edit_file"):
        icon = f" \u270e"
    if args is None:
        sys.stderr.write(f"  {icon}  {name}(\u2026)\n")
    else:
        args_str = _format_args(args)
        result_str = _format_result_preview(result_preview) if result_preview else ""
        sys.stderr.write(f"  {icon}  {name}({args_str})  {C_GRAY}\u2026 {result_str}{C_RESET}\n")
    sys.stderr.flush()


def print_tree(tree_data):
    tree = tree_data["tree"]
    current = tree_data["current_node"]
    session = tree_data["session"]
    name = session.get("name", "?")
    desc = session.get("description", "")
    print(f"\n  \033[1m{name}\033[0m  \u2014  {desc}")
    print(f"  Current node: {current}")
    if not tree:
        print("  (empty)")
    for b in tree:
        marker = " \u25c4" if b["id"] == current else ""
        indent = "  " * b["depth"]
        compressed = " (compressed)" if b["compressed"] else ""
        label = _trunc(b["label"], 60)
        print(f"  {indent}[{b['id']}] {label}{compressed}{marker}")
    print()


def _update_watcher(update_done):
    pass  # unused — tree update message is now handled in main loop


def print_result(result):
    global _stream_char_count
    text = result["result"]
    print()
    if text:
        print(text)
    print()
    ctx = result.get("context", {})
    n_selected = len(ctx.get("selected_ids", []))
    n_summarised = len(ctx.get("summarised_ids", []))
    phase_times = ctx.get("phase_times", {})
    usage = result.get("usage", {})
    selector_error = ctx.get("error")

    if selector_error:
        sys.stdout.write(f"  {C_YELLOW}⚠ Selector failed: {selector_error} — odpowiedź bez kontekstu drzewa{C_RESET}\n")
        sys.stdout.flush()

    parts = []
    for phase in ("select", "websearch", "execute"):
        t = phase_times.get(phase)
        if t is not None and t > 0:
            color = PHASE_COLORS.get(phase, "")
            label = {"select": "Sel", "websearch": "Web", "execute": "Exe"}.get(phase, phase)
            parts.append(f"{color}{label} {t:.1f}s{C_RESET}")
    if n_selected:
        parts.append(f"{C_GRAY}\u2502 {n_selected} selected{C_RESET}")
    if n_summarised:
        parts.append(f"{C_GRAY}\u2502 {n_summarised} summarised{C_RESET}")
    if usage:
        sent_str = _fmt_tokens(usage.get("sent", 0))
        received_str = _fmt_tokens(usage.get("received", 0))
        parts.append(f"{C_GRAY}\u2502 \u2197{sent_str} sent / \u2198{received_str} received{C_RESET}")
    parts.append(f"{C_GRAY}\u2514 updating tree in background{C_RESET}")
    print("  " + "  ".join(parts))
    print()
    _stream_char_count = 0


def print_history(logs):
    if not logs:
        print("  No history")
        return
    print()
    for log in logs:
        ts = (log.get("timestamp") or "")[:16]
        prompt = log["prompt"][:60] + ("\u2026" if len(log["prompt"]) > 60 else "")
        sel = json.loads(log["selected_ids"]) if log["selected_ids"] else []
        summ = json.loads(log["summarised_ids"]) if log["summarised_ids"] else []
        newb = json.loads(log["new_branches"]) if log["new_branches"] else []
        tsent = log.get("tokens_sent") or 0
        trecv = log.get("tokens_received") or 0
        meta = ""
        if sel:
            meta += f" \u25b8{len(sel)}"
        if summ:
            meta += f" \u25bd{len(summ)}"
        if newb:
            meta += f" +{len(newb)}"
        if tsent or trecv:
            meta += f"  {C_GRAY}\u2197{_fmt_tokens(tsent)}/\u2198{_fmt_tokens(trecv)}{C_RESET}"
        print(f"  {ts}  {prompt}{' ' + meta if meta else ''}")
    print()


def print_sessions(sessions):
    if not sessions:
        print("  No sessions found")
        return
    w = shutil.get_terminal_size().columns
    name_w = max(len(s["name"]) for s in sessions) + 2
    for s in sessions:
        desc = _trunc(s["description"], w - name_w - 24)
        last = s.get("last_used", "")[:16] if s.get("last_used") else ""
        print(f"  \033[1m{s['name']}\033[0m  {desc:<{w - name_w - 24}} {last}")
    print()


def _format_options(opts):
    lines = []
    for i, opt in enumerate(opts, 1):
        label = opt.get("label", str(opt)) if isinstance(opt, dict) else opt
        lines.append(f"    {i}. {label}")
    return "\n".join(lines)


def _repl_question(text, options):
    sys.stdout.write(f"\n  {C_YELLOW}? Agent asks:{C_RESET}\n    {text}\n")
    if options:
        sys.stdout.write(_format_options(options) + "\n")
    sys.stdout.flush()
    try:
        answer = input(f"  > ")
    except (KeyboardInterrupt, EOFError):
        return "(user cancelled)"
    return answer


def _bridge_healthy(bridge_url: str) -> bool:
    try:
        resp = httpx.get(f"{bridge_url}/health", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


_SERVER_URL = os.getenv("OPENCODE_SERVER", "http://localhost:4096")


def _fetch_available_models() -> dict[str, list[str]]:
    """Return {provider_id: [model_id, ...]} for all connected providers."""
    result: dict[str, list[str]] = {}

    # Try bridge /v1/models first
    try:
        resp = httpx.get(f"{BRIDGE_URL}/v1/models", timeout=5.0)
        data = resp.json()
        models = data.get("data", [])
        for m in models:
            mid = m.get("id", "")
            if "/" in mid:
                pid, _, _ = mid.partition("/")
                result.setdefault(pid, []).append(mid[len(pid) + 1:])
    except Exception:
        pass

    # Fallback: try OpenCode directly
    if not result:
        try:
            resp = httpx.get(f"{_SERVER_URL}/provider", timeout=5.0)
            data = resp.json()
            connected = set(data.get("connected", []))
            for p in data.get("all", []):
                pid = p.get("id", "")
                if pid not in connected:
                    continue
                models = list(p.get("models", {}).keys())
                if models:
                    result[pid] = models
        except Exception:
            pass

    # Scan configured providers from environment (*_BASE_URL)
    configured_providers = _scan_configured_providers()
    for pid, cfg in configured_providers.items():
        if pid in result:
            continue
        try:
            headers = {}
            if cfg.get("api_key"):
                headers["Authorization"] = f"Bearer {cfg['api_key']}"
            resp = httpx.get(f"{cfg['base_url']}/models", headers=headers, timeout=5.0)
            models_data = resp.json()
            model_ids = [m.get("id", "") for m in models_data.get("data", []) if m.get("id")]
            if model_ids:
                result[pid] = model_ids
        except Exception:
            pass

    # Ensure common providers have at least some models listed
    if "opencode" not in result:
        result["opencode"] = ["deepseek-v4-flash-free", "deepseek-v4-flash-thinking"]
    if "github-copilot" not in result:
        result["github-copilot"] = ["gpt-4o", "claude-sonnet-4-5", "gpt-5-mini"]

    return result


def _scan_configured_providers() -> dict[str, dict]:
    """Scan .env for '*_BASE_URL' entries. Returns {provider: {base_url, api_key}}."""
    result = {}
    exclude = {"OPENCODE", "OPENCODE_BRIDGE", "LLM", "AGENT_TREE"}
    for key, value in os.environ.items():
        if not key.endswith("_BASE_URL"):
            continue
        prefix = key[:-9]
        if prefix in exclude:
            continue
        pid = prefix.lower().replace("_", "-")
        api_key = os.getenv(f"{prefix}_API_KEY") or os.getenv("LLM_API_KEY", "")
        result[pid] = {"base_url": value, "api_key": api_key}
    return result


def cmd_models(orch, role_arg: str | None, provider_filter: str | None = None):
    """Handle /models [selector|executor] [provider]."""
    available = _fetch_available_models()

    def _print_current():
        sys.stdout.write(f"\n  Selector:  {C_GRAY}{orch.cheap_model}{C_RESET}\n")
        sys.stdout.write(f"  Executor:  {C_GRAY}{orch.expensive_model}{C_RESET}\n")

    def _show_all_models():
        if available:
            sys.stdout.write("\n  Available models:\n")
            for pid, models in available.items():
                sys.stdout.write(f"  \033[1m{pid}\033[0m\n")
                for m in models:
                    sys.stdout.write(f"    {m}\n")
        else:
            sys.stdout.write(f"  {C_YELLOW}Could not fetch models from OpenCode{C_RESET}\n")
        sys.stdout.write("\n")
        sys.stdout.flush()

    if not role_arg:
        _print_current()
        _show_all_models()
        return

    role = role_arg.lower()
    if role not in ("selector", "executor"):
        _print_current()
        _show_all_models()
        return

    current = orch.cheap_model if role == "selector" else orch.expensive_model

    # Build flat list — filter by provider if specified
    if provider_filter:
        pid = provider_filter.lower()
        all_models = [f"{p}/{m}" for p, models in available.items()
                      for m in models if p == pid]
        if not all_models:
            sys.stdout.write(f"  Provider '{pid}' not found or has no models\n")
            sys.stdout.write(f"  Available: {', '.join(available.keys())}\n\n")
            sys.stdout.flush()
            return
    else:
        all_models = [f"{pid}/{m}" for pid, models in available.items() for m in models]

    if not all_models:
        print("  Could not fetch models from OpenCode")
        return

    sys.stdout.write(f"\n  Select model for \033[1m{role}\033[0m{' (\033[1m' + provider_filter + '\033[0m only)' if provider_filter else ''}:\n")
    for i, m in enumerate(all_models, 1):
        marker = f"  {C_GRAY}(current){C_RESET}" if m == current else ""
        sys.stdout.write(f"    {C_GRAY}{i:2}.{C_RESET} {m}{marker}\n")
    sys.stdout.write("\n")
    sys.stdout.flush()

    try:
        raw = input(f"  Enter number or model id > ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return

    if not raw:
        return

    # Accept number or full model id
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(all_models):
            chosen = all_models[idx]
        else:
            print(f"  Invalid number: {raw}")
            return
    elif "/" in raw:
        chosen = raw
    else:
        print(f"  Invalid input — enter a number or provider/model")
        return

    try:
        if role == "selector":
            orch.set_cheap_model(chosen)
            sys.stdout.write(f"  Selector → \033[1m{chosen}\033[0m\n\n")
        else:
            orch.set_expensive_model(chosen)
            sys.stdout.write(f"  Executor → \033[1m{chosen}\033[0m\n\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f"  {C_YELLOW}Error changing {role} model: {e}{C_RESET}\n\n")
        sys.stdout.flush()


def cmd_connect(orch, provider: str | None, arg2: str | None, arg3: str | None):
    """
    Handle /connect [provider [base_url] [api_key]]

    /connect                           — list all configured providers
    /connect copilot                   — show Copilot config
    /connect copilot <url> [key]       — configure provider
    /connect copilot off               — remove provider config
    """
    env_path = find_dotenv(usecwd=False)

    if not provider:
        configured = _scan_configured_providers()
        if not configured:
            sys.stdout.write("  No providers configured. Usage:\n")
            sys.stdout.write("    /connect <provider> [base_url] [api_key]\n\n")
            sys.stdout.write("  Examples:\n")
            sys.stdout.write("    /connect copilot https://api.githubcopilot.com ghu_xxx\n")
            sys.stdout.write("    /connect ollama http://localhost:11434/v1\n")
            sys.stdout.write("    /connect deepseek https://api.deepseek.com/v1 sk-xxx\n")
            sys.stdout.write("    /connect groq https://api.groq.com/openai/v1 gsk-xxx\n")
            sys.stdout.write("    /connect openrouter https://openrouter.ai/api/v1 sk-xxx\n")
            sys.stdout.flush()
            return
        sys.stdout.write("  Configured providers:\n")
        for pid, cfg in configured.items():
            key_hint = cfg['api_key'][:8] + '...' if cfg['api_key'] else 'no key'
            sys.stdout.write(f"    \033[1m{pid}\033[0m  {cfg['base_url']}  ({key_hint})\n")
        sys.stdout.write("\n  Use /connect <provider> [url] [key] to add/change\n")
        sys.stdout.flush()
        return

    if arg2 == "off":
        sys.stdout.write(f"  Removing {provider} config...\n")
        if env_path:
            try:
                dotenv_set_key(str(env_path), f"{provider.upper().replace('-', '_')}_BASE_URL", "")
            except OSError:
                pass
        sys.stdout.write("  Done (set empty URL to disable; remove the line manually from .env)\n")
        sys.stdout.flush()
        return

    preset_urls = {
        "copilot": "https://api.githubcopilot.com",
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": "http://localhost:11434/v1",
        "groq": "https://api.groq.com/openai/v1",
        "lmstudio": "http://localhost:1234/v1",
        "together": "https://api.together.xyz/v1",
        "mistral": "https://api.mistral.ai/v1",
        "anthropic": "https://api.anthropic.com",
    }

    base_url = arg2 or preset_urls.get(provider, "")
    api_key = arg3 or ""
    env_prefix = provider.upper().replace("-", "_")

    if not base_url:
        sys.stdout.write(f"  No default URL for '{provider}'. Provide URL:\n")
        sys.stdout.write(f"    /connect {provider} <base_url> [api_key]\n")
        sys.stdout.flush()
        return

    if not api_key and provider not in ("ollama", "lmstudio"):
        if env_path:
            api_key = os.getenv(f"{env_prefix}_API_KEY") or os.getenv("LLM_API_KEY", "")

    if not env_path:
        sys.stdout.write(f"  {C_YELLOW}Warning: no .env found, config saved for this session only{C_RESET}\n")
        sys.stdout.flush()

    # Test connection first
    sys.stdout.write(f"  Testing {provider} at {base_url}...")
    sys.stdout.flush()
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = httpx.get(f"{base_url}/models", headers=headers, timeout=10.0)
        models_data = resp.json()
        model_ids = [m.get("id", "") for m in models_data.get("data", []) if m.get("id")]
        sys.stdout.write(f" \033[32mOK\033[0m ({len(model_ids)} models)\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f" \033[31mFAILED\033[0m ({e})\n")
        sys.stdout.write(f"  Check the URL and try again\n")
        sys.stdout.flush()
        return

    # Save to .env
    if env_path:
        try:
            dotenv_set_key(str(env_path), f"{env_prefix}_BASE_URL", base_url)
            if api_key:
                dotenv_set_key(str(env_path), f"{env_prefix}_API_KEY", api_key)
            os.environ[f"{env_prefix}_BASE_URL"] = base_url
            if api_key:
                os.environ[f"{env_prefix}_API_KEY"] = api_key
        except OSError:
            pass

    sys.stdout.write(f"  Connected to \033[1m{provider}\033[0m\n")
    if model_ids:
        sys.stdout.write("  Models:\n")
        for m in model_ids[:10]:
            sys.stdout.write(f"    {m}\n")
        if len(model_ids) > 10:
            sys.stdout.write(f"    ... and {len(model_ids) - 10} more\n")
    sys.stdout.write("\n  Use /models to select a model\n")
    sys.stdout.flush()


def cmd_init(orch, active: str | None, path_arg: str | None):
    """Handle /init [path]."""
    from initializer import init_project, _collect_files

    if not active:
        print("  No active session. Create one: /new <name> <description>")
        return

    if path_arg:
        project_path = Path(path_arg).expanduser().resolve()
    else:
        project_path = Path(get_workspace()).resolve()

    if not project_path.is_dir():
        print(f"  Not a directory: {project_path}")
        return

    db = orch.get_session_db(active)

    sys.stdout.write(f"\n  Scanning {C_GRAY}{project_path}{C_RESET}...\n")
    sys.stdout.flush()

    files = _collect_files(project_path)
    total = len(files)
    truncated_scan = total >= 200

    if truncated_scan:
        sys.stdout.write(f"  {C_YELLOW}Warning: ≥200 files found — only first 200 analysed.{C_RESET}\n")
    else:
        sys.stdout.write(f"  Found {total} file(s)\n")
    sys.stdout.flush()

    _phase_labels = {
        "triage": "[1/3] Triage",
        "quick":  "[2/3] Indexing",
        "deep":   "[3/3] Analysing",
    }
    _current_phase = [None]

    def phase_cb(phase, current, total_p, label):
        prefix = _phase_labels.get(phase, phase)
        if phase != _current_phase[0]:
            # clear previous progress line and print phase header
            sys.stdout.write("\r" + " " * 80 + "\r")
            _current_phase[0] = phase
        if phase == "triage":
            sys.stdout.write(f"\r  {prefix} — {C_GRAY}{label}{C_RESET}")
        elif phase == "quick":
            sys.stdout.write(f"\r  {prefix} {C_GRAY}{label}{C_RESET}  [{current}/{total_p}]")
        else:  # deep
            sys.stdout.write(f"\r  {prefix} {C_GRAY}{label}{C_RESET}  [{current}/{total_p}]")
        sys.stdout.flush()

    result = init_project(project_path, db, orch.cheap, phase_cb=phase_cb)

    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()

    if result["status"] == "already_init":
        print(f"  Project '{result['project']}' already initialised in this session.")
        print(f"  Delete the session and create a new one to re-initialise.")
    elif result["status"] == "error":
        print(f"  Error: {result['message']}")
    else:
        added = result["added"]
        skipped = result["skipped"]
        key_count = result.get("key_count", 0)
        truncated = result["truncated"]
        msg = f"  Done — {added} node(s) added ({key_count} with deep analysis)"
        if skipped:
            msg += f", {skipped} skipped"
        if truncated:
            msg += f" (truncated at 200 files)"
        print(msg + ".")
        print(f"  Project: \033[1m{result['project']}\033[0m")
        set_workspace(project_path)
        print(f"  Workspace → {C_GRAY}{project_path}{C_RESET}\n")



def check_bridge():
    """Auto-start bridge if not running. Show instructions if not installed."""
    bridge_url = BRIDGE_URL
    bridge_path = Path(os.getenv("BRIDGE_PATH", "~/opencode-bridge")).expanduser()
    bridge_js = bridge_path / "bridge.js"

    if _bridge_healthy(bridge_url):
        return

    # Not running — check if installed
    if not bridge_js.exists():
        sys.stdout.write(f"\n  \033[93m⚠ Bridge nie jest zainstalowany.\033[0m\n\n")
        sys.stdout.write(f"  Instalacja:\n")
        sys.stdout.write(f"    \033[1mgit clone https://github.com/crazyboy24/opencode-proxy {bridge_path}\033[0m\n")
        sys.stdout.write(f"    \033[1mcd {bridge_path} && npm install\033[0m\n\n")
        sys.stdout.write(f"  Następnie uruchom ponownie agent-tree.\n\n")
        sys.stdout.flush()
        return

    # Installed but not running — start automatically
    port = bridge_url.rstrip("/").split(":")[-1]
    sys.stdout.write(f"  {C_GRAY}Uruchamiam bridge (port {port})...{C_RESET}\n")
    sys.stdout.flush()

    env = {**os.environ, "PORT": port}
    try:
        subprocess.Popen(
            ["node", str(bridge_js)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        sys.stdout.write(f"  \033[93m⚠ Nie znaleziono 'node'. Zainstaluj Node.js: https://nodejs.org\033[0m\n\n")
        sys.stdout.flush()
        return

    # Poll until healthy (max 6s)
    for _ in range(12):
        time.sleep(0.5)
        if _bridge_healthy(bridge_url):
            sys.stdout.write(f"  {C_GRAY}Bridge gotowy.{C_RESET}\n")
            sys.stdout.flush()
            return

    sys.stdout.write(f"  \033[93m⚠ Bridge nie odpowiada po 6s — zapytania mogą nie działać.\033[0m\n\n")
    sys.stdout.flush()


def main():
    global _stream_started, _stream_char_count, _round_info
    set_workspace(Path.cwd())

    log_init()
    agent_log("repl_main_start", cwd=str(Path.cwd()))

    orch = Orchestrator()
    set_question_callback(_repl_question)

    sessions = orch.list_sessions()
    active = None
    if sessions:
        active = sessions[0]["name"]

    print(HEADER)
    if os.getenv("AGENT_TREE_LOG"):
        print(f"  Log: {C_GRAY}~/.agent-tree/logs/{C_RESET}")
    print(f"  Selector: {C_GRAY}{orch.cheap_model}{C_RESET}  |  Executor: {C_GRAY}{orch.expensive_model}{C_RESET}")
    check_bridge()
    print("  Type /help for commands, /quit to exit")
    print(f"  Workspace: {C_GRAY}{get_workspace()}{C_RESET}")
    if active:
        print(f"  Active session: \033[1m{active}\033[0m")
    print()

    readline.clear_history()

    _pending_update = None  # threading.Event from last orch.process()

    while True:
        # Print "Tree updated" BEFORE the prompt so prompt is always last
        if _pending_update is not None:
            _pending_update.wait(timeout=0)  # non-blocking check
            if _pending_update.is_set():
                sys.stdout.write(f"  {C_GRAY}\u2514 Tree updated{C_RESET}\n")
                sys.stdout.flush()
                _pending_update = None

        try:
            label = active or "no session"
            prompt = input(f"\033[36m({label})\033[0m > ")
        except (KeyboardInterrupt, EOFError):
            print()
            agent_log("repl_eof", event="keyboard_interrupt")
            break

        text = prompt.strip()
        if not text:
            continue

        if text == "/quit" or text == "/exit":
            agent_log("repl_command", cmd=text)
            break

        if text.startswith("/"):
            parts = text.split(maxsplit=2) if text.count(" ") >= 2 else text.split(maxsplit=1)
            cmd = parts[0]
            arg1 = parts[1] if len(parts) > 1 else None
            arg2 = parts[2] if len(parts) > 2 else None
            agent_log("repl_command", cmd=cmd, arg1=arg1, arg2=arg2)

            if cmd == "/help":
                print(HELP)

            elif cmd == "/new":
                if not arg1:
                    print("  Usage: /new <name> <description>")
                    continue
                name = arg1
                desc = arg2 or ""
                try:
                    orch.new_session(name, desc)
                    active = name
                    print(f"  Created and switched to \033[1m{active}\033[0m")
                except ValueError as e:
                    print(f"  Error: {e}")

            elif cmd == "/switch":
                if not arg1:
                    print("  Usage: /switch <name>")
                    continue
                target = arg1
                try:
                    s = orch.registry.get_session(target)
                    if s:
                        active = target
                        print(f"  Switched to \033[1m{active}\033[0m")
                    else:
                        print(f"  Session '{target}' not found")
                except Exception:
                    print(f"  Session '{target}' not found")

            elif cmd == "/tree":
                if not active:
                    print("  No active session")
                    continue
                try:
                    data = orch.get_tree(active)
                    print_tree(data)
                except ValueError as e:
                    print(f"  Error: {e}")

            elif cmd == "/list":
                print_sessions(orch.list_sessions())

            elif cmd == "/history":
                if not active:
                    print("  No active session")
                    continue
                n = int(arg1) if arg1 and arg1.isdigit() else 15
                try:
                    logs = orch.get_history(active, n)
                    print_history(logs)
                except ValueError as e:
                    print(f"  Error: {e}")

            elif cmd == "/tokens":
                if not active:
                    print("  No active session")
                    continue
                try:
                    stats = orch.get_token_stats(active)
                    print(f"\n  {C_GRAY}\u2197{_fmt_tokens(stats['total_sent'])} sent  \u2198{_fmt_tokens(stats['total_received'])} received  across {stats['prompts']} prompts{C_RESET}\n")
                except ValueError as e:
                    print(f"  Error: {e}")

            elif cmd == "/websearch":
                if not arg1:
                    print("  Usage: /websearch <query>")
                    continue
                from websearch import search, format_results
                print("  Searching...", end="", flush=True)
                try:
                    results = search(arg1)
                    print("\r" + " " * 40 + "\r", end="", flush=True)
                    if results:
                        print("\n" + format_results(results))
                    else:
                        print("  No results")
                except Exception as e:
                    print(f"\r  Error: {e}")

            elif cmd == "/workspace":
                if arg1:
                    new_ws = Path(arg1).expanduser().resolve()
                    try:
                        set_workspace(new_ws)
                        print(f"  Workspace changed to: {C_GRAY}{get_workspace()}{C_RESET}")
                    except Exception as e:
                        print(f"  Error: {e}")
                else:
                    print(f"  Workspace: {C_GRAY}{get_workspace()}{C_RESET}")

            elif cmd == "/models":
                cmd_models(orch, arg1, arg2)

            elif cmd == "/connect":
                parts = text.split(maxsplit=3)
                p_provider = parts[1] if len(parts) > 1 else None
                p_url = parts[2] if len(parts) > 2 else None
                p_key = parts[3] if len(parts) > 3 else None
                cmd_connect(orch, p_provider, p_url, p_key)

            elif cmd == "/init":
                cmd_init(orch, active, arg1)

            elif cmd == "/alias":
                if not arg1 or not arg2:
                    print("  Usage: /alias <alias> <target>")
                    continue
                try:
                    orch.set_alias(arg1, arg2)
                    print(f"  Alias '{arg1}' \u2192 '{arg2}'")
                except ValueError as e:
                    print(f"  Error: {e}")

            elif cmd == "/delete":
                if not arg1:
                    print("  Usage: /delete <name>")
                    continue
                try:
                    orch.delete_session(arg1)
                    if arg1 == active:
                        remaining = orch.list_sessions()
                        active = remaining[0]["name"] if remaining else None
                    print(f"  Deleted '{arg1}'")
                except ValueError as e:
                    print(f"  Error: {e}")

            else:
                print(f"  Unknown command: {cmd}. Try /help")

            continue

        if not active:
            print("  No active session. Create one: /new <name> <description>")
            agent_log("repl_no_active_session")
            continue

        agent_log("repl_execute_prompt", session=active, prompt=text, prompt_len=len(text))

        abort_event = threading.Event()
        old_handler = signal.signal(signal.SIGINT, lambda s, f: abort_event.set())

        try:
            global _stream_started, _stream_char_count
            _stream_started = False
            _stream_char_count = 0
            _round_info = ""

            _spinner.start("execute")

            def display(token):
                global _stream_started, _stream_char_count
                if not _stream_started:
                    _stream_started = True
                    _spinner.stop()
                sys.stdout.write(token)
                sys.stdout.flush()
                _stream_char_count += len(token)

            result = orch.process(
                active, text,
                progress=lambda p: _spinner.start(p),
                display=display,
                on_tool_call=show_tool_call,
                on_round_stats=on_round_stats,
                abort_event=abort_event,
            )

            if abort_event.is_set():
                _spinner.stop()
                sys.stdout.write(f"{C_YELLOW}\n  Aborted by user{C_RESET}\n")
                sys.stdout.flush()
                signal.signal(signal.SIGINT, old_handler)
                continue

            if _stream_started:
                sys.stdout.write("\n")
                sys.stdout.flush()
            else:
                _spinner.stop()

            usage = result.get("usage", {})
            agent_log("repl_execute_done", session=active, result_chars=len(result.get("result", "")),
                      sent=usage.get("sent", 0), received=usage.get("received", 0))
            print_result(result)
            _pending_update = result.get("update_done")

        except Exception as e:
            _spinner.stop()
            agent_log("repl_execute_error", session=active, error=str(e))
            print(f"  Error: {e}")
        finally:
            signal.signal(signal.SIGINT, old_handler)

    _spinner.stop()
    agent_log("repl_shutdown")
    log_close()
    try:
        readline.write_history_file(str(REPL_HISTORY))
    except OSError:
        pass


if __name__ == "__main__":
    main()
