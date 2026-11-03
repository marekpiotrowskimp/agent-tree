# Agent z Drzewem Kontekstu

System łączy tani LLM (DeepSeek V4 Flash Free) i drogi LLM (Copilot Pro) w sekwencji: tani model buduje i selekcjonuje drzewo kontekstu, drogi model wykonuje właściwe zadanie z optymalnym kontekstem.

## Architektura

```
Prompt użytkownika
       │
       ▼
┌──────────────────────────────────────────────┐
│  DEEPSEEK V4 FLASH FREE (Selector)           │
│  • przeszukuje drzewo (FTS5)                 │
│  • wybiera gałęzie pasujące do prompta       │
│  • jeśli > context window → podsumowuje      │
│  • zwraca: { selected, summarised, context } │
└───────────────────┬──────────────────────────┘
                    │ wybrany kontekst
                    ▼
┌──────────────────────────────────────────────┐
│  COPILOT PRO MODEL (Executor)                │
│  • otrzymuje tylko powiązane gałęzie         │
│  • wykonuje zadanie z precyzyjnym kontekstem  │
└───────────────────┬──────────────────────────┘
                    │ odpowiedź
                    ▼
┌──────────────────────────────────────────────┐
│  DEEPSEEK V4 FLASH FREE (Updater)            │
│  • parsuje odpowiedź                         │
│  • dodaje nowe gałęzie (append-only)         │
│  • jeśli gałąź za duża → podsumowuje starą   │
│  • ustawia current_node                      │
│  • zapisuje całe drzewo do SQLite            │
└──────────────────────────────────────────────┘
```

## Architektura systemu

```
┌──────────────────────────────────────────────┐
│  main.py (CLI)                               │
│  new / session / tree / list / alias / delete │
└─────────────┬────────────────────────────────┘
              │
┌─────────────▼────────────────────────────────┐
│  orchestrator.py                              │
│  process(session, prompt) → { wynik, stats } │
└─────────────┬────────────────────────────────┘
              │
    ┌─────────┴─────────┬────────────┐
    ▼                   ▼            ▼
┌──────┐          ┌────────┐   ┌────────┐
│ db.py │          │selector│   │updater │
│registry│         │.py     │   │.py     │
│sessions│         │ executor│  │        │
└───┬───┘          │ .py    │   └────────┘
    │              └────────┘
    ▼
┌──────────────────────────────────────────────┐
│  SQLite (~/.config/agent-tree/)              │
│  ├── registry.db (sesje + aliasy)            │
│  └── sessions/<uuid>.db                      │
│      ├── branches (label, full, summary)     │
│      ├── keywords (FTS5 search)              │
│      ├── session_log (historia)              │
│      └── meta (current_node)                 │
└──────────────────────────────────────────────┘

─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
Most HTTP

┌──────────────────────────────────────────────┐
│  opencode-bridge (port 5001)                  │
│  POST /v1/chat/completions                    │
│  → github-copilot (executor)                  │
│  → opencode (selector/updater)                │
└──────────────────────┬───────────────────────┘
                       │ @opencode-ai/sdk
                       ▼
┌──────────────────────────────────────────────┐
│  opencode serve (port 4096)                   │
│  Provider: github-copilot (OAuth)             │
│  Provider: opencode (Zen – DeepSeek free)     │
└──────────────────────────────────────────────┘
```

## Modele

| Rola | Model | Dostęp | Koszt |
|---|---|---|---|
| Selector | **DeepSeek V4 Flash Free** | OpenCode Zen | $0 |
| Executor | **Copilot Pro** (np. gpt-5.4) | GitHub Copilot OAuth | subskrypcja |
| Updater | **DeepSeek V4 Flash Free** | OpenCode Zen | $0 |

Dostępne modele Copilot przez OpenCode: `claude-sonnet-4.5`, `claude-opus-4.7/4.8`, `gpt-5.3-codex`, `gpt-5.4`, `gpt-5.4-mini`, `gemini-2.5-pro`, `gemini-3.5-flash` i inne.

## Stack techniczny

- **OpenCode v1.17.5** – dostęp do modeli (Copilot OAuth + Zen free)
- **opencode-bridge** (port 5001) – OpenAI-compatible proxy na OpenCode
- **Python 3.12 + OpenAI SDK** – logika selektor/executor/updater
- **SQLite** (registry.db + per-session .db) – trwałe drzewo z FTS5

## Zasady drzewa

- **Append-only** – tylko dodawanie, nigdy usuwanie
- **Bez utraty** – zamiast usunięcia, stare treści podsumowywane (summary przechowuje esencję)
- **Selektywny retrieval** – DeepSeek wybiera tylko pasujące gałęzie przed wysłaniem do Copilot
- **Auto-summary** – jeśli wybrany kontekst przekracza okno, najmniej istotne gałęzie są podsumowywane
- **Current node** – wskaźnik aktualnego miejsca pracy (automatycznie ustawiany przez updater)
- **Per-session** – każda sesja ma osobną bazę UUID, registry mapuje nazwę → UUID

