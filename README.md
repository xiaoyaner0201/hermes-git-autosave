[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

# git-autosave

A lightweight, local alternative to Hermes directory-level filesystem checkpoints (`checkpoints.enabled`).
Before mutating tool calls, it saves the affected Git repository's current working state to
`refs/hermes-autosave/<branch>` with a reflog, using a private index. Same-turn calls deduplicate by
successfully protected paths, so later explicit targets outside the earlier snapshot's scope are still saved.
It never changes HEAD, branches, the real
index or the working tree, never pushes, and never blocks, rewrites or approves a tool call.

Outside Git, or in a denied top-level repository, it copies only the existing file about to be changed.
New files have no pre-edit version; directory targets outside allowed Git repositories are not copied.

## Why this instead of checkpoints or git-hook?

Built-in checkpoints protect directories, including non-Git projects, using a separate filesystem snapshot
store. This plugin reuses each project's local Git object database for incremental snapshots and provides
single-file fallback copies, not directory-wide non-Git recovery. Its small scope reduces scan overhead.

The catalog's `git-hook` plugin fetches/pulls and commits/pushes to the real branch. This plugin does none of
those: shadow refs are local recovery history, not project commits or synchronization. The plugins have
different purposes. Autosave is not a backup service or a guarantee against data loss.

## Install

Requires Hermes >= 0.22.0, Python 3.11+ and a local Git binary, on Linux or macOS. Windows is not supported in
0.3.0: terminal-command path parsing assumes a POSIX shell, so Windows paths are misread. Older Hermes versions can incorrectly block
concurrent tool calls while a pre-tool snapshot is still running; v0.22.0 fixes this guard race
([NousResearch/hermes-agent#105223](https://github.com/NousResearch/hermes-agent/pull/105223)).

```sh
hermes plugins install xiaoyaner0201/hermes-git-autosave
hermes plugins enable git-autosave
```

Restart running CLI/gateway sessions to load the hook. Local CLI subcommands are intentionally not registered
inside an isolated plugin host; the observer hook does not depend on CLI registration.

To use this instead of built-in checkpoints, set this in the active profile's `config.yaml`:

```yaml
checkpoints:
  enabled: false
```

Disable built-in checkpoints only if the narrower Git/file-copy coverage fits your recovery needs. Existing
checkpoint data is not migrated or removed. Enabling both is supported but duplicates snapshot work.

## Settings

All settings are optional, under `plugins.entries.git-autosave.settings` in the active profile's configuration.
They are read with `ctx.get_config` during registration. Core functions also work without a plugin context.
Numeric settings require finite positive numbers (not booleans or strings); count limits require whole numbers.
Invalid values fall back to defaults and are recorded once per service instance in `ledger.jsonl`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `storage_dir` | unset | Override the plugin data root; `~` is expanded, relative paths use the launch directory. |
| `keep_days` | `3` | Retention window used by `prune`, status and ledger queries; pruning must be run explicitly. |
| `max_file_mb` | `10` | Maximum size of newly hashed files and fallback copies, in MiB. Existing tracked blobs may remain. |
| `hook_budget_seconds` | `8` | Total worker deadline (startup and IPC included); cap `12` seconds, plus bounded cleanup/diagnostic allowance below 0.2s; below the host's 30-second timeout. |
| `big_repo_entries` | `20000` | Above this index entry count, update touched paths only. |
| `max_untracked` | `2000` | Above this eligible untracked-file count, add only explicitly touched untracked files. |
| `extra_skip_dirs` | `[]` | Extra path-component names to exclude, including tracked files. |
| `deny_toplevels` | `[]` | Extra repository top-level paths that must use single-file copies only. |

```yaml
plugins:
  entries:
    git-autosave:
      settings:
        keep_days: 3
        hook_budget_seconds: 8
        extra_skip_dirs: [generated-dependencies]
        deny_toplevels: []
```

Storage defaults to `plugins.plugin_storage.plugin_data_dir("git-autosave")`, falling back to
`<HERMES_HOME>/plugin-data/git-autosave` when that module is unavailable. Layout:

```text
<plugin-data-root>/
  files/<YYYYMMDD>/objects/<hash-prefix>/v2-<path-sha256>.<HHMMSS>-<ns-sequence>-<sha8>
  files/<YYYYMMDD>/objects/<hash-prefix>/<copy-name>.json
  ledger.jsonl
  state/
```

New storage names use the SHA-256 of the canonical absolute path, not original filename components, keeping
copy and metadata names bounded even for maximum-length source names. Atomic, owner-only sidecar metadata
stores the original path, content identity, mode and observation key independently of the best-effort ledger.
A new copy is incomplete without its metadata and is not listed. Copies deduplicate across processes only
against the latest content-and-mode observation: A → B → A and mode cycles preserve all transitions.
An advisory-locked durable sequence assigns increasing keys to Git and copy observations even when the clock
stalls or moves backward. Legacy path-based layouts remain readable, with ledger mode / owner-only fallback
and mtime ordering for the oldest names. Every layout rejects conflicting metadata/ledger original-path
identity. Inspect uncertain metadata-free legacy copies with `--to` rather than overwriting an original.
No state is stored in the plugin install directory; Git objects
remain in each repository.

## Coverage and safety rules

- `write_file` and `patch` paths, including V4A Update/Add/Delete/Move headers, trigger pre-edit protection.
  Header whitespace and Move arrows with or without surrounding spaces follow the host's syntax.
  Absolute paths after `~` expansion are used as-is; relative paths use public `resolve_agent_cwd()` (session
  cwd, then `TERMINAL_CWD`, then launch directory). Without that helper, use `TERMINAL_CWD` or `os.getcwd()`.
- Content writes through symlinks (`write_file`, patch Update/Add, terminal redirection/tee/in-place edits)
  protect the resolved referent as its own path: normal allowed-repository snapshot, otherwise a file copy.
  Exclusions still apply. Patch Delete/Move and terminal unlink/move protect entries, not referent content;
  V4A link writes follow the host: each Update header resolves before any operation runs, later hops
  follow the entries earlier Move/Delete/Add operations leave, and an Add creates a new regular entry.
  Link entries are restorable only where Git snapshots exist; outside Git or in denied repositories the link
  entry itself is not saved. `find <link>` lists entry versions and a best-effort ledger hint to find the
  referent separately. No alias state can redirect restore to another file.
- Terminal `workdir` is the shell's initial cwd; a leading static `cd` applies afterward. `env -C`/`--chdir`
  changes only that command's cwd, and `install -t`/`--target-directory` includes its destination directory.
  Shell redirections resolve in the segment's shell cwd after static `cd`, never in an env/Git child cwd.
  Git `-C` and common file-mutator operands select affected repositories, including operands outside cwd.
  A bounded token parser
  recognizes newline command boundaries, sudo/env/command/nohup/time/nice/xargs wrappers, rm/rmdir/unlink/mv,
  cp/rsync/install/dd/shred/chmod/touch, redirection (`>`, `>|`, `>>`, `&>>`), in-place sed/perl, truncate,
  Git checkout/restore/reset/clean/stash/rebase/switch/rm/mv/apply/am/merge/pull/cherry-pick/revert,
  checkout-index, read-tree -u, content-changing update-index and worktree remove, patch/unzip/tar extraction,
  find -delete/-exec and non-append tee. `tee -a`/`tee --append` intentionally do not trigger protection;
  `>>` does. This is not a full shell parser: dynamic operands and indirect mutations (scripts, interpreters,
  shell functions) may be missed. It makes no approval decisions. Commands above 65,536 characters
  (64 KiB for ASCII) are never tokenized: they are conservatively considered destructive, protect only the
  initial workdir repository, and log one warning per service instance without recording command contents.
- Dependency directories are always excluded, even if tracked: node_modules, bower_components, jspm_packages,
  .pnpm-store, .yarn, vendor, .venv, venv, site-packages, dist-packages, __pypackages__, .tox, .nox, .eggs, Pods,
  Carthage, .gradle, .m2, .cargo, .bundle.
- Build/cache directories exclude untracked files and fallback copies but retain tracked files: __pycache__,
  .pytest_cache, .mypy_cache, .ruff_cache, .hypothesis, .cache, .next, .nuxt, .svelte-kit, .turbo, .parcel-cache,
  .vite, .angular, .expo, dist, build, out, target, coverage, .coverage, htmlcov, .terraform, DerivedData.
- Denied repository top-levels: filesystem/drive roots, the home directory, `HERMES_HOME` and its ancestors,
  direct `HERMES_HOME/profiles/*` directories, direct children of macOS volume mounts and Linux `/mnt` and
  `/media`, plus `deny_toplevels`. These get file copies only, never a whole-repository snapshot.
- Big repositories, or repositories whose last slow snapshot took at least three seconds within the past
  hour, use paths-only mode. A directory target adds tracked files only; unrelated untracked entries are
  not enumerated. Files not touched retain their
  real-index version; unrelated working-tree changes may not be saved. Gitlinks are not recursively scanned.
- Per-repository nonblocking locks serialize the hook, sweep and prune. POSIX uses `flock`; Windows uses
  `msvcrt` byte-range locks, with filesystem-dependent advisory behavior. A busy repository is skipped and
  logged. Without a host turn ID, the plugin does not guess turn boundaries; unchanged-tree deduplication
  still applies. Same-turn deduplication is process-local and bounded, not shared across agent processes.
  Only paths actually raw-hashed in this turn with the same repository HEAD oid and branch suppress an
  explicit repeat. Stat-only/index baseline coverage never does. State-changing Git commands invalidate
  that repository's turn cache and do not repopulate it from their own pre-command snapshot.
  Later ignored/sparse/uncovered targets retry; failed, skipped and oversized-file attempts cover nothing.
- The actual pre-tool work (path discovery, file I/O, state and ledger writes) runs synchronously in a fresh,
  exclusive Python worker. The host waits on bounded, nonblocking IPC, then kills only its owned worker group
  and polls reaping for at most 50ms. Git children stay in that group. No backup thread survives a timeout,
  and a successful normal snapshot finishes before the tool runs. Defaults remain 8 seconds, capped at 12;
  bounded cleanup and a separate bounded diagnostic writer can add less than 0.2 seconds. Read-only tool
  names return immediately. Contention/request-size failures skip protection rather than wait indefinitely.
  The implementation uses CPython's spawn helper (no Python code/preexec_fn after fork, no inherited Python
  locks, no exec-error-pipe wait), tested on CPython 3.14/macOS; Linux needs platform verification.
- Timeout/error means protection may be incomplete: recovery is NOT guaranteed. A short fixed stderr warning
  is attempted by a separately bounded writer, and callback.health retains a failure count/last error even
  when stderr is unavailable. Ordinary errors also go to the worker's best-effort ledger; a hard-killed worker
  cannot guarantee a durable timeout record. No raw tool args are written to IPC temp files (IPC is in memory).
  Requests/replies are capped at 1 MiB/256 KiB; oversized cache replies drop coverage and safely retry later.
  Regular files are opened no-follow/nonblocking, descriptor-validated, size-bounded, then Git hashes private
  owner-only copies rather than reopening raced working paths. A killed worker can leave private temporary
  index/blob files; they contain allowed source bytes, not commands, and are not published recovery versions.
- Registration defers settings/state logging until the bounded worker. Loading/importing plugin source and
  host configuration callbacks are outside the hook deadline. An uninterruptible kernel process or failure
  of the OS process-creation primitives cannot be made killable by Python: the parent never waits forever
  for it, but immediate reclamation of such an OS task/lock cannot be guaranteed. No Windows support.
- Git fsmonitor, hooks, split-index and auto-maintenance are disabled for plugin subprocesses. A fresh private
  HEAD-based index is populated only with index-info entries; Python compares read-only index stats with lstat,
  including racily-clean entries. Git is never asked to refresh or compare working bytes against an index.
  Explicit existing file targets are always raw-hashed, even if normalized, filtered or assume-unchanged.
  Untouched paths may retain index representations. Skip-worktree entries keep their index/HEAD baseline
  during implicit or directory snapshots, even when present with different bytes; absent sparse files are
  never inferred deleted. Explicit, present file targets save their real bytes instead. No clean filters run.
  Symbolic shadow refs cause a skip;
  all ref writes/deletions use --no-deref. GIT_NO_LAZY_FETCH=1 and protocol.allow=never prevent implicit fetching;
  missing required objects produce a logged, fail-open skip.
- Remotes whose `mirror` setting Git parses as true (including valueless keys, nonzero integers, yes and on),
  or with a push refspec containing `refs/*`, cause repository snapshots to be skipped. Invalid mirror boolean
  values cause a logged skip/error; the plugin never assumes they are safe.
  Plain `git push`, `git push --all` and `git push --tags` do not publish `refs/hermes-autosave/*`.
  `git push --mirror` or a `refs/*` refspec would publish them: never use those with private snapshots.

## CLI and restore recipes

Every subcommand supports `--json`:

```sh
hermes autosave status
hermes autosave find path/to/file
hermes autosave list [repo]
hermes autosave prune --dry-run
hermes autosave prune
hermes autosave sweep --since-minutes 360 --budget-seconds 150
```

`status` reports the data root, sizes, repositories snapshotted during `keep_days`, and recent ledger errors.
`list` lists retained reflog snapshots (and a surviving tip). `find` lists only paths whose raw pre-edit
bytes were actually read, merged newest-observation-first across branches and copy/Git backends. Each Git
commit's `Autosave-Observed` trailer records observed paths, content IDs and modes; its key records at_ns.
Copy metadata lives beside each copy. Ledger loss cannot turn placeholder index/HEAD bytes into versions.
Paths-only snapshots observe only selected paths (directory targets expand to tracked files).
A recurring A after B is a new observation even on another branch/backend. Consecutive identical trees/copies
may deduplicate only if no observation intervened; unrelated observations conservatively force a fresh event.
Old Git snapshots without provenance and plain HEAD are not listed by find: inspect them manually with Git.
Legacy copies retain metadata/ledger fallbacks. Review the selected version before restoring: the destination
is replaced. Version numbers are 1-based; rerun `find` if history changes.
`find <link>` lists only the link entry's versions; an optional plain ledger note points
at the referent's separate history. Notes and old alias-state files never affect restore.

```sh
hermes autosave restore path/to/file --version 1
hermes autosave restore path/to/file --version 2 --to recovered/file --json
git -C <repo> reflog show refs/hermes-autosave/<branch>
git -C <repo> diff 'refs/hermes-autosave/<branch>@{1}' -- path/to/file
```

`restore` creates parents, restores regular files through a temporary sibling and atomic replacement with
saved mode, and recreates symlinks without following the existing destination. It never stages the result.
Restore writes only the requested path (or `--to`), including for links. Recover referent content through
`hermes autosave restore <referent>`. A Git link-entry version recreates the link; outside Git / denied
repositories no link-entry version is saved. `restored_to` states the path changed.
`restore --to` intentionally follows symlinked parent directories of the destination; verify and trust those
parents before restoring. Only the final destination symlink is replaced instead of followed.
Fallback copies record the original mode; legacy copies without mode metadata restore as owner-only files.
`git checkout <snapshot> -- <file>` also restores mode, but changes the real index; avoid `git show > file`,
which follows symlinks and loses mode. A reflog snapshot is not an ancestor on the user's branch.
Detached HEAD snapshots use a stable `detached-<repository-hash>` shadow ref.

`prune` deletes shadow refs whose tips are older than `keep_days`, expires old reflog entries, removes old
file-copy day directories, and trims the ledger. Day-directory retention is calendar-day granularity.
It never runs `git gc`; unreachable objects can remain until the user's normal Git garbage collection.
Run prune explicitly or schedule it separately. `--dry-run` makes no snapshot/ref/ledger changes.
Symlinked storage/files/state/day directories are refused; day deletion uses no-follow directory descriptors
and symlink-attack-resistant rmtree. Ledger/lock reads and writes use the pinned root/state descriptors too;
root or state identity changes abort pruning. Platforms without these primitives (including Windows) refuse pruning
with a reported error rather than use unsafe deletion. Sweep budgets also cover ledger scans and per-call
processing; session messages above 1 MiB are skipped with a warning.

## Scheduling the sweep yourself

The sweep catches hook misses from budget exhaustion, agent subprocesses or sessions not yet restarted.
It reads `<HERMES_HOME>/state.db` read-only using a `mode=ro` URI and a short SQLite timeout. Recent sessions
use `COALESCE(last_activity_at, started_at)`; messages supply write/patch paths and terminal workdir/leading
cd targets. It also includes repositories from the plugin ledger during the same lookback window (default
six hours). The schema is private and version-dependent: columns are probed, missing fields produce warnings,
and the ledger remains usable. This is discovery, not a filesystem-wide scan. A sweep runs after mutations,
so it cannot recreate a missing pre-edit version.

Success is silent with exit 0 unless `--json`. Errors or schema warnings print a short summary to stdout and
still exit 0, deliberately allowing a no-agent Hermes cron to deliver it. `--json` always prints a result
object with `errors`, `warnings`, repository results and copy counts/paths; monitor those fields, not the exit
code. The plugin creates no schedules or background processes.

Choose one scheduler; use the same active profile/HERMES_HOME and an absolute Hermes executable path.

1. Hermes no-agent cron: create your own `autosave-sweep.sh` inside `<HERMES_HOME>/scripts/` containing:

   ```sh
   #!/bin/sh
   exec /absolute/path/to/hermes autosave sweep --since-minutes 360 --budget-seconds 150
   ```

   Then schedule it yourself:

   ```sh
   hermes cron create "every 10m" --no-agent --script autosave-sweep.sh --deliver telegram --name autosave-sweep
   ```

   Choose a connected delivery platform, or `--deliver local` for saved output only. A plain terminal has no
   live delivery channel. Check your Hermes version's `hermes cron create --help`.
2. crontab: add `*/10 * * * * /absolute/path/to/hermes autosave sweep --since-minutes 360 --budget-seconds 150`.
   Set `HERMES_HOME` and `PATH` explicitly in your crontab if needed; cron's environment differs from your shell.
3. launchd (macOS): a user LaunchAgent with `ProgramArguments` set to the executable, `autosave`, `sweep`,
   `--since-minutes`, `360`, `--budget-seconds`, `150`; set `StartInterval` to `600`, configure environment and
   `StandardOutPath`/`StandardErrorPath`, then load it yourself with `launchctl bootstrap`.
4. systemd (Linux): a user oneshot service with `ExecStart=/absolute/path/to/hermes autosave sweep`, explicit
   environment and a timer with `OnUnitActiveSec=10min`. Enable it yourself with `systemctl --user enable --now`.

Separately schedule `hermes autosave prune` if you need enforced retention. Sweep does not prune implicitly.

## Disclosure

This plugin reads the Hermes session database read-only for sweep discovery. It runs local Git subprocesses
in repositories the agent touches, writes shadow refs and reflogs into those repositories' `.git`, and copies
existing files the agent is about to modify into the plugin data directory. Untracked files not ignored by
`.gitignore` can be stored in the local `.git` object database; review ignore rules for secrets and sensitive
files first. Copies and objects are local and unencrypted. It makes no network calls, starts no background
processes, requests no credentials, and does not integrate with any credential store or telemetry service.
It never copies/reads credential-store working files or collects them as untracked snapshot entries:
`HERMES_HOME/.env`, `HERMES_HOME/auth.json` (and conservatively any `auth.json`), `.env`, `.env.*`, `.netrc`,
`.git-credentials`, `.npmrc`, `.pypirc`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`,
`.claude/.credentials.json`, and all files beneath `.ssh`, `.aws`, `.docker`, `.azure`, `.kube`,
`.config/gcloud` and `.config/gh`. This includes `.aws/credentials`, `.aws/config`, `.docker/config.json`,
`.azure/accessTokens.json`, `.azure/msal_token_cache.*`, `.kube/config`, `.config/gh/hosts.yml`, and gcloud's
`application_default_credentials.json`, `credentials.db` and `access_tokens.db`. Matching is case-insensitive
and includes absolute path ancestors, even for repositories nested in credential directories. Lexical paths
and every content-path symlink substitution are checked before collapsing the chain (at most 40 hops).
Loops/overlong content chains are skipped; the final link entry can still be snapshotted independently.
Credential/dependency-named intermediate links cannot hide excluded content paths. Entries already
committed by the user remain at HEAD, unchanged; working/staged credential changes are not collected. This name-based exclusion is not a secret scanner;
other sensitive files still need ignore rules.

## Limitations and development

Recovery coverage is best-effort: repository errors, contention, oversized files, unmerged indexes, indirect
terminal mutations, unavailable host-local paths (remote/container tools) and exhausted deadlines can leave
changes unsaved. No concurrent snapshot of an actively changing working tree can guarantee an atomic view.
Windows locking depends on filesystem support; ACLs govern privacy, and shell restore commands target cmd.exe,
not PowerShell. Same-turn deduplication is process-local. Session schema compatibility is probed, not promised.
Removing the plugin does not remove its durable data or refs.

**Known limitation: multi-operation V4A patches through symlinks.** When one patch moves or deletes a
symlink and then updates the moved path, the plugin replays the operations to find the file actually written.
That replay compares path spellings exactly, while the host goes through the filesystem. On case-insensitive or
Unicode-normalizing filesystems (default macOS and Windows), a link whose text names an intermediate link with a
different case or Unicode form (`MIDDLE` vs `middle`) can make the plugin protect the old target instead of the
file the host edits. That file then has no pre-edit version. The replay also does not re-run the host's patch
validation, so a patch the host rejects can still leave an extra local copy of an unrelated file, and the separate
`path` argument of a V4A call may copy a link's target the patch never touches. Ordinary `write_file` and
single-operation `patch` edits through links are not affected. If you rely on recovery for such patches,
commit first or split the Move and the Update into separate calls.

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q
```

Hermetic tests use temporary repositories/HERMES_HOME and no network. CI targets Ubuntu and macOS
on Python 3.11 and 3.13. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
Apache-2.0 — Copyright 2026 The hermes-git-autosave Authors; see [LICENSE](LICENSE).
