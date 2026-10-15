## Change

Describe the behavior change and any disclosure or compatibility impact.

## Verification

- [ ] `python -m ruff check .`
- [ ] `python -m pytest -q` (temporary repositories/HERMES_HOME; no network)
- [ ] `hermes plugins validate .` with a throwaway HERMES_HOME
- [ ] Real index, HEAD, branches and working tree remain untouched
- [ ] English and translated documentation updated together
- [ ] No private Hermes imports, runtime overrides, pushes or background processes
