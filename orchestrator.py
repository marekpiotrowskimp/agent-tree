from llm_client import LLMClient, create_llm_client
from db import RegistryDB, SessionDB
from selector import select
from executor import execute, execute_stream, execute_with_tools
from updater import update
from websearch import search as web_search, format_results as format_web
from dotenv import find_dotenv, set_key
from log import log
from tools import get_workspace
from pathlib import Path
import httpx
import os
import threading
import time


class Orchestrator:
    def __init__(self):
        self.cheap_model = os.getenv("CHEAP_MODEL", "opencode/deepseek-v4-flash-free")
        self.expensive_model = os.getenv("EXPENSIVE_MODEL", "github-copilot/gpt-5.4")
        self.max_tokens = int(os.getenv("MAX_CONTEXT_TOKENS", "8000"))
        self.selector_max_tokens = int(os.getenv("SELECTOR_MAX_TOKENS", "16000"))
        self.updater_max_tokens = int(os.getenv("UPDATER_MAX_TOKENS", "8000"))

        self.cheap = create_llm_client(self.cheap_model)
        self.expensive = create_llm_client(self.expensive_model)
        self.registry = RegistryDB()
        self._env_path = find_dotenv(usecwd=False)
        log("orch_init", cheap=self.cheap_model, expensive=self.expensive_model)

    def new_session(self, name, description):
        uuid_str = self.registry.create_session(name, description)
        db = SessionDB(uuid_str, name, description)
        db.close()
        return uuid_str

    def get_session_db(self, session_name: str) -> SessionDB:
        session = self.registry.get_session(session_name)
        if not session:
            raise ValueError(f"Session '{session_name}' not found")
        return SessionDB(session["uuid"], session["name"], session["description"])

    def process(self, session_name, prompt, progress=None, display=None, on_tool_call=None, on_round_stats=None, abort_event=None):
        session = self.registry.get_session(session_name)
        if not session:
            log("proc_no_session", session=session_name)
            raise ValueError(f"Session '{session_name}' not found")

        db = SessionDB(session["uuid"], session["name"], session["description"])
        phase_times = {}
        select_usage = {"sent": 0, "received": 0}

        log("proc_start", session=session_name, prompt_len=len(prompt), prompt_preview=prompt[:80])

        try:
            tree = db.full_tree()
            current_branch = db.get_branch(db.current_node_id)
            skip_select = len(tree) <= 1 and (not current_branch or current_branch["id"] == "root")

            if skip_select:
                log("proc_skip_select", reason="single_node_tree")
                selected = {
                    "context": "",
                    "selected_ids": [],
                    "summarised_ids": [],
                    "new_current": db.current_node_id,
                    "web_search": "",
                    "usage": select_usage,
                }
                phase_times["select"] = 0
            else:
                if progress:
                    progress("select")
                log("proc_select_start", model=self.cheap_model, tree_size=len(tree))
                t0 = time.monotonic()
                selected = select(prompt, db, self.cheap, self.max_tokens, self.selector_max_tokens)
                phase_times["select"] = time.monotonic() - t0
                select_usage = selected.get("usage", {"sent": 0, "received": 0})
                log("proc_select_done", duration=phase_times["select"],
                    selected=len(selected.get("selected_ids",[])),
                    summarised=len(selected.get("summarised_ids",[])),
                    usage=select_usage, error=selected.get("error"))

            new_current = selected["new_current"]
            if new_current != db.current_node_id and db.get_branch(new_current):
                db.current_node_id = new_current

            context = selected["context"]

            # Enrich context with actual file contents from file_paths
            enrich_paths = selected.get("file_paths", [])
            if enrich_paths:
                log("proc_enrich_start", paths_count=len(enrich_paths))
                t0 = time.monotonic()
                ws = Path(get_workspace())
                enriched = []
                for fp in enrich_paths[:20]:
                    full_path = ws / fp
                    if full_path.is_file() and full_path.stat().st_size < 50000:
                        try:
                            content = full_path.read_text(encoding="utf-8", errors="replace")
                            enriched.append(f"--- {fp} ---\n{content[:3000]}")
                        except Exception:
                            pass
                if enriched:
                    context = context + "\n\n" + "\n\n".join(enriched)
                    log("proc_enrich_done", files=len(enriched), added_chars=sum(len(e) for e in enriched))
                else:
                    log("proc_enrich_empty")
                phase_times.setdefault("enrich", time.monotonic() - t0)
            else:
                log("proc_enrich_skip", reason="no_file_paths")

            web_query = selected.get("web_search", "")
            if web_query:
                if progress:
                    progress("websearch")
                log("proc_websearch_start", query=web_query)
                t0 = time.monotonic()
                try:
                    web_results = web_search(web_query)
                    web_text = format_web(web_results)
                    if web_text:
                        context = f"{context}\n\n{web_text}" if context else web_text
                    log("proc_websearch_done", chars=len(web_text) if web_text else 0)
                except Exception as e:
                    log("proc_websearch_error", error=str(e))
                phase_times["websearch"] = time.monotonic() - t0
            else:
                phase_times["websearch"] = 0

            execute_usage = {"sent": 0, "received": 0}
            touched_files = {}
            if display:
                if progress:
                    progress("execute")
                log("proc_execute_start", model=self.expensive_model, context_chars=len(context),
                    display=True, tools=True)
                t0 = time.monotonic()
                try:
                    result, execute_usage, touched_files = execute_with_tools(prompt, context, self.expensive,
                                                              display=display, on_tool_call=on_tool_call,
                                                              on_round_stats=on_round_stats,
                                                              abort_event=abort_event)
                except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.TimeoutException) as e:
                    log("proc_execute_fallback", reason=type(e).__name__, model=self.expensive_model)
                    result, execute_usage = execute(prompt, context, self.expensive)
                    display(result)
                phase_times["execute"] = time.monotonic() - t0
                log("proc_execute_done", duration=phase_times["execute"],
                    result_chars=len(result), usage=execute_usage,
                    touched_files=len(touched_files))
            else:
                if progress:
                    progress("execute")
                log("proc_execute_start", model=self.expensive_model, display=False, tools=False)
                t0 = time.monotonic()
                result, execute_usage = execute(prompt, context, self.expensive)
                phase_times["execute"] = time.monotonic() - t0
                log("proc_execute_done", duration=phase_times["execute"],
                    result_chars=len(result), usage=execute_usage)

            orchestrator_tokens = {
                "select_sent": select_usage["sent"],
                "select_received": select_usage["received"],
                "execute_sent": execute_usage.get("sent", 0),
                "execute_received": execute_usage.get("received", 0),
            }

            update_done = threading.Event()
            t = threading.Thread(
                target=self._run_update,
                args=(session["uuid"], session["name"], session["description"],
                      prompt, result, selected["selected_ids"],
                      selected["summarised_ids"], orchestrator_tokens,
                      update_done, touched_files),
                daemon=True,
            )
            t.start()

            total_sent = select_usage["sent"] + execute_usage.get("sent", 0)
            total_received = select_usage["received"] + execute_usage.get("received", 0)

            return {
                "result": result,
                "context": {
                    "selected_ids": selected["selected_ids"],
                    "summarised_ids": selected["summarised_ids"],
                    "phase_times": phase_times,
                    "error": selected.get("error"),
                },
                "usage": {"sent": total_sent, "received": total_received},
                "update_done": update_done,
            }
        finally:
            db.close()

    def _run_update(self, uuid, name, description, prompt, result, selected_ids, summarised_ids,
                    orchestrator_tokens=None, update_done=None, touched_files=None):
        client = create_llm_client(self.cheap_model)
        db = SessionDB(uuid, name, description)
        log("proc_update_start", session=name, cheap_model=self.cheap_model,
            prompt_len=len(prompt), result_len=len(result))
        try:
            update(prompt, result, db, client,
                   selected_ids=selected_ids,
                   summarised_ids=summarised_ids,
                   max_tree_tokens=self.updater_max_tokens,
                   orchestrator_tokens=orchestrator_tokens,
                   touched_files=touched_files or {})
            log("proc_update_done", session=name)
        except Exception as e:
            log("proc_update_error", session=name, error=str(e))
        finally:
            db.close()
            if update_done:
                update_done.set()

    def list_sessions(self):
        return self.registry.list_sessions()

    def get_tree(self, session_name):
        session = self.registry.get_session(session_name)
        if not session:
            raise ValueError(f"Session '{session_name}' not found")
        db = SessionDB(session["uuid"], session["name"], session["description"])
        try:
            tree = db.full_tree()
            current = db.current_node_id
            logs = db.get_logs(10)
            return {"tree": tree, "current_node": current, "session": session, "logs": logs}
        finally:
            db.close()

    def get_history(self, session_name, limit=15):
        session = self.registry.get_session(session_name)
        if not session:
            raise ValueError(f"Session '{session_name}' not found")
        db = SessionDB(session["uuid"], session["name"], session["description"])
        try:
            return db.get_logs(limit)
        finally:
            db.close()

    def get_token_stats(self, session_name):
        session = self.registry.get_session(session_name)
        if not session:
            raise ValueError(f"Session '{session_name}' not found")
        db = SessionDB(session["uuid"], session["name"], session["description"])
        try:
            return db.get_token_stats()
        finally:
            db.close()

    def set_cheap_model(self, model: str):
        log("set_model", role="cheap", model=model)
        new_cheap = create_llm_client(model)
        self.cheap = new_cheap
        self.cheap_model = model
        if self._env_path:
            try:
                set_key(str(self._env_path), "CHEAP_MODEL", model)
            except OSError:
                pass

    def set_expensive_model(self, model: str):
        log("set_model", role="expensive", model=model)
        new_expensive = create_llm_client(model)
        self.expensive = new_expensive
        self.expensive_model = model
        if self._env_path:
            try:
                set_key(str(self._env_path), "EXPENSIVE_MODEL", model)
            except OSError:
                pass

    def set_alias(self, alias, target):
        self.registry.set_alias(alias, target)

    def delete_session(self, name):
        self.registry.delete_session(name)
