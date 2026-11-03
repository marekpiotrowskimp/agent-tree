import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


_FILE = None
_ENABLED = False


def init(filename=None):
    global _FILE, _ENABLED
    if not os.getenv("AGENT_TREE_LOG"):
        _ENABLED = False
        return
    _ENABLED = True
    log_dir = Path.home() / ".agent-tree" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if filename:
        path = log_dir / filename
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"agent_tree_{ts}.jsonl"
    _FILE = open(path, "a", buffering=1)
    _log("init", log_path=str(path))


def _log(event, **data):
    if not _ENABLED or _FILE is None:
        return
    try:
        record = {"t": f"{time.monotonic():.3f}", "e": event, "ts": datetime.now().isoformat(), **data}
        _FILE.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def log(event, **data):
    _log(event, **data)


def close():
    global _FILE, _ENABLED
    if _FILE:
        try:
            _log("close")
            _FILE.close()
        except Exception:
            pass
        _FILE = None
    _ENABLED = False
