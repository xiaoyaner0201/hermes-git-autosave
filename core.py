"""Local shadow-ref snapshots and file copies; no branch, real-index or network writes."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid

REF_PREFIX = "refs/hermes-autosave/"
DEP_DIRS = frozenset({
    "node_modules", "bower_components", "jspm_packages", ".pnpm-store", ".yarn", "vendor",
    ".venv", "venv", "site-packages", "dist-packages", "__pypackages__", ".tox", ".nox", ".eggs",
    "Pods", "Carthage", ".gradle", ".m2", ".cargo", ".bundle",
})
BUILD_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".cache",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".vite", ".angular", ".expo",
    "dist", "build", "out", "target", "coverage", ".coverage", "htmlcov", ".terraform", "DerivedData",
})
DEFAULTS = dict(storage_dir=None, keep_days=3, max_file_mb=10, hook_budget_seconds=8,
                big_repo_entries=20000, max_untracked=2000, extra_skip_dirs=[], deny_toplevels=[])


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().resolve()
    return Path(get_hermes_home()).resolve()


def default_storage() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:
        return hermes_home() / "plugin-data" / "git-autosave"
    return Path(plugin_data_dir("git-autosave"))


def agent_cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
    except ImportError:
        return os.path.abspath(os.path.expanduser(os.environ.get("TERMINAL_CWD") or os.getcwd()))
    return str(resolve_agent_cwd())


def canonical_file(path):
    """Resolve ancestors but not the last component (which may be a Git symlink)."""
    p = Path(path)
    return p.parent.resolve() / p.name


def symlink_paths(path, follow_final=True):
    """Yield lexical paths at every symlink substitution, before collapsing policy names."""
    path = Path(os.path.expanduser(str(path)))
    if not path.is_absolute():
        path = Path.cwd() / path
    yield path
    pending = deque(path.parts[1:])
    current = Path(path.anchor)
    hops = 0
    while pending:
        part = pending.popleft()
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if candidate.is_symlink() and (follow_final or pending):
            hops += 1
            if hops > 40:
                raise RuntimeError("Symlink hop limit exceeded")
            target = Path(os.readlink(candidate))
            target = target if target.is_absolute() else current / target
            yield target.joinpath(*pending)
            pending = deque((*target.parts[1:], *pending))
            current = Path(target.anchor)
        else:
            current = candidate
    yield current


def credential_path(path):
    """Name-only policy: never open a credential store to decide whether it is secret."""
    parts = tuple(path.split("/")) if isinstance(path, str) and os.name != "nt" else Path(path).parts
    parts = tuple(part.casefold() for part in parts)  # Also protect aliases on case-insensitive filesystems.
    name = parts[-1] if parts else ""
    return (name in {".env", "auth.json", ".netrc", ".git-credentials", ".npmrc", ".pypirc"}
            or name.startswith((".env.", "id_rsa", "id_ed25519", "id_ecdsa"))
            or name.endswith((".pem", ".key"))
            or any(part in {".ssh", ".aws", ".docker", ".azure", ".kube"} for part in parts[:-1])
            or (".claude" in parts[:-1] and name == ".credentials.json")
            or any(parts[i:i + 2] in ((".config", "gcloud"), (".config", "gh"))
                   for i in range(len(parts) - 1)))


# ls-files --debug emits five fixed stat lines after each NUL-terminated stage/path.
INDEX_STAT = re.compile(
    r"  ctime: (\d+):(\d+)\n  mtime: (\d+):(\d+)\n  dev: (\d+)\tino: (\d+)\n"
    r"  uid: (\d+)\tgid: (\d+)\n  size: (\d+)\tflags: ([0-9a-fA-F]+)\n")


def resolve_path(path: str, base: str | None = None) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    anchor = os.path.expanduser(base or agent_cwd())
    if not os.path.isabs(anchor):
        anchor = os.path.join(os.getcwd(), anchor)
    return os.path.join(anchor, expanded)  # Preserve lexical policy names and symlink/.. semantics.


class Budget:
    def __init__(self, seconds=8, deadline=None):
        # Reserve time for timeout cleanup and ledger logging.
        self.deadline = deadline if deadline is not None else time.monotonic() + seconds - min(0.1, seconds / 10)

    def left(self):
        left = self.deadline - time.monotonic()
        if left <= 0.02:
            raise TimeoutError("Autosave time budget exhausted")
        return left


@contextmanager
def locked(path: Path, dir_fd=None):
    """Nonblocking advisory lock. Optional pinned parent prevents path substitution."""
    if dir_fd is None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | (os.O_NOFOLLOW if dir_fd is not None else 0)
    fd = os.open(path, flags, 0o600, dir_fd=dir_fd)
    f = os.fdopen(fd, "r+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if not path.stat().st_size:
                f.write(b"0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired and os.name == "nt":
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        f.close()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_regular(path, limit, expected=None):
    """Pin and validate the opened entry, not a pre-open is_file observation.

    NONBLOCK prevents a regular->FIFO swap hanging open; NOFOLLOW rejects a final
    link swap. Neither makes regular filesystem I/O interruptible: hooks also need
    the killable worker boundary. Size growth is bounded while reading.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > limit or
                (expected is not None and (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino))):
            raise ValueError("Source changed identity/type or exceeds size limit")
        content = stream.read(int(limit) + 1)
        if len(content) > limit:
            raise ValueError("Source exceeds size limit")
        return content, info


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Autosave:
    def __init__(self, settings=None):
        raw = settings if isinstance(settings, dict) else {}
        self.settings = {}
        invalid = []
        for key, default in DEFAULTS.items():
            value = raw.get(key, default)
            if key == "storage_dir":
                valid = value is None or (isinstance(value, str) and bool(value.strip()))
            elif key in ("extra_skip_dirs", "deny_toplevels"):
                valid = isinstance(value, list) and all(isinstance(x, str) and bool(x.strip()) for x in value)
            else:
                try:
                    valid = (type(value) in (int, float) and math.isfinite(value) and value > 0
                             and (key not in ("big_repo_entries", "max_untracked") or float(value).is_integer()))
                except (ValueError, OverflowError):
                    valid = False
            self.settings[key] = value if valid else default
            if not valid:
                invalid.append(key)
        self.settings["hook_budget_seconds"] = min(12.0, self.settings["hook_budget_seconds"])
        self.root = Path(os.path.abspath(os.path.expanduser(self.settings["storage_dir"]))) if self.settings["storage_dir"] else None
        self._invalid = invalid
        self._warned = False

    @property
    def storage(self):
        return self.root or default_storage()

    @property
    def state(self):
        return self.storage / "state"

    @property
    def ledger(self):
        return self.storage / "ledger.jsonl"

    @property
    def files(self):
        return self.storage / "files"

    def warn_settings(self):
        if not self._warned:
            self._warned = True
            for key in self._invalid:
                self.log({"kind": "settings", "error": f"Invalid setting {key}; using default"})

    def log(self, record, descriptors=None):
        try:
            if descriptors is not None:
                self.storage_identity(descriptors)
            with locked(Path("ledger.lock") if descriptors else self.state / "ledger.lock",
                        dir_fd=descriptors[2] if descriptors else None):
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                fd = os.open("ledger.jsonl" if descriptors else self.ledger,
                             flags | (os.O_NOFOLLOW if descriptors else 0), 0o600,
                             dir_fd=descriptors[0] if descriptors else None)
                with os.fdopen(fd, "wb") as f:
                    f.write((json.dumps({"at": time.time(), **record}) + "\n").encode())
        except Exception:
            pass  # An unavailable ledger must never affect a tool call.

    def read_ledger(self, days=None, budget=None, dir_fd=None):
        cutoff = time.time() - (days if days is not None else self.settings["keep_days"]) * 86400
        records = []
        try:
            fd = os.open("ledger.jsonl" if dir_fd is not None else self.ledger,
                         os.O_RDONLY | (os.O_NOFOLLOW if dir_fd is not None else 0), dir_fd=dir_fd)
            with os.fdopen(fd, encoding="utf-8") as f:
                for line in f:
                    if budget:
                        budget.left()
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict) and float(rec.get("at", 0)) >= cutoff:
                            records.append(rec)
                    except (ValueError, TypeError):
                        continue
        except OSError as exc:
            if dir_fd is not None and not isinstance(exc, FileNotFoundError):
                raise
        return records

    def commit_observations(self, repo, commit, budget=None, message=None):
        """Only raw reads are observations; index/HEAD placeholders have no provenance."""
        if message is None:
            message = self.git(repo, ["log", "-1", "--format=%B", commit], budget)
        for line in message.splitlines():
            if line.startswith("Autosave-Observed: "):
                value = json.loads(line.removeprefix("Autosave-Observed: "))
                return value if isinstance(value, dict) else {}
        return {}  # Legacy trees cannot prove which paths were read.

    def observation_key(self, floor=0):
        """One durable, locked sequence for Git/copy observations, independent of the ledger."""
        with locked(self.state / "versions.lock"):
            sequence = self.state / "versions.json"
            key = max(time.time_ns(), floor + 1, read_json(sequence).get("at_ns", 0) + 1)
            atomic_json(sequence, {"at_ns": key})
            return key

    def repo_state(self, repo):
        return self.state / "repos" / (hashlib.sha256(os.fsencode(repo)).hexdigest() + ".json")

    def known_repos(self, descriptors=None):
        repos = {r["repo"] for r in self.read_ledger(365000, dir_fd=descriptors[0] if descriptors else None)
                 if isinstance(r.get("repo"), str)}
        if descriptors is None:
            records = [read_json(p) for p in (self.state / "repos").glob("*.json")]
        else:
            records = []
            try:
                fd = os.open("repos", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[2])
            except FileNotFoundError:
                return sorted(repos)
            try:
                for name in os.listdir(fd):
                    if name.endswith(".json"):
                        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                        with os.fdopen(child, encoding="utf-8") as f:
                            try:
                                rec = json.load(f)
                                if isinstance(rec, dict):
                                    records.append(rec)
                            except ValueError:
                                pass
            finally:
                os.close(fd)
        for rec in records:
            if isinstance(rec.get("repo"), str):
                repos.add(rec["repo"])
        return sorted(repos)

    def denied(self, repo):
        p = Path(repo).resolve()
        hh = hermes_home()
        builtins = {Path(p.anchor), Path.home().resolve(), hh, *hh.parents}
        builtins.update(Path(x).expanduser().resolve() for x in self.settings["deny_toplevels"])
        # Construct OS mount points without platform-specific machine paths.
        mounts = [Path(os.sep) / name for name in ("Volumes", "mnt", "media")]
        return (p in builtins or p.parent == hh / "profiles" or any(p.parent == m for m in mounts))

    def content_path(self, path, tracked=False, follow_final=True):
        """Resolve only after checking the lexical path and every bounded link hop."""
        names = DEP_DIRS | set(self.settings["extra_skip_dirs"]) | {".git"}
        if not tracked:
            names |= BUILD_DIRS
        storage = self.storage.resolve()
        try:
            resolved = None
            for candidate in symlink_paths(path, follow_final=follow_final):
                if (credential_path(candidate) or any(part in names for part in candidate.parts)
                        or candidate.is_relative_to(storage)):
                    return None
                resolved = candidate
            return resolved
        except (OSError, RuntimeError):
            return None  # Broken/looping traversal is not evidence of safe content coverage.

    def skip(self, path, tracked=False):
        # Entry protection never needs to follow the final link; content writers check it separately.
        return self.content_path(path, tracked, follow_final=False) is None

    def repo_skipper(self, repo):
        """Check Git's relative POSIX entries without resolving each tracked file."""
        root = Path(repo)  # snapshot_repo already resolved the repository root.
        storage = self.storage.resolve()
        if root.is_relative_to(storage):
            prefix = ()  # The whole repository is inside plugin storage.
        elif storage.is_relative_to(root):
            prefix = storage.relative_to(root).parts
        else:
            prefix = None
        dependencies = DEP_DIRS | set(self.settings["extra_skip_dirs"]) | {".git"}
        untracked_names = dependencies | BUILD_DIRS
        root_dependency = any(part in dependencies for part in root.parts)
        root_build = any(part in BUILD_DIRS for part in root.parts)

        def skip_entry(path, tracked=False):
            parts = path.split("/")
            names = dependencies if tracked else untracked_names
            return (root_dependency or (root_build and not tracked) or
                    any(part in names for part in parts) or
                    (prefix is not None and tuple(parts[:len(prefix)]) == prefix))

        return skip_entry

    def git(self, repo, args, budget=None, index=None, data=None, check=True, extra_env=None):
        budget = budget or Budget(15)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1", GIT_NO_LAZY_FETCH="1")
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        env.update(extra_env or {})
        timeout = budget.left()
        process = subprocess.Popen(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
             "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.splitIndex=false",
             "-c", "splitIndex.sharedIndexExpire=never", "-c", "protocol.allow=never", "-C", str(repo), *args],
            stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            start_new_session=os.name != "nt" and not getattr(self, "_hook_worker", False),
        )
        try:
            stdout, stderr = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill a POSIX process group, including a shell's sleeping child holding our pipes open.
            if os.name != "nt" and not getattr(self, "_hook_worker", False):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            try:
                process.communicate(timeout=0.03)
            except subprocess.TimeoutExpired:
                process.stdout.close()
                process.stderr.close()
            raise
        if check and process.returncode:
            raise RuntimeError(f"git {args[0]}: {os.fsdecode(stderr).strip()[:200]}")
        return os.fsdecode(stdout)

    def toplevel(self, path, budget=None):
        p = Path(path)
        if not p.is_dir():
            p = p.parent
        while not p.is_dir() and p != p.parent:
            p = p.parent
        out = self.git(p, ["rev-parse", "--show-toplevel"], budget, check=False).removesuffix("\n")
        return str(Path(out).resolve()) if out else None

    def classify(self, path, budget=None):
        if self.skip(path, tracked=True):
            return "skip", None
        repo = self.toplevel(path, budget)
        return ("git", repo) if repo and not self.denied(repo) else ("file", repo)

    def remote_safe(self, repo, budget):
        cfg = self.git(repo, ["config", "--get-regexp", r"^remote\..*\.(mirror|push)$"], budget, check=False)
        mirrors = set()
        for line in cfg.splitlines():
            key, _, value = line.partition(" ")
            if key.endswith(".mirror"):
                mirrors.add(key)
            elif "refs/*" in value:
                return False
        for key in mirrors:
            # Git accepts bare keys and nonzero integers as true. Invalid values must raise/skip.
            values = self.git(repo, ["config", "--type=bool", "--get-all", key], budget).splitlines()
            if not values or any(value not in {"true", "false"} for value in values):
                raise RuntimeError("Invalid mirror boolean: " + key)
            if "true" in values:
                return False
        return True

    def repo_lock(self, repo, budget):
        common = self.git(repo, ["rev-parse", "--git-common-dir"], budget).removesuffix("\n")
        return Path(resolve_path(common, repo)) / "hermes-autosave.lock"

    def snapshot_repo(self, repo, paths=(), reason="", budget=None):
        budget = budget or Budget(self.settings["hook_budget_seconds"])
        start = time.monotonic()
        repo = str(Path(repo).resolve())
        result = dict(repo=repo, paths=list(paths), reason=reason, changed=False)
        try:
            if self.denied(repo):
                result["skipped"] = "Denied top-level repository"
            elif not self.remote_safe(repo, budget):
                result["skipped"] = "Mirror or refs/* push remote"
            else:
                with locked(self.repo_lock(repo, budget)):
                    self._snapshot(repo, list(paths), result, budget)
                    result["secs"] = time.monotonic() - start
                    atomic_json(self.repo_state(repo), {"repo": repo, "at": time.time(),
                                "at_ns": result.get("at_ns", read_json(self.repo_state(repo)).get("at_ns", 0)),
                                "slow_at": time.time() if result["secs"] >= 3 else
                                read_json(self.repo_state(repo)).get("slow_at", 0)})
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        result["secs"] = time.monotonic() - start
        self.log({"kind": "repo", **result})
        return result

    def _snapshot(self, repo, paths, result, budget):
        self.refs(repo, budget)  # Refuse the entire namespace if any shadow ref is symbolic.
        branch = self.git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], budget, check=False).strip()
        ref = REF_PREFIX + (branch or "detached-" + hashlib.sha256(os.fsencode(repo)).hexdigest()[:12])
        result["ref"] = ref
        head = self.git(repo, ["rev-parse", "--verify", "--quiet", "HEAD"], budget, check=False).strip()
        prev = self.git(repo, ["rev-parse", "--verify", "--quiet", ref], budget, check=False).strip()
        real = Path(resolve_path(self.git(repo, ["rev-parse", "--git-path", "index"], budget).removesuffix("\n"), repo))
        index_mtime = real.stat().st_mtime_ns if real.exists() else 0
        # Read stat data only. Never ask Git to compare working bytes to an index.
        tracked, stats, sparse = {}, {}, set()
        # Stage-only enumeration keeps paths-only hooks cheap in large repositories.
        for entry in filter(None, self.git(repo, ["ls-files", "--stage", "-v", "-z"], budget).split("\0")):
            budget.left()
            tag, entry = entry[0], entry[2:]
            meta, path = entry.split("\t", 1)
            if tag.upper() == "S":
                sparse.add(path)
            mode, sha, stage = meta.split()
            if stage != "0":
                raise RuntimeError("Unmerged index; snapshot skipped")
            tracked[path] = (mode, sha)
        scoped = (len(tracked) > self.settings["big_repo_entries"] or
                  read_json(self.repo_state(repo)).get("slow_at", 0) >= time.time() - 3600)
        if not scoped:
            raw = self.git(repo, ["ls-files", "--stage", "--debug", "-z"], budget)
            offset = 0
            while offset < len(raw):
                budget.left()
                end = raw.index("\0", offset)
                _, path = raw[offset:end].split("\t", 1)
                match = INDEX_STAT.match(raw, end + 1)
                if not match:
                    raise RuntimeError("Unsupported index stat output; snapshot skipped")
                stats[path] = (*map(int, match.groups()[:9]), int(match[10], 16))
                offset = match.end()
        result["mode"] = "paths-only" if scoped else "full"
        targets = set()
        directories = []
        for path in paths:
            if self.content_path(path, tracked=True, follow_final=False) is None:
                continue
            target = canonical_file(path)
            if target.is_relative_to(repo):
                rel = target.relative_to(repo).as_posix()
                targets.add(rel)
                if target.is_dir() and not target.is_symlink():
                    directories.append(rel.rstrip("/") + "/" if rel != "." else "")
        skip_entry = self.repo_skipper(repo)
        # Evaluate absolute ancestors once, not once per tracked entry in large repositories.
        root_sensitive = credential_path(repo + "/__autosave_probe__")
        root_claude = any(part.casefold() == ".claude" for part in Path(repo).parts)
        credential_prefix = ".config/" if Path(repo).name.casefold() == ".config" else ""

        def credential_entry(path):
            return (root_sensitive or (root_claude and path.rsplit("/", 1)[-1].casefold() == ".credentials.json")
                    or credential_path(credential_prefix + path))

        def included(path):
            return not scoped or path in targets or any(path.startswith(d) for d in directories)

        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="index-", dir=self.state) as temp:
            index = Path(temp) / "index"
            # A fresh HEAD-based index has no cached working-tree stat data or shared-index links.
            self.git(repo, ["read-tree", head] if head else ["read-tree", "--empty"], budget, index)
            baseline = set(filter(None, self.git(repo, ["ls-files", "-z"], budget, index).split("\0")))
            remove = {p for p in baseline | tracked.keys() if skip_entry(p, tracked=True)}
            remove.update(p for p in baseline - tracked.keys() if not credential_entry(p))
            updates = ["0 " + "0" * (len(head) or 40) + "\t" + p for p in remove]
            candidates = []
            covered = set()
            observed = {}
            for path, (mode, sha) in tracked.items():
                budget.left()
                if path in remove or credential_entry(path):
                    continue  # Committed credential entries stay at HEAD; never read working bytes.
                updates.append(f"{mode} {sha}\t{path}")  # Preserve staged and sparse-excluded entries.
                if path in sparse and (path not in targets or not os.path.lexists(os.path.join(repo, path))):
                    continue  # Only explicit, present file targets override skip-worktree baselines.
                if not included(path):
                    continue
                if path in targets or scoped:
                    candidates.append(path)
                    continue
                cs, cn, ms, mn, dev, ino, uid, gid, size, flags = stats[path]
                if flags & 0x40000000:  # skip-worktree: absent sparse entries are not deletions.
                    continue
                try:
                    info = os.lstat(os.path.join(repo, path))
                except FileNotFoundError:
                    candidates.append(path)
                    continue
                saved = (cs * 1000000000 + cn, ms * 1000000000 + mn, dev, ino, uid, gid, size)
                current = (info.st_ctime_ns, info.st_mtime_ns, info.st_dev & 0xffffffff,
                           info.st_ino & 0xffffffff, info.st_uid, info.st_gid, info.st_size)
                executable = stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)
                if (saved != current or info.st_mtime_ns >= index_mtime or
                        (os.name != "nt" and mode in ("100644", "100755") and executable != (mode == "100755"))):
                    candidates.append(path)
            # Scoped targets below already include ignored/untracked explicit paths. Do not enumerate
            # unrelated working-tree entries merely to discard all of them in paths-only mode.
            untracked = ([] if scoped else list(filter(None, self.git(
                repo, ["ls-files", "-o", "--exclude-standard", "-z"], budget).split("\0"))))
            untracked = [p for p in untracked if not skip_entry(p) and not credential_entry(p)]
            if len(untracked) > self.settings["max_untracked"]:
                untracked = [p for p in untracked if p in targets]
            # Explicit paths may be ignored by Git; pre-edit protection still applies.
            candidates = list(dict.fromkeys(candidates + untracked + [
                p for p in targets if p not in tracked and not skip_entry(p) and not credential_entry(p)]))
            regular = []
            parents = {}
            for path in candidates:
                budget.left()
                p = Path(repo) / path
                parent = os.path.dirname(path)
                if parent not in parents:
                    parents[parent] = tuple(dict.fromkeys(symlink_paths(Path(repo) / parent)))
                hops = [directory / p.name for directory in parents[parent]]
                if any(not candidate.is_relative_to(repo) or credential_path(candidate)
                       or skip_entry(candidate.relative_to(repo).as_posix(), tracked=path in tracked)
                       for candidate in hops):
                    continue  # Check every directory-alias hop before reading working bytes.
                try:
                    info = p.lstat()
                except FileNotFoundError:
                    if path in tracked:
                        updates.append("0 " + "0" * len(tracked[path][1]) + "\t" + path)
                    continue
                if stat.S_ISLNK(info.st_mode):
                    if self.content_path(p, tracked=path in tracked, follow_final=False) is None:
                        continue
                    content, mode = os.fsencode(os.readlink(p)), "120000"
                elif stat.S_ISREG(info.st_mode):
                    if info.st_size > self.settings["max_file_mb"] * 1048576:
                        continue
                    try:
                        content, opened = read_regular(p, self.settings["max_file_mb"] * 1048576, info)
                    except (OSError, ValueError):
                        continue  # A raced entry must not become a false observation.
                    mode = (tracked.get(path, ("100644", ""))[0] if os.name == "nt" else
                            "100755" if opened.st_mode & 0o111 else "100644")
                    if not any(c in temp + path for c in "\n\r"):
                        # Batch Git only reads our pinned-byte copies, never reopens
                        # working paths that could now be symlinks/FIFOs/credentials.
                        copy = Path(temp) / ("blob-" + str(len(regular)))
                        fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(content)
                        regular.append((path, mode, str(copy)))
                        continue
                else:
                    continue  # Gitlinks/submodules are not recursively scanned.
                # No git add: clean filters can execute arbitrary repository commands. Store raw bytes instead.
                sha = self.git(repo, ["hash-object", "-w", "--no-filters", "--stdin"], budget, data=content).strip()
                updates.append(f"{mode} {sha}\t{path}")
                observed[path] = [mode, sha]
                covered.add(path)
            if regular:
                # Absolute paths start with '/', never Git's C-quote marker.
                data = os.fsencode("".join(copy + "\n" for _, _, copy in regular))
                hashes = self.git(repo, ["hash-object", "-w", "--no-filters", "--stdin-paths"],
                                  budget, data=data).splitlines()
                if len(hashes) != len(regular):
                    raise RuntimeError("Incomplete batch hash-object result")
                updates.extend(f"{mode} {sha}\t{path}" for (path, mode, _), sha in zip(regular, hashes))
                observed.update((path, [mode, sha]) for (path, mode, _), sha in zip(regular, hashes))
                covered.update(path for path, _, _ in regular)
            if updates:
                self.git(repo, ["update-index", "-z", "--index-info"], budget, index,
                         os.fsencode("\0".join(updates) + "\0"))
            tree = self.git(repo, ["write-tree"], budget, index).strip()
            result["covered_paths"] = [os.path.join(repo, path) for path in sorted(covered)]
            previous_key = 0
            if prev:
                previous_tree, message = self.git(repo, ["log", "-1", "--format=%T%x00%B", prev], budget).split("\0", 1)
                subject = message.splitlines()[0]
                match = re.search(r"\[autosave-key=(\d+)\]$", subject)
                previous_key = int(match[1]) if match else 0
                # Conservative global dedupe: any intervening observation (even another backend/branch)
                # forces a fresh event. Never turn an unobserved tree entry into a new version.
                if (previous_key == read_json(self.state / "versions.json").get("at_ns")
                        and tree == previous_tree):
                    previous = self.commit_observations(repo, prev, budget, message=message)
                    if all(previous.get(path) == value for path, value in observed.items()):
                        return
            at_ns = self.observation_key(max(previous_key, read_json(self.repo_state(repo)).get("at_ns", 0)))
            result["at_ns"] = at_ns
            author = dict(GIT_AUTHOR_NAME="Hermes Autosave", GIT_AUTHOR_EMAIL="autosave@hermes.local",
                          GIT_COMMITTER_NAME="Hermes Autosave", GIT_COMMITTER_EMAIL="autosave@hermes.local")
            message = ("Hermes autosave: " + result["reason"] + f" [autosave-key={at_ns}]\n\n"
                       + "Autosave-Observed: " + json.dumps(observed, separators=(",", ":")) + "\n")
            sha = self.git(repo, ["commit-tree", tree, *(["-p", head] if head else [])],
                           budget, data=message.encode(), extra_env=author).strip()
            self.git(repo, ["update-ref", "--no-deref", "--create-reflog", "-m", "Hermes autosave", ref, sha,
                           prev or "0" * len(sha)], budget)
            result.update(changed=True, commit=sha)

    def copy_relative(self, path):
        p = Path(path)
        # Windows drive colon cannot appear in a filename; drive identity is retained in the layout.
        return Path(p.drive.replace(":", ""), *p.parts[1:]) if p.drive else Path(*p.parts[1:])

    def file_versions(self, path, budget=None):
        path = str(canonical_file(resolve_path(str(path))))
        rel = self.copy_relative(path)
        versions = []
        records = {r.get("copy"): r for r in self.read_ledger(365000, budget=budget) if r.get("kind") == "file"}

        def identity_matches(copy):
            # Every layout arm must reject any known conflicting original identity.
            if budget:
                budget.left()
            return all(record.get("path", path) == path for record in
                       (read_json(copy.with_name(copy.name + ".json")), records.get(str(copy), {})))

        key = hashlib.sha256(os.fsencode(path)).hexdigest()
        for day in sorted(self.files.glob("[0-9]" * 8)):
            objects = day / "objects" / key[:2]
            if objects.is_dir():
                pattern = re.compile(r"v2-" + key + r"\.\d{6}-\d{20}-[0-9a-f]{8}")
                versions.extend(p for p in objects.iterdir() if pattern.fullmatch(p.name) and p.is_file()
                                and self.copy_metadata(p).get("path") == path and identity_matches(p))
            parent = day / rel.parent
            if parent.is_dir():
                pattern = re.compile(re.escape(rel.name) + r"\.\d{6}-(?:\d{20}-)?[0-9a-f]{8}")
                for p in parent.iterdir():
                    legacy = pattern.fullmatch(p.name)
                    current = p.name.startswith("v1-") and pattern.fullmatch(p.name[3:])
                    if (p.is_file() and identity_matches(p)
                            and (legacy or (current and self.copy_metadata(p).get("path") == path))):
                        versions.append(p)
        return sorted(versions, key=self.copy_order)

    def copy_metadata(self, copy, records=None):
        """New versions own their restore metadata; the ledger is only a legacy fallback."""
        metadata = read_json(copy.with_name(copy.name + ".json"))
        if metadata:
            return metadata
        if copy.name.startswith(("v1-", "v2-")):
            return {}  # An unpublished/incomplete version is not restorable.
        if records is None:
            records = {r.get("copy"): r for r in self.read_ledger(365000) if r.get("kind") == "file"}
        return records.get(str(copy), {})

    @staticmethod
    def copy_order(path):
        """Nanosecond creation sequence, with mtime fallback for legacy copy names."""
        match = re.search(r"\.(?:\d{6})-(\d{20})-[0-9a-f]{8}$", path.name)
        return int(match[1]) if match else path.stat().st_mtime_ns

    def backup_file(self, path, reason="", budget=None):
        budget = budget or Budget(8)
        try:
            lexical = resolve_path(str(path))
            if self.skip(lexical):
                return None
            path = str(canonical_file(lexical))
            p = Path(path)
            if (not p.is_file() or p.is_symlink()
                    or p.stat().st_size > self.settings["max_file_mb"] * 1048576):
                return None
            key = hashlib.sha256(os.fsencode(path)).hexdigest()
            with locked(self.state / ("file-" + key + ".lock")):
                budget.left()
                content, opened = read_regular(p, self.settings["max_file_mb"] * 1048576, p.lstat())
                mode = stat.S_IMODE(opened.st_mode)
                digest = hashlib.sha256(content).hexdigest()
                versions = self.file_versions(path, budget)
                records = {r.get("copy"): r for r in self.read_ledger(365000, budget=budget)
                           if r.get("kind") == "file"}
                modes = {str(v): self.copy_metadata(v, records).get("mode") for v in versions}
                # Dedupe the latest observation only: A -> B -> A is three versions.
                for version in versions[-1:]:
                    budget.left()
                    if (self.copy_order(version) == read_json(self.state / "versions.json").get("at_ns")
                            and modes.get(str(version)) == mode and version.name.endswith("-" + digest[:8])
                            and hashlib.sha256(version.read_bytes()).hexdigest() == digest):
                        return None
                at_ns = self.observation_key(max((self.copy_order(v) for v in versions), default=0))
                # No original path components: valid source names can already be at NAME_MAX.
                dst = self.files / time.strftime("%Y%m%d") / "objects" / key[:2] / (
                    "v2-" + key + time.strftime(".%H%M%S-") + f"{at_ns:020d}-" + digest[:8])
                dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                budget.left()
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                metadata = dict(kind="file", path=path, copy=str(dst), sha256=digest, content_id=digest,
                                reason=reason, at_ns=at_ns, mode=mode)
                try:
                    atomic_json(dst.with_name(dst.name + ".json"), metadata)
                except Exception:
                    dst.unlink(missing_ok=True)
                    raise  # A copy without its restore metadata is incomplete, not a successful backup.
                self.log(metadata)
                return str(dst)
        except Exception as exc:
            self.log(dict(kind="file", path=path, error=f"{type(exc).__name__}: {exc}"[:300]))
            return None

    def refs(self, repo, budget=None, descriptors=None):
        lines = self.git(repo, ["for-each-ref", "--format=%(refname) %(symref)", REF_PREFIX], budget).splitlines()
        refs = []
        for line in lines:
            ref, _, target = line.partition(" ")
            if target:
                message = "Symbolic shadow ref; repository skipped: " + ref
                self.log(dict(kind="ref", repo=str(repo), error=message), descriptors=descriptors)
                raise RuntimeError(message)
            refs.append(ref)
        return refs

    def versions(self, repo, budget=None):
        budget = budget or Budget(30)
        versions = []
        for ref in self.refs(repo, budget):
            lines = self.git(repo, ["reflog", "show", "--format=%H %ct %s", ref], budget).splitlines()
            seen = set()
            # Include the tip even if a user expired the reflog manually.
            lines.insert(0, self.git(repo, ["log", "-1", "--format=%H %ct %s", ref], budget).strip())
            for line in lines:
                sha, timestamp, message = line.split(" ", 2)
                if sha not in seen:
                    key = re.search(r"\[autosave-key=(\d+)\]$", message)
                    version = dict(repo=repo, ref=ref, commit=sha, at=int(timestamp), message=message)
                    if key:
                        version["at_ns"] = int(key[1])
                    versions.append(version)
                    seen.add(sha)
        return versions

    @contextmanager
    def storage_dirs(self):
        """Pin storage directories with no-follow descriptors; refuse unsafe platforms."""
        paths = (self.storage, self.files, self.state)
        root = self.storage.resolve()
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise RuntimeError("Unsafe symlinked storage directory: " + str(path))
        if not hasattr(os, "O_NOFOLLOW") or not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("Safe descriptor-based pruning is unavailable on this platform")
        descriptors = []
        try:
            self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for name in root.parts[1:]:
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                    os.close(root_fd)
                    root_fd = child_fd
            except Exception:
                os.close(root_fd)
                raise
            descriptors.append(root_fd)
            for name in ("files", "state"):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                descriptors.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd))
            for name in os.listdir(descriptors[1]):
                if re.fullmatch(r"[0-9]{8}", name):
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[1])
                    os.close(fd)
            yield descriptors
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def storage_identity(self, descriptors):
        """Refuse a replaced root/state; all actual prune I/O still uses pinned descriptors."""
        root = self.storage.lstat()
        pinned = os.fstat(descriptors[0])
        state = os.stat("state", dir_fd=descriptors[0], follow_symlinks=False)
        pinned_state = os.fstat(descriptors[2])
        if ((root.st_dev, root.st_ino) != (pinned.st_dev, pinned.st_ino) or
                (state.st_dev, state.st_ino) != (pinned_state.st_dev, pinned_state.st_ino)):
            raise RuntimeError("Storage directory changed identity during prune")

    def prune(self, dry_run=False):
        result = dict(dry_run=dry_run, refs_deleted=[], reflogs_expired=0, file_days_deleted=[], ledger_kept=0, errors=[])
        try:
            with self.storage_dirs() as descriptors:
                return self._prune(dry_run, result, descriptors)
        except Exception as exc:
            result["errors"].append(str(exc))
            return result

    def _prune(self, dry_run, result, descriptors):
        self.storage_identity(descriptors)
        cutoff = time.time() - self.settings["keep_days"] * 86400
        for repo in self.known_repos(descriptors):
            try:
                budget = Budget(30)
                if self.denied(repo):
                    continue
                with locked(self.repo_lock(repo, budget)):
                    for ref in self.refs(repo, budget, descriptors):
                        tip = int(self.git(repo, ["log", "-1", "--format=%ct", ref], budget).strip())
                        if tip < cutoff:
                            result["refs_deleted"].append(dict(repo=repo, ref=ref))
                            if not dry_run:
                                self.git(repo, ["update-ref", "--no-deref", "-d", ref], budget)
                        elif not dry_run:
                            expire = str(int(cutoff))
                            self.git(repo, ["reflog", "expire", "--no-updateref", "--no-rewrite", "--expire=" + expire,
                                           "--expire-unreachable=" + expire, ref], budget)
                            result["reflogs_expired"] += 1
            except Exception as exc:
                result["errors"].append(f"{repo}: {exc}")
        day_cutoff = time.strftime("%Y%m%d", time.localtime(cutoff))
        try:
            files_fd = descriptors[1]
            days = sorted(name for name in os.listdir(files_fd) if re.fullmatch(r"[0-9]{8}", name))
            # Validate every day before deleting anything, including unexpired days.
            for name in days:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=files_fd)
                os.close(fd)
            for name in days:
                if name < day_cutoff:
                    result["file_days_deleted"].append(name)
                    if not dry_run:
                        shutil.rmtree(name, dir_fd=files_fd)
            self.storage_identity(descriptors)
            root_fd, _, state_fd = descriptors
            with locked(Path("ledger.lock"), dir_fd=state_fd):
                self.storage_identity(descriptors)
                keep = self.read_ledger(dir_fd=root_fd)
                result["ledger_kept"] = len(keep)
                if not dry_run:
                    name = ".ledger-" + uuid.uuid4().hex
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=root_fd)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            for rec in keep:
                                f.write(json.dumps(rec) + "\n")
                        self.storage_identity(descriptors)
                        os.replace(name, "ledger.jsonl", src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    finally:
                        try:
                            os.unlink(name, dir_fd=root_fd)
                        except FileNotFoundError:
                            pass
        except Exception as exc:
            result["errors"].append(str(exc))
        return result
