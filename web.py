#!/usr/bin/env python3
import json
import os
import sys
import http.server
import shutil
import re
from urllib.parse import urlparse
from dotenv import load_dotenv
from db import RegistryDB, SessionDB

load_dotenv()

PORT = int(os.getenv("WEB_PORT", "8080"))


def api_list_sessions():
    reg = RegistryDB()
    sessions = reg.list_sessions()
    result = []
    for s in sessions:
        uuid = reg.get_session(s["name"])["uuid"]
        result.append({"name": s["name"], "description": s["description"],
                        "last_used": s.get("last_used", ""), "uuid": uuid})
    return json.dumps(result)


def api_session_tree(name):
    reg = RegistryDB()
    session = reg.get_session(name)
    if not session:
        return json.dumps({"error": "not found"}), 404

    db = SessionDB(session["uuid"], session["name"], session["description"])
    try:
        branches = db.full_tree()
        current = db.current_node_id
        logs = db.get_logs(50)

        branch_list = []
        for b in branches:
            branch_list.append({
                "id": b["id"],
                "label": b["label"],
                "summary": b["summary"],
                "full_text": b.get("full_text"),
                "depth": b["depth"],
                "compressed": bool(b["compressed"]),
                "parent_id": b["parent_id"],
                "keywords": b.get("keywords", []),
                "created_at": b.get("created_at", ""),
            })

        log_list = []
        for log in logs:
            log_list.append({
                "prompt": log["prompt"],
                "selected_ids": json.loads(log["selected_ids"]) if log["selected_ids"] else [],
                "summarised_ids": json.loads(log["summarised_ids"]) if log["summarised_ids"] else [],
                "new_branches": json.loads(log["new_branches"]) if log["new_branches"] else [],
                "current_node": log.get("current_node", ""),
                "timestamp": log.get("timestamp", ""),
                "tokens_sent": log.get("tokens_sent") or 0,
                "tokens_received": log.get("tokens_received") or 0,
            })

        return json.dumps({
            "uuid": session["uuid"],
            "name": session["name"],
            "description": session["description"],
            "current_node": current,
            "branches": branch_list,
            "logs": log_list,
        }), 200
    finally:
        db.close()


def api_delete_branch(session_name, branch_id):
    reg = RegistryDB()
    session = reg.get_session(session_name)
    if not session:
        return json.dumps({"error": "session not found"}), 404
    db = SessionDB(session["uuid"], session["name"], session["description"])
    try:
        if branch_id == "root":
            return json.dumps({"error": "cannot delete root"}), 400
        count = db.delete_branch(branch_id)
        return json.dumps({"ok": True, "deleted": count}), 200
    except ValueError as e:
        return json.dumps({"error": str(e)}), 400
    except Exception as e:
        return json.dumps({"error": str(e)}), 500
    finally:
        db.close()