## Struktura projektu

```
multiagent/
├── PROJEKT.md
├── requirements.txt           # openai, pydantic, python-dotenv
├── .env                       # BRIDGE_URL, modele
├── db.py                      # RegistryDB + SessionDB (SQLite)
├── selector.py                # DeepSeek → wybiera gałęzie
├── executor.py                # Copilot → wykonuje zadanie (+ tools loop)
├── updater.py                 # DeepSeek → aktualizuje drzewo
├── orchestrator.py            # główna pętla
├── main.py                    # CLI entrypoint
├── repl.py                    # interaktywny REPL (readline, /komendy)
├── web.py                     # Web UI (D3.js tree viz, port 8080)
├── websearch.py               # DuckDuckGo instant-answer search
├── tools.py                   # 10 narzędzi agentic (bash/grep/glob/read/write/edit/…)
└── oc_client.py               # klient OpenCode (ask / stream / tools)

~/.config/agent-tree/
├── registry.db                # rejestr sesji
└── sessions/
    ├── a1b2c3d4-....db        # jedna sesja
    └── ...
```

## CLI

```bash
# Nowa sesja
python main.py new "blog-api" "Stwórz API dla bloga z CRUD i autoryzacją JWT"

# Zadanie w sesji
python main.py session "blog-api" "Zaplanuj endpointy REST"

# Lista sesji
python main.py list

# Podgląd drzewa
python main.py tree "blog-api"

# Alias
python main.py alias "blog-api" "blog"

# Usunięcie sesji
python main.py delete "blog-api"
```

## Schemat SQLite

**registry.db:**
- `sessions` – uuid, name, description, root_node, created_at, last_used
- `aliases` – alias → target

**sessions/<uuid>.db:**
- `branches` – id, label, full_text, summary, parent_id, depth, compressed
- `keywords` – branch_id, keyword
- `branches_fts` – FTS5 indeks label + summary
- `session_log` – prompt, selected_ids, new_branches, timestamp
- `sessions_meta` – current_node_id, name, description

## Przepływ

1. **Prompt** → Selector (DeepSeek V4 Flash Free) – wyszukuje w drzewie gałęzie pasujące do prompta, podsumowuje jeśli przekroczone okno
2. **Executor** (Copilot Pro) – dostaje: wybrany kontekst + prompt
3. **Updater** (DeepSeek) – parsuje odpowiedź, dodaje nowe gałęzie, kompresuje przepełnione, ustawia current_node
4. **Persystencja** – wszystko zapisywane do SQLite, registry aktualizowany

## Status (v1, 2026-06-19)

- [x] OpenCode + Copilot (OAuth)
- [x] opencode-bridge (port 5001)
- [x] RegistryDB + SessionDB (SQLite)
- [x] Selector (DeepSeek selection)
- [x] Executor (Copilot execution + agentic tools loop, max 12 rund)
- [x] Updater (tree update + compression)
- [x] Orchestrator (main loop)
- [x] CLI (new / session / tree / list / alias / delete)
- [x] REPL interaktywny (readline, /komendy, spinner faz, streaming)
- [x] Web Search (DuckDuckGo instant-answer, automatycznie gdy selector wskaże)
- [x] Tools – 10 narzędzi agentic (bash, grep, glob, read_file, write_file, edit_file, list_dir, webfetch, question, todowrite)
- [x] Web UI (D3.js tree viz, port 8080, delete branch, session log)
- [x] Files cache – hierarchiczne gałęzie `files-cache` w drzewie (executor śledzi touched_files, updater upsertuje węzły per plik/katalog)

## Pomysły na rozwój

### `get_history` — narzędzie historii dla LLM

**Ocena przydatności: niska–średnia** przy dobrym drzewie.

Pomysł: dodać do `tools.py` narzędzie `get_history(limit=10)` które zwraca ostatnie N wpisów z `session_log` jako skrócone podsumowanie (timestamp, prompt, liczba gałęzi, tokeny). LLM sam decydowałby kiedy go wywołać.

Dlaczego drzewo kontekstu w większości przypadków wystarczy: updater po każdej turze zapisuje wyniki i ustalenia jako gałęzie — selector wybiera je przy kolejnym zapytaniu. Historia (`session_log`) przechowuje surowe metadane zapytań, a nie ich semantykę.

Kiedy `get_history` byłoby przydatne:
- Prompt nawiązuje do poprzedniego przez zaimek ("zrób to samo co poprzednio")
- Debugowanie pętli: model utknął w kółko i potrzebuje zobaczyć że dana próba już była
- Audyt: użytkownik pyta "co robiłem wcześniej w tej sesji"

Implementacja: ~30 linii w `tools.py` + wpis w `TOOL_DEFS`. Nie wymaga zmian w DB.
