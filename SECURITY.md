# Security policy

Report security issues privately to the repository maintainers using GitHub's private vulnerability reporting,
not a public issue containing secrets. Include the plugin/Hermes/Git/Python versions and a minimal reproduction
using synthetic data. Version 0.3.x is the supported release line.

Snapshots are local, not encrypted, and not a substitute for an independent backup. Untracked files not ignored
by Git can enter the repository object database. Review `.gitignore` before enabling the plugin. File copies
use owner-only permissions on POSIX; Windows access is also governed by inherited filesystem ACLs.

## Credential exclusions

Never copied or read from the working tree, or collected as untracked Git entries: Hermes `HERMES_HOME/.env`
and `auth.json` (conservatively any `auth.json`), `.env`, `.env.*`, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`,
`*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `.claude/.credentials.json`, and every file beneath
`.ssh`, `.aws`, `.docker`, `.azure`, `.kube`, `.config/gcloud` and `.config/gh`. Included examples:
`.aws/credentials`, `.aws/config`, `.docker/config.json`, `.azure/accessTokens.json`, `.azure/msal_token_cache.*`,
`.kube/config`, `.config/gh/hosts.yml`, `.config/gcloud/application_default_credentials.json`,
`.config/gcloud/credentials.db`, `.config/gcloud/access_tokens.db`.
Matching is case-insensitive and uses absolute path ancestors, including nested credential-directory repository
roots. Lexical paths and every symlink substitution are checked before chain collapse (maximum 40 hops);
credential/dependency-named intermediate links and directory aliases cannot bypass content policy. Loops or
longer content chains are skipped; final link entries remain independently protectable without traversing
the referent. Already committed entries remain unchanged at HEAD; working/staged credential changes are not collected.
This policy uses path names, not credential contents; it is not a general secret scanner.

Git subprocesses disable clean filters by avoiding working-tree/index comparison entirely. Fresh private indexes
are nonsplit; real index dependencies are read-only. Symbolic shadow refs cause a logged skip, ref writes use
`--no-deref`, and implicit fetching is disabled with `GIT_NO_LAZY_FETCH=1` and `protocol.allow=never`.
Prune refuses symlinked storage traversal components and uses descriptor-based, no-follow deletion. Ledger and
lock I/O is relative to pinned root/state descriptors; changed storage identities abort pruning. Without
symlink-attack-resistant directory primitives, pruning reports an error rather than delete unsafely.
Restore replaces destination symlinks rather than following them; parent directories must be trusted.
Content writes through symlinks protect referents as independent paths under normal repository/copy rules.
Patch Delete/Move and terminal unlink/move use entry semantics only; V4A link writes are modelled
like the host: Update headers resolve before any operation, later hops follow the entries earlier Move/Delete/Add
operations leave (relative link text from the link's current parent), Add creates a new regular entry, and every hop
passes the exclusion policy. The replay compares path spellings exactly and does not re-run host validation; see the
README's known limitation for case/Unicode-equivalent link names and rejected patches. Excluded content is never
copied either way. V4A headers accept host-compatible
whitespace, including Move arrows without spaces. Link entries are restorable where Git
snapshots exist; outside Git or in denied repositories the link entry itself is not saved. Restore writes
only the requested path (or --to), never an alias's recorded referent. Find may show a plain ledger hint to
search the referent path separately; that hint is not recovery state. Old alias-state files are ignored.
Copy restore metadata is atomic and owner-only beside
its bounded path-hashed copy, not dependent on the best-effort ledger; incomplete new copies are not listed.
Every lookup layout, including legacy names, rejects any known metadata/ledger original-path mismatch.
Unknown metadata-free legacy copies should be inspected with --to, not restored over originals.

The plugin lists only raw-read observations, not index/HEAD placeholders. Git commit trailers and copy
sidecars carry provenance independently of the ledger. Restore defaults to the newest path observation
across branches/backends, not the newest tree containing that path. Old Git trees without provenance and
plain HEAD are deliberately not automatic recovery candidates; use Git for manual inspection.

Turn deduplication accepts only raw-hashed paths under the same HEAD oid and branch. Stat-only baseline
checks cannot suppress an explicit file target. State-changing Git terminal commands invalidate repository
coverage; this heuristic shares the documented direct-command parser limits.

The plugin never requests credentials, fetches remote code, makes network calls or pushes refs. Avoid mirror
pushes and broad refspecs: they can publish shadow snapshots. The guard uses Git's --type=bool semantics,
including bare keys and nonzero integers; invalid mirror values produce a logged skip/error, never a safe
assumption. Removing a shadow ref does not erase its objects;
the plugin never runs `git gc`. Use your repository's normal retention/security procedures.