def serve_index():
    return INDEX_HTML


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, data, mime="text/html; charset=utf-8", status=200):
        data = data.encode("utf-8") if isinstance(data, str) else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, data, status=200):
        self._send(data, "application/json", status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._send(serve_index())
        elif path == "/api/sessions":
            self._json(api_list_sessions())
        elif path.startswith("/api/sessions/"):
            name = path[len("/api/sessions/"):]
            data, status = api_session_tree(name)
            self._json(data, status)
        else:
            self._send("Not found", status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        m = re.match(r'/api/sessions/([^/]+)/branches/([^/]+)', path)
        if m:
            name, branch_id = m.groups()
            data, status = api_delete_branch(name, branch_id)
            self._json(data, status)
        else:
            self._send("Not found", status=404)

    def log_message(self, fmt, *args):
        pass


def main():
    host = os.getenv("WEB_HOST", "0.0.0.0")
    print(f"  agent-tree web UI at http://localhost:{PORT}")
    print()
    server = http.server.HTTPServer((host, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        server.shutdown()


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>agent-tree</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;height:100vh;display:flex;overflow:hidden}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:#161b22}
::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}

/* sidebar */
#sidebar{width:280px;min-width:280px;background:#161b22;border-right:1px solid #30363d;display:flex;flex-direction:column;overflow-y:auto}
#sidebar h1{padding:16px 20px;font-size:15px;font-weight:600;color:#f0f6fc;border-bottom:1px solid #30363d;letter-spacing:.3px}
#sidebar h1 span{color:#58a6ff}
#session-list{padding:8px;flex:1}
.session-btn{display:block;width:100%;text-align:left;padding:10px 12px;margin-bottom:4px;background:#0d1117;border:1px solid #21262d;border-radius:6px;color:#c9d1d9;cursor:pointer;font-size:13px;transition:.15s}
.session-btn:hover{border-color:#30363d;background:#1c2128}
.session-btn.active{border-color:#58a6ff;background:#1f3140}
.session-btn .s-desc{font-size:11px;color:#8b949e;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.session-btn .s-time{font-size:10px;color:#484f58}

/* detail panel */
#detail{display:none;padding:16px;border-top:1px solid #30363d;background:#0d1117;max-height:40vh;overflow-y:auto}
#detail.show{display:block}
#detail h3{font-size:13px;color:#f0f6fc;margin-bottom:8px}
#detail .d-field{margin-bottom:10px}
#detail .d-label{font-size:10px;text-transform:uppercase;color:#8b949e;letter-spacing:.5px;margin-bottom:2px}
#detail .d-value{font-size:13px;color:#c9d1d9;word-break:break-word}
#detail .d-value.code{font-family:ui-monospace,monospace;font-size:12px;background:#161b22;padding:8px;border-radius:4px;max-height:200px;overflow-y:auto;white-space:pre-wrap}
#detail .d-value.keywords{display:flex;gap:4px;flex-wrap:wrap}
#detail .kw{background:#1f3140;color:#58a6ff;padding:2px 6px;border-radius:3px;font-size:11px}
#detail .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}
#detail .badge.current{background:#d29922;color:#0d1117}
#detail .badge.compressed{background:#30363d;color:#8b949e}
.d-delete{margin-top:12px;padding:6px 14px;background:#3d0000;color:#ff6b6b;border:1px solid #660000;border-radius:4px;cursor:pointer;font-size:12px;transition:.15s;width:100%;text-align:center}
.d-delete:hover{background:#520000;border-color:#aa0000}
.d-delete:disabled{opacity:.4;cursor:not-allowed}

/* main area */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#header{padding:12px 24px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
#header h2{font-size:16px;font-weight:600;color:#f0f6fc}
#header .h-desc{font-size:12px;color:#8b949e;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#header .h-current{font-size:11px;color:#484f58;background:#161b22;padding:2px 8px;border-radius:4px}
#header .h-zoom{font-size:10px;color:#484f58;margin-left:auto}
.tool-btn{font-size:10px;padding:3px 8px;border-radius:3px;border:1px solid #30363d;background:#161b22;color:#8b949e;cursor:pointer;transition:.15s;display:inline-flex;align-items:center;gap:3px}
.tool-btn:hover{border-color:#58a6ff;color:#58a6ff}
#mode-toggle{font-size:11px;padding:4px 10px;border-radius:4px;border:1px solid #30363d;background:#161b22;color:#8b949e;cursor:pointer;transition:.15s}
#mode-toggle:hover{border-color:#58a6ff;color:#58a6ff}
#mode-toggle.active{background:#1f3140;border-color:#58a6ff;color:#58a6ff}
#viz{flex:1;overflow:hidden;position:relative;cursor:grab}
#viz:active{cursor:grabbing}
#viz svg{width:100%;height:100%;display:block}

/* tree nodes */
.node circle{stroke:#30363d;stroke-width:1.5;fill:#1c2128;cursor:pointer;transition:fill .15s,stroke .15s,r .15s}
.node circle:hover{stroke:#58a6ff;fill:#1f3140;r:7}
.node circle.current{stroke:#d29922;fill:#3d2b00;stroke-width:3}
.node circle.path-ancestor{stroke:#d29922;fill:#2d2100;stroke-width:2}
.node circle.compressed{stroke-dasharray:3,2}
.node circle.has-hidden{stroke-dasharray:5,2}
.node text{font-size:11px;fill:#c9d1d9;font-family:system-ui,-apple-system,sans-serif}
.node text.path-text{fill:#f0f6fc;font-weight:500}
.node text.id-label{font-size:9px;fill:#484f58}
.node text.id-label.path-text{fill:#d29922}
.node text.hidden-count{font-size:9px;fill:#484f58;font-style:italic}
.node .collapse-icon{font-size:9px;fill:#f0f6fc;font-weight:700;pointer-events:none}
.link{fill:none;stroke:#30363d;stroke-width:1.5;transition:stroke .15s,stroke-width .15s}
.link.path-link{stroke:#d29922;stroke-width:2.5}

/* log panel */
#log{display:none;border-top:1px solid #30363d;background:#161b22;flex-shrink:0;max-height:180px;overflow-y:auto}
#log.show{display:block}
#log h3{font-size:12px;color:#8b949e;padding:8px 16px;border-bottom:1px solid #30363d;font-weight:500}
.log-row{padding:6px 16px;border-bottom:1px solid #21262d;font-size:12px;display:flex;gap:12px;align-items:baseline}
.log-row .l-time{color:#484f58;font-size:10px;flex-shrink:0;font-family:ui-monospace,monospace}
.log-row .l-prompt{color:#c9d1d9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.log-row .l-meta{color:#8b949e;font-size:10px;flex-shrink:0;display:flex;gap:6px;align-items:baseline}
.log-row .l-meta .sel{color:#58a6ff}
.log-row .l-meta .sum{color:#d29922}
.log-row .l-meta .new{color:#3fb950}
.log-row .l-meta .tokens{color:#484f58;font-family:ui-monospace,monospace;font-size:9px}

#empty{display:flex;align-items:center;justify-content:center;flex:1;color:#484f58;font-size:14px}
#empty.show{display:flex}
#empty.show~#viz,#empty.show~#log{display:none}
#viz,#log,#detail,#empty{display:none}
</style>
</head>
<body>

<div id="sidebar">
  <h1><span>agent</span>-tree</h1>
  <div id="session-list"></div>
  <div id="detail"></div>
</div>

<div id="main">
  <div id="header">
    <h2 id="h-name">—</h2>
    <span class="h-desc" id="h-desc"></span>
    <span class="h-current" id="h-current"></span>
    <button class="tool-btn" onclick="collapseAll()">&#x2503; Collapse</button>
    <button class="tool-btn" onclick="expandAll()">&#x252b; Expand</button>
    <button id="mode-toggle" onclick="toggleTreeMode()">Full Tree</button>
    <span class="h-zoom">scroll = zoom, drag = pan</span>
  </div>
  <div id="empty" class="show">Select a session from the sidebar</div>
  <div id="viz"><svg></svg></div>
  <div id="log"></div>
</div>

<script>
const COLORS = d3.scaleSequential(d3.interpolateViridis).domain([0, 8]);

let currentSession = null;
let treeMode = "full";
let collapsedIds = new Set();

function fmt(n) {
  if (!n) return "0";
  return n >= 1000 ? (n/1000).toFixed(1) + "k" : String(n);
}

function loadSessions() {
  fetch("/api/sessions")
    .then(r => r.json())
    .then(sessions => {
      const list = document.getElementById("session-list");
      list.innerHTML = "";
      if (sessions.length === 0) {
        list.innerHTML = '<div style="padding:16px;color:#484f58;font-size:13px">No sessions</div>';
        return;
      }
      sessions.forEach(s => {
        const btn = document.createElement("button");
        btn.className = "session-btn";
        btn.innerHTML = `<div>${s.name}</div><div class="s-desc">${esc(s.description)}</div><div class="s-time">${s.last_used.slice(0,16)}</div>`;
        btn.onclick = () => loadSession(s.name);
        list.appendChild(btn);
      });
      loadSession(sessions[0].name);
    });
}

function loadSession(name) {
  collapsedIds.clear();
  document.querySelectorAll(".session-btn").forEach(b => b.classList.remove("active"));
  const btns = document.querySelectorAll(".session-btn");
  btns.forEach(b => { if (b.children[0].textContent === name) b.classList.add("active"); });

  fetch("/api/sessions/" + encodeURIComponent(name))
    .then(r => r.json())
    .then(data => {
      if (data.error) return;
      currentSession = data;
      document.getElementById("h-name").textContent = data.name;
      document.getElementById("h-desc").textContent = data.description;
      document.getElementById("h-current").textContent = "current: " + data.current_node;
      document.getElementById("empty").classList.remove("show");
      document.getElementById("viz").style.display = "block";
      document.getElementById("detail").classList.remove("show");
      renderTree(data);
      renderLog(data.logs || []);
    });
}

function buildHierarchy(branches) {
  const map = {};
  branches.forEach(b => map[b.id] = {...b, children: []});
  let root = null;
  branches.forEach(b => {
    if (b.parent_id && map[b.parent_id]) {
      map[b.parent_id].children.push(map[b.id]);
    } else if (!b.parent_id) {
      root = map[b.id];
    }
  });
  return root;
}

function getPathSet(branches, currentId) {
  const map = {};
  branches.forEach(b => map[b.id] = b);
  const path = new Set();
  let cur = currentId;
  while (cur && map[cur]) {
    path.add(cur);
    cur = map[cur].parent_id;
  }
  path.add("root");
  return path;
}

function getChildCount(branches, parentId) {
  let count = 0;
  const queue = [parentId];
  while (queue.length) {
    const cur = queue.shift();
    branches.forEach(b => {
      if (b.parent_id === cur) {
        count++;
        queue.push(b.id);
      }
    });
  }
  return count;
}

function toggleTreeMode() {
  const btn = document.getElementById("mode-toggle");
  treeMode = treeMode === "full" ? "path" : "full";
  btn.textContent = treeMode === "full" ? "Full Tree" : "Current Path";
  btn.classList.toggle("active", treeMode === "path");
  if (currentSession) renderTree(currentSession);
}

function collapseAll() {
  if (!currentSession) return;
  collapsedIds = new Set(currentSession.branches.filter(b => b.id !== "root").map(b => b.id));
  if (currentSession) renderTree(currentSession);
}

function expandAll() {
  collapsedIds = new Set();
  if (currentSession) renderTree(currentSession);
}

function renderTree(data) {
  const svg = d3.select("#viz svg");
  svg.on(".zoom", null);
  svg.selectAll("*").remove();

  if (!data.branches || data.branches.length === 0) {
    svg.append("text").attr("x","50%").attr("y","50%").attr("text-anchor","middle")
       .attr("fill","#484f58").attr("font-size","14px").text("Empty tree");
    return;
  }

  const root = buildHierarchy(data.branches);
  if (!root) return;

  const pathSet = data.current_node
    ? getPathSet(data.branches, data.current_node)
    : new Set();

  const width = document.getElementById("viz").clientWidth || 800;
  const height = document.getElementById("viz").clientHeight || 600;

  const treeLayout = d3.tree().size([height - 80, width - 200]).separation((a,b) => (a.parent == b.parent ? 1 : 1.2));
  const treeRoot = d3.hierarchy(root);

  function applyCollapsedIds(node) {
    if (!node.children) return;
    if (collapsedIds.has(node.data.id)) {
      node._children = node.children;
      node.children = null;
    } else {
      node.children.forEach(c => applyCollapsedIds(c));
    }
  }
  (treeRoot.children || []).forEach(c => applyCollapsedIds(c));

  if (treeMode === "path" && data.current_node) {
    function collapseIfNotOnPath(node) {
      if (!node.children) return;
      const hasPathChild = (n) => pathSet.has(n.data.id) || (n.children || []).some(c => hasPathChild(c));
      const keep = pathSet.has(node.data.id) || (node.children || []).some(c => hasPathChild(c));
      if (!keep && node.data.id !== "root") {
        if (node.children) { node._children = node.children; node.children = null; }
      } else {
        node.children.forEach(c => collapseIfNotOnPath(c));
      }
    }
    (treeRoot.children || []).forEach(c => collapseIfNotOnPath(c));
  }
  treeLayout(treeRoot);

  const g = svg.append("g");

  const zoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on("zoom", (event) => {
      g.attr("transform", event.transform);
    });
  svg.call(zoom);

  const initialTransform = d3.zoomIdentity.translate(80, 40);
  svg.call(zoom.transform, initialTransform);

  g.selectAll(".link")
    .data(treeRoot.links())
    .join("path")
    .attr("class", d => pathSet.has(d.target.data.id) && pathSet.has(d.source.data.id) ? "link path-link" : "link")
    .attr("d", d3.linkHorizontal()
      .x(d => d.y)
      .y(d => d.x));

  const node = g.selectAll(".node")
    .data(treeRoot.descendants())
    .join("g")
    .attr("class", "node")
    .attr("transform", d => `translate(${d.y},${d.x})`)
    .style("cursor","pointer")
    .on("click", (e, d) => {
      e.stopPropagation();
      showDetail(d.data);
    });

  node.append("circle")
    .attr("r", d => d.data.id === "root" ? 8 : (d.children || d._children) ? 7 : 5)
    .on("click", (e, d) => {
      e.stopPropagation();
      if (d.data.id === "root") return;
      if (collapsedIds.has(d.data.id)) collapsedIds.delete(d.data.id);
      else collapsedIds.add(d.data.id);
      renderTree(data);
    })
    .attr("class", d => {
      let cls = "";
      if (d.data.id === data.current_node) cls += " current";
      else if (pathSet.has(d.data.id) && d.data.id !== "root") cls += " path-ancestor";
      if (d.data.compressed) cls += " compressed";
      if (d._children && d._children.length) cls += " has-hidden";
      return cls;
    })
    .style("fill", d => {
      let c;
      if (d.data.id === "root") c = "#58a6ff";
      else if (d.data.id === data.current_node) c = "#d29922";
      else if (pathSet.has(d.data.id)) c = "#584000";
      else c = COLORS(d.data.depth);
      if (d._children && d._children.length) return c + "77";
      return c;
    });

node.append("title").text(d => d.data.label + (d.data.summary ? "\n" + d.data.summary : ""));

node.filter(d => d.children || d._children)
    .append("text")
    .attr("class", "collapse-icon")
    .attr("text-anchor", "middle")
    .attr("dy", ".33em")
    .text(d => d.children ? "\u2212" : "+");

node.append("text")
    .attr("dx", d => d.data.id === "root" ? 14 : 12)
    .attr("dy", 4)
    .attr("class", d => pathSet.has(d.data.id) ? "path-text" : null)
    .text(d => d.data.label.length > 50 ? d.data.label.slice(0,48) + "\u2026" : d.data.label);

  node.append("text")
    .attr("dx", d => d.data.id === "root" ? 14 : 12)
    .attr("dy", 16)
    .attr("class", d => "id-label" + (pathSet.has(d.data.id) ? " path-text" : ""))
    .text(d => d.data.id === "root" ? "" : d.data.id);

  node.filter(d => d._children && d._children.length)
    .append("text")
    .attr("dx", d => d.data.id === "root" ? 14 : 12)
    .attr("dy", 26)
    .attr("class", "hidden-count")
    .text(d => "(" + d._children.length + " hidden)");
}

function toggleCollapseAll() {
  if (collapsedIds.size > 0) expandAll();
  else collapseAll();
}

function showDetail(branch) {
  const detail = document.getElementById("detail");
  detail.classList.add("show");
  detail.innerHTML = `
    <h3>${esc(branch.label)}</h3>
    <div class="d-field">
      <div class="d-label">ID</div>
      <div class="d-value">${esc(branch.id)} ${branch.id === (currentSession||{}).current_node ? '<span class="badge current">current</span>' : ''} ${branch.compressed ? '<span class="badge compressed">compressed</span>' : ''}</div>
    </div>
    <div class="d-field">
      <div class="d-label">Depth</div>
      <div class="d-value">${branch.depth}</div>
    </div>
    <div class="d-field">
      <div class="d-label">Summary</div>
      <div class="d-value code">${esc(branch.summary || "(empty)")}</div>
    </div>
    ${branch.full_text ? `<div class="d-field"><div class="d-label">Full Text</div><div class="d-value code">${esc(branch.full_text)}</div></div>` : ''}
    ${branch.keywords && branch.keywords.length ? `<div class="d-field"><div class="d-label">Keywords</div><div class="d-value keywords">${branch.keywords.map(k => '<span class="kw">'+esc(k)+'</span>').join('')}</div></div>` : ''}
    ${branch.id !== 'root' ? `<button class="d-delete" onclick="deleteBranch('${esc(branch.id)}')">&times; Delete this branch</button>` : ''}
  `;
}

function deleteBranch(branchId) {
  const branch = currentSession.branches.find(b => b.id === branchId);
  if (!branch) return;
  const count = getChildCount(currentSession.branches, branchId);
  const msg = count > 0
    ? "Delete '" + branch.label + "' and all " + count + " descendants?"
    : "Delete '" + branch.label + "'?";
  if (!confirm(msg)) return;

  const btn = document.querySelector(".d-delete");
  if (btn) btn.disabled = true;

  fetch("/api/sessions/" + encodeURIComponent(currentSession.name) + "/branches/" + encodeURIComponent(branchId), { method: "DELETE" })
    .then(r => r.json())
    .then(res => {
      if (res.ok) {
        loadSession(currentSession.name);
      } else {
        alert(res.error);
        if (btn) btn.disabled = false;
      }
    });
}

function renderLog(logs) {
  const panel = document.getElementById("log");
  if (!logs.length) { panel.classList.remove("show"); return; }
  panel.classList.add("show");
  panel.innerHTML = "<h3>Session Log</h3>" + logs.slice().reverse().map(l => `
    <div class="log-row">
      <span class="l-time">${(l.timestamp||"").slice(0,16)}</span>
      <span class="l-prompt">${esc(l.prompt.slice(0,80))}${l.prompt.length>80?'\u2026':''}</span>
      <span class="l-meta">
        ${l.selected_ids.length ? `<span class="sel">\u25b8${l.selected_ids.length}</span>` : ''}
        ${l.summarised_ids.length ? `<span class="sum">\u25bd${l.summarised_ids.length}</span>` : ''}
        ${l.new_branches.length ? `<span class="new">+${l.new_branches.length}</span>` : ''}
        ${l.tokens_sent || l.tokens_received ? `<span class="tokens">\u2197${fmt(l.tokens_sent)}/\u2198${fmt(l.tokens_received)}</span>` : ''}
      </span>
    </div>
  `).join("");
}

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

window.addEventListener("resize", () => {
  if (currentSession) renderTree(currentSession);
});

loadSessions();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
