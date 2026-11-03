# agent-tree

Agent LLM z **drzewem kontekstu**: tani model buduje i wybiera kontekst, drogi model wykonuje zadanie.

```
prompt → Selector (tani LLM, FTS5 po drzewie) → Executor (mocny LLM, tylko wybrane gałęzie)
       → Updater (tani LLM, dopisuje nowe gałęzie, zapis do SQLite)
```

- Drzewo kontekstu w SQLite (append-only, automatyczne podsumowywanie dużych gałęzi)
- Narzędzia agenta: odczyt/zapis plików w workspace, wyszukiwanie w sieci
- Interfejsy: REPL (`repl.py`) i panel webowy (`web.py`)

Szczegóły architektury: [PROJEKT.md](PROJEKT.md).

## Uruchomienie

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # klucze API dostawców LLM
python main.py
```
