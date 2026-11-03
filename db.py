import sqlite3
import json
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path

AGENT_TREE_DIR = Path(os.path.expanduser(os.getenv("AGENT_TREE_DIR", "~/.config/agent-tree")))
SESSIONS_DIR = AGENT_TREE_DIR / "sessions"
REGISTRY_PATH = AGENT_TREE_DIR / "registry.db"

AGENT_TREE_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

def _json(val):
    return json.dumps(val) if val else "[]"

def _now():
    return datetime.now(timezone.utc).isoformat()

class RegistryDB:
    def __init__(self):
        self.conn = sqlite3.connect(str(REGISTRY_PATH))
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                uuid TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                root_node TEXT NOT NULL DEFAULT 'root',
                created_at TEXT NOT NULL,
                last_used TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT PRIMARY KEY,
                target TEXT NOT NULL REFERENCES sessions(name)
            );
            CREATE INDEX IF NOT EXISTS idx_aliases_target ON aliases(target);
        """)
        self.conn.commit()

    def create_session(self, name, description):
        existing = self.conn.execute(
            "SELECT uuid FROM sessions WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"Session '{name}' already exists")

        session_uuid = str(uuid.uuid4())
        now = _now()
        self.conn.execute(
            "INSERT INTO sessions (uuid, name, description, root_node, created_at, last_used) VALUES (?, ?, ?, 'root', ?, ?)",
            (session_uuid, name, description, now, now),
        )
        self.conn.commit()
        return session_uuid

    def get_session(self, name):
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE name = ?", (name,)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE sessions SET last_used = ? WHERE name = ?",
                (_now(), name),
            )
            self.conn.commit()
            return dict(row)
        alias = self.conn.execute(
            "SELECT target FROM aliases WHERE alias = ?", (name,)
        ).fetchone()
        if alias:
            return self.get_session(alias["target"])
        return None

    def last_session(self):
        row = self.conn.execute(
            "SELECT * FROM sessions ORDER BY last_used DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self):
        rows = self.conn.execute(
            "SELECT name, description, last_used FROM sessions ORDER BY last_used DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_alias(self, alias, target):
        existing = self.conn.execute(
            "SELECT name FROM sessions WHERE name = ?", (target,)
        ).fetchone()
        if not existing:
            raise ValueError(f"Session '{target}' not found")
        self.conn.execute(
            "INSERT OR REPLACE INTO aliases (alias, target) VALUES (?, ?)",
            (alias, target),
        )
        self.conn.commit()

    def delete_session(self, name):
        session = self.get_session(name)
        if not session:
            raise ValueError(f"Session '{name}' not found")
        db_path = SESSIONS_DIR / f"{session['uuid']}.db"
        if db_path.exists():
            db_path.unlink()
        self.conn.execute("DELETE FROM sessions WHERE name = ?", (name,))
        self.conn.execute("DELETE FROM aliases WHERE target = ?", (name,))
        self.conn.commit()

class SessionDB:
    def __init__(self, uuid_str, name, description=""):
        self.uuid = uuid_str
        self.path = SESSIONS_DIR / f"{uuid_str}.db"
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init(name, description)

    def _init(self, name, description):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS branches (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                full_text TEXT,
                summary TEXT NOT NULL,
                parent_id TEXT REFERENCES branches(id),
                depth INTEGER DEFAULT 0,
                compressed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS keywords (
                branch_id TEXT REFERENCES branches(id) ON DELETE CASCADE,
                keyword TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS branches_fts USING fts5(label, summary, content='branches');
            CREATE TABLE IF NOT EXISTS session_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt TEXT NOT NULL,
                selected_ids TEXT NOT NULL DEFAULT '[]',
                summarised_ids TEXT NOT NULL DEFAULT '[]',
                new_branches TEXT NOT NULL DEFAULT '[]',
                current_node TEXT,
                tokens_sent INTEGER DEFAULT 0,
                tokens_received INTEGER DEFAULT 0,
                timestamp TEXT DEFAULT (datetime('now'))
            );
        """)
        meta = self.conn.execute(
            "SELECT value FROM sessions_meta WHERE key = 'initialized'"
        ).fetchone()
        if not meta:
            self.conn.execute(
                "INSERT OR REPLACE INTO sessions_meta VALUES (?, ?)",
                ("initialized", "1"),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO sessions_meta VALUES (?, ?)",
                ("current_node_id", "root"),
            )
            self.conn.execute(
                "INSERT INTO branches (id, label, full_text, summary, depth) VALUES ('root', ?, '', ?, 0)",
                (name, description),
            )
            self.conn.commit()

    @property
    def current_node_id(self):
        row = self.conn.execute(
            "SELECT value FROM sessions_meta WHERE key = 'current_node_id'"
        ).fetchone()
        return row["value"] if row else "root"

    @current_node_id.setter
    def current_node_id(self, node_id):
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions_meta VALUES ('current_node_id', ?)",
            (node_id,),
        )
        self.conn.commit()

    def add_branch(self, label, full_text, summary, keywords, parent_id=None, depth=None):
        branch_id = f"b_{uuid.uuid4().hex[:8]}"
        if parent_id is None:
            parent_id = self.current_node_id
        parent = self.get_branch(parent_id)
        parent_depth = parent["depth"] if parent else 0
        depth = depth if depth is not None else parent_depth + 1

        self.conn.execute(
            "INSERT INTO branches (id, label, full_text, summary, parent_id, depth) VALUES (?, ?, ?, ?, ?, ?)",
            (branch_id, label, full_text, summary, parent_id, depth),
        )
        for kw in keywords:
            self.conn.execute(
                "INSERT INTO keywords (branch_id, keyword) VALUES (?, ?)",
                (branch_id, kw),
            )
        self.conn.execute(
            "INSERT INTO branches_fts (rowid, label, summary) VALUES (last_insert_rowid(), ?, ?)",
            (label, summary),
        )
        self.conn.commit()
        return branch_id

    def compress_branch(self, branch_id, new_summary):
        self.conn.execute(
            "UPDATE branches SET full_text = NULL, summary = ?, compressed = 1 WHERE id = ?",
            (new_summary, branch_id),
        )
        self.conn.execute(
            "INSERT INTO branches_fts (rowid, label, summary) VALUES ((SELECT rowid FROM branches WHERE id = ?), ?, ?)",
            (branch_id, "", new_summary),
        )
        self.conn.commit()

    def get_branch(self, branch_id):
        row = self.conn.execute(
            "SELECT * FROM branches WHERE id = ?", (branch_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        kw_rows = self.conn.execute(
            "SELECT keyword FROM keywords WHERE branch_id = ?", (branch_id,)
        ).fetchall()
        result["keywords"] = [r["keyword"] for r in kw_rows]
        return result

    def get_children(self, branch_id):
        rows = self.conn.execute(
            "SELECT * FROM branches WHERE parent_id = ? ORDER BY created_at",
            (branch_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_subtree(self, branch_ids):
        selected = set(branch_ids)
        result = {}
        for bid in branch_ids:
            branch = self.get_branch(bid)
            if branch:
                result[bid] = branch
                pid = branch["parent_id"]
                while pid and pid not in selected:
                    parent = self.get_branch(pid)
                    if parent:
                        result[pid] = parent
                        selected.add(pid)
                    pid = parent["parent_id"] if parent else None
        return result

    def search_branches(self, keywords, limit=20):
        if not keywords:
            return []
        query = " OR ".join(f'"{kw}"' for kw in keywords)
        try:
            rows = self.conn.execute(
                f"SELECT b.* FROM branches b JOIN branches_fts fts ON b.rowid = fts.rowid WHERE branches_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def full_tree(self):
        branches = self.conn.execute(
            "SELECT * FROM branches ORDER BY depth, created_at"
        ).fetchall()
        return [dict(b) for b in branches]

    def log(self, prompt, selected_ids, summarised_ids, new_branches, tokens_sent=0, tokens_received=0):
        self.conn.execute(
            "INSERT INTO session_log (prompt, selected_ids, summarised_ids, new_branches, current_node, tokens_sent, tokens_received) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (prompt, _json(selected_ids), _json(summarised_ids), _json(new_branches), self.current_node_id, tokens_sent, tokens_received),
        )
        self.conn.commit()

    def get_logs(self, limit=20):
        rows = self.conn.execute(
            "SELECT * FROM session_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_token_stats(self):
        row = self.conn.execute(
            "SELECT COUNT(*) as prompts, COALESCE(SUM(tokens_sent), 0) as total_sent, COALESCE(SUM(tokens_received), 0) as total_received FROM session_log"
        ).fetchone()
        return {
            "prompts": row["prompts"],
            "total_sent": row["total_sent"],
            "total_received": row["total_received"],
        }

    def delete_branch(self, branch_id):
        if branch_id == "root":
            raise ValueError("Cannot delete root node")

        parent = self.conn.execute(
            "SELECT parent_id FROM branches WHERE id = ?", (branch_id,)
        ).fetchone()
        parent_id = parent["parent_id"] if parent else None

        to_delete = set()
        queue = [branch_id]
        while queue:
            cur = queue.pop()
            if cur in to_delete:
                continue
            to_delete.add(cur)
            for c in self.conn.execute(
                "SELECT id FROM branches WHERE parent_id = ?", (cur,)
            ).fetchall():
                queue.append(c["id"])

        for bid in to_delete:
            self.conn.execute("DELETE FROM keywords WHERE branch_id = ?", (bid,))
            self.conn.execute(
                "DELETE FROM branches_fts WHERE rowid = (SELECT rowid FROM branches WHERE id = ?)",
                (bid,),
            )
            self.conn.execute("DELETE FROM branches WHERE id = ?", (bid,))

        if self.current_node_id in to_delete:
            self.current_node_id = parent_id or "root"

        self.conn.commit()
        return len(to_delete)

    def get_branch_by_label(self, label: str, parent_id: str | None = None) -> dict | None:
        if parent_id is not None:
            row = self.conn.execute(
                "SELECT * FROM branches WHERE label = ? AND parent_id = ?",
                (label, parent_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM branches WHERE label = ? AND parent_id IS NULL",
                (label,),
            ).fetchone()
        return dict(row) if row else None

    def update_branch_summary(self, branch_id: str, summary: str, keywords: list):
        self.conn.execute(
            "UPDATE branches SET summary = ? WHERE id = ?",
            (summary, branch_id),
        )
        self.conn.execute("DELETE FROM keywords WHERE branch_id = ?", (branch_id,))
        for kw in keywords:
            self.conn.execute(
                "INSERT INTO keywords (branch_id, keyword) VALUES (?, ?)",
                (branch_id, kw),
            )
        self.conn.execute(
            "INSERT INTO branches_fts (rowid, label, summary) VALUES ((SELECT rowid FROM branches WHERE id = ?), ?, ?)",
            (branch_id, "", summary),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
