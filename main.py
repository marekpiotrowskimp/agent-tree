import argparse
import json
import sys
from dotenv import load_dotenv
from orchestrator import Orchestrator

load_dotenv()

def print_tree(tree_data):
    tree = tree_data["tree"]
    current = tree_data["current_node"]
    session = tree_data["session"]

    print(f"\n🌳 Session: {session['name']}")
    print(f"   {session['description']}")
    print(f"   Created: {session['created_at']}")
    print()
    print("   Nodes:")
    for b in tree:
        marker = " ◄" if b["id"] == current else ""
        indent = "  " * b["depth"]
        compressed = " (compressed)" if b["compressed"] else ""
        print(f"  {indent}[{b['id']}] {b['label']}{compressed}{marker}")

    logs = tree_data.get("logs", [])
    if logs:
        print()
        print("   Recent queries:")
        for log in logs[:5]:
            selected = json.loads(log["selected_ids"]) if log["selected_ids"] else []
            new = json.loads(log["new_branches"]) if log["new_branches"] else []
            prompt_short = log["prompt"][:60] + "..." if len(log["prompt"]) > 60 else log["prompt"]
            print(f"  [{log['timestamp'][:19]}] \"{prompt_short}\" → {len(selected)} selected, {len(new)} new nodes")
    print()

def main():
    parser = argparse.ArgumentParser(description="Agent z drzewem kontekstu")
    sub = parser.add_subparsers(dest="command")

    new_p = sub.add_parser("new", help="Create new session")
    new_p.add_argument("name", help="Session name")
    new_p.add_argument("description", help="Session description")

    session_p = sub.add_parser("session", aliases=["s"], help="Run task in session")
    session_p.add_argument("name", nargs="?", help="Session name (default: last used)")
    session_p.add_argument("prompt", nargs="+", help="Task description")

    tree_p = sub.add_parser("tree", help="Show session tree")
    tree_p.add_argument("name", nargs="?", help="Session name (default: last used)")

    sub.add_parser("list", aliases=["ls"], help="List all sessions")

    alias_p = sub.add_parser("alias", help="Set alias for session")
    alias_p.add_argument("alias", help="Alias name")
    alias_p.add_argument("target", help="Target session name")

    delete_p = sub.add_parser("delete", help="Delete session")
    delete_p.add_argument("name", help="Session name")

    args = parser.parse_args()
    orch = Orchestrator()

    if args.command == "new":
        orch.new_session(args.name, args.description)
        print(f"✅ Session '{args.name}' created")

    elif args.command in ("session", "s"):
        name = args.name or orch.registry.last_session()
        if not name:
            session_list = orch.list_sessions()
            if session_list:
                name = session_list[0]["name"]
            else:
                print("❌ No sessions. Create one: python main.py new <name> <description>")
                return
        if isinstance(name, dict):
            name = name["name"]
        prompt = " ".join(args.prompt)
        print(f"🔍 Processing in session '{name}'...", flush=True)
        result = orch.process(name, prompt)
        print()
        print(result["result"])
        print()
        print(f"📊 Selected: {len(result['context']['selected_ids'])} branches", flush=True)
        if result["context"]["summarised_ids"]:
            print(f"📦 Summarised: {len(result['context']['summarised_ids'])} branches")

    elif args.command == "tree":
        name = args.name
        if not name:
            last = orch.registry.last_session()
            name = last["name"] if last else None
        if not name:
            print("❌ No sessions")
            return
        tree_data = orch.get_tree(name)
        print_tree(tree_data)

    elif args.command in ("list", "ls"):
        sessions = orch.list_sessions()
        if not sessions:
            print("No sessions found")
            return
        print(f"\n{'Name':<20} {'Description':<40} {'Last used':<20}")
        print("-" * 80)
        for s in sessions:
            desc = s["description"][:38] + ".." if len(s["description"]) > 38 else s["description"]
            last = s["last_used"][:19] if s["last_used"] else ""
            print(f"{s['name']:<20} {desc:<40} {last:<20}")
        print()

    elif args.command == "alias":
        orch.set_alias(args.alias, args.target)
        print(f"✅ Alias '{args.alias}' → '{args.target}'")

    elif args.command == "delete":
        orch.delete_session(args.name)
        print(f"✅ Session '{args.name}' deleted")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
