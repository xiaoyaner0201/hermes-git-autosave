# Changelog

## 0.3.1 — Unreleased

- Tokenization failures no longer abort the terminal observer. Valid heredocs can also trigger these
  failures; opaque commands conservatively attempt the initial cwd repository snapshot and invalidate its
  turn coverage without guessing shell paths. A fixed `kind=parser`, `code=command_parse_fallback` warning
  per affected hook call records degraded coverage without command contents or exception text.
- Existing snapshot budgets, exclusions and the 65,536-character tokenization ceiling remain in force.
  Heredoc semantics are not modeled; opaque commands outside allowed Git repositories have no single-file
  protection and never cause directory copies. Tool calls remain unblocked.
- Pre-tool work runs in a fresh, exclusive worker process that the hook kills at its deadline, so a stalled
  filesystem or Git call can no longer hold the host's tool entry. Nullable `terminal.workdir` and invalid
  settings warnings are preserved across the worker boundary.
- The stat comparison that decides which tracked files to rehash ignores `st_dev`, as Git's default build
  does. Device numbers can change after a reboot or remount; previously every clean tracked file was then
  reread on each snapshot and medium repositories ran out of budget until the next real index rewrite.
- The private index and hash copies now live in a temporary directory inside the repository's Git directory
  instead of the plugin storage root, avoiding a per-file cross-volume copy when storage is on another disk.
  Leftovers from a killed worker are removed under the repository lock on the next snapshot.

## 0.3.0

First public release.

- Pre-edit protection for `write_file`, `patch` (including V4A Update/Add/Delete/Move headers) and common
  mutating terminal commands. In Git repositories the plugin saves the working state to the local shadow ref
  `refs/hermes-autosave/<branch>` (with reflog) through a private index; HEAD, branches, the real index and
  the working tree are never changed, and nothing is pushed.
- Outside Git, or in denied top-level repositories, it copies the existing file into the plugin data directory,
  with per-copy metadata (content identity, file mode, observation order).
- `hermes autosave status | find | list | restore | prune | sweep`, JSON output. `restore` writes only the
  requested path (or `--to`), keeps the file mode and recreates missing parents. `sweep` reads the Hermes
  session database read-only; schedule it yourself.
- Content writes through symlinks protect the file actually written, under its own path; V4A link writes
  follow the host's order of resolution. Restoring a link never writes its referent.
- Credential stores and dependency directories are excluded at every symlink hop. Plugin Git subprocesses
  run no clean filters, hooks, fsmonitor, split-index or lazy fetch. Repositories with mirror remotes or
  `refs/*` push refspecs are skipped. Pruning is pinned to the storage directory.
- Large repositories stay within the hook budget: paths-only mode, batched hashing, and a linear command
  parser capped at 64 KiB.
- Requires Hermes >= 0.22.0, which no longer blocks concurrent tool calls while a pre-tool hook runs.
- Linux and macOS only. Windows is not supported yet: terminal-command path parsing assumes a POSIX shell.

Known limitation: multi-operation V4A patches that move a symlink and then update it can miss the edited file
on case-insensitive or Unicode-normalizing filesystems, and a patch the host rejects can leave an extra local
copy. See the README.
