# Verification

Release 0.3.0 was checked on the pinned commit with:

- `python -m ruff check .` and `python -m pytest -q` on Hermes-free Python 3.11 and inside Hermes v0.22.0 and
  v0.22.1 virtual environments (the hook and real-host tests run only where Hermes is importable);
- `hermes plugins validate . --install-deps` from Hermes v0.22.1 with a throwaway `HERMES_HOME`: no warnings;
- a real Hermes concurrent-dispatch probe: v0.22.0+ runs a second tool call while a snapshot is in progress
  (older releases block it, hence `requires_hermes: ">=0.22.0"`);
- timing through a real Hermes host on synthetic repositories: about 0.55 s for a 25,000-file repository and
  for a repository with 1,500 untracked files.

## Mutation checks

Each check applies one replacement to `core.py` in a disposable `git archive HEAD` copy and runs the full
Hermes-free suite. Each must turn the suite red; never mutate a live plugin, real credentials or production
repositories.

| Mutant (single replacement in `core.py`) | Expected | Representative failing test |
| --- | --- | --- |
| `env["GIT_INDEX_FILE"] = str(index)` → `pass` (private-index operations write the real index) | 9 failures | `tests/test_core.py::test_index_status_head_unchanged` |
| `remove = {p for p in baseline \| tracked.keys() if skip_entry(p, tracked=True)}` → `remove = set()` (tracked dependency dirs kept) | 22 failures | `tests/test_core.py::test_dependencies_excluded[node_modules]` |
| `return False` at the start of `denied()` (top-level deny disabled) | 7 failures | `tests/test_core.py::test_denied_top_level_file_only[hermes]` |
