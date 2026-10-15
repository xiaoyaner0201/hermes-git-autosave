# Changelog

## 0.3.0 — Unreleased

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
