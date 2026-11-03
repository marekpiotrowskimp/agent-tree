# agent-tree

An LLM agent with a **context tree**: a cheap model builds and selects the context, a strong model does the actual work.

```
prompt → Selector (cheap LLM, FTS5 search over the tree) → Executor (strong LLM, selected branches only)
       → Updater (cheap LLM, appends new branches, saves to SQLite)
```

## Features

- Context tree per session in SQLite (append-only, automatic summarization of oversized branches)
- Agent tools: read/write files in the workspace, web search
- Model routing through [OpenCode](https://opencode.ai) — by default `opencode/deepseek-v4-flash-free` (cheap) and `github-copilot/gpt-5.4` (strong), configurable in `.env`
- Three interfaces: CLI (`main.py`), interactive REPL (`repl.py`) and a web panel (`web.py`)

Architecture details (in Polish): [PROJEKT.md](PROJEKT.md).

## Setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # models, context limits, OpenCode server/bridge, DeepSeek API key
```

## Usage

```sh
python main.py new <name> "<description>"   # create a session
python main.py session <name> "<task>"      # run a task in a session (alias: s)
python main.py tree [<name>]                # show the session tree
python main.py list                         # list sessions (alias: ls)
python main.py alias <alias> <session>      # set an alias
python main.py delete <name>                # delete a session

python repl.py                              # interactive REPL
python web.py                               # web panel
```

## License

MIT — see [LICENSE](LICENSE).
