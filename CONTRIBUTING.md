# Contributing

Use Python 3.11 or newer and a local Git binary. Create a virtual environment, then run:

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q
```

Tests must use temporary repositories and a temporary `HERMES_HOME`, without network access. Never test against
an existing personal repository or a real Hermes profile. Preserve the observer contract: no tool directives,
no HEAD/branch/real-index/working-tree writes, no pushes, no schedules and no core monkey-patches.

Use only the optional public Hermes helpers documented in the README. Keep comments, CLI output and source
in English. Update all three READMEs together. Do not add a tool-only `pyproject.toml`: Hermes treats it as
installable packaging. Run tests and lint before each focused conventional commit.

For catalog submission, use an exact 40-character commit SHA and disclose session database reads, local Git
subprocesses, shadow refs/reflogs and file copies. Run `hermes plugins validate .` with a throwaway
`HERMES_HOME`. The manifest compatibility floor must reflect a tested Hermes release before publication.
