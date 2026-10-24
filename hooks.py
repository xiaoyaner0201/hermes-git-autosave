"""Bounded direct-command heuristic, not a shell security or approval parser."""
import io
import os
import re
import shlex
import threading

from . import core
from .isolation import make_hook  # noqa: F401 — public factory used by registration

PATCH_HEADER = re.compile(r"^\*\*\*\s*((?:Update|Add|Delete|Move)[ \t]+File|Move[ \t]+to):\s*(.+?)\s*$", re.MULTILINE)
MAX_COMMAND_CHARS = 65536  # Hard tokenization ceiling; shlex is quadratic on one long token.
_UNPARSED = ()  # Parsed commands are lists; None means opaque, this tuple means omitted.
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
MUTATORS = {"rm", "rmdir", "unlink", "mv", "cp", "rsync", "install", "truncate", "dd", "shred", "patch", "unzip", "chmod", "touch"}
GIT_MUTATORS = {"checkout", "restore", "reset", "clean", "stash", "rebase", "switch", "rm", "mv", "apply", "am",
                "merge", "pull", "cherry-pick", "revert", "checkout-index"}


class CommentStream(io.StringIO):
    """shlex discards comment lines via readline; leave their boundary for read()."""

    def readline(self, size=-1):
        line = super().readline(size)
        if line.endswith("\n"):
            self.seek(self.tell() - 1)
            return line[:-1]
        return line


def oversized(command):
    return isinstance(command, str) and len(command) > MAX_COMMAND_CHARS


def segments(command):
    if not isinstance(command, str) or oversized(command):
        return []
    lexer = shlex.shlex(CommentStream(command), posix=True, punctuation_chars=";&|<>\n")
    lexer.whitespace = " \t\r"  # Newlines are command boundaries, not spaces.
    lexer.whitespace_split = True
    words = list(lexer)
    result, current = [], []
    for word in words:
        if (word not in REDIRECTS and word and all(c in ";&|<>\n" for c in word)
                and any(c in ";&|\n" for c in word)):
            result.append(current)
            current = []
        else:
            current.append(word)
    result.append(current)
    return result


def parsed_segments(command):
    """None means opaque, not harmless: shlex can also reject valid shell heredocs."""
    try:
        return segments(command)
    except ValueError:
        return None


def unwrap(words):
    return unwrap_cwd(words, None)[0]


def unwrap_cwd(words, cwd):
    """Consume wrappers once; env's cwd belongs to this command, not later segments."""
    i = 0
    consuming = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-T"}, "env": {"-u", "--unset", "-C", "--chdir"},
                 "nice": {"-n", "--adjustment"}, "xargs": {"-I", "-n", "-P", "-s", "-d", "-E", "-L"},
                 "time": {"-f", "-o", "--format", "--output"}}
    wrappers = {"sudo", "env", "command", "nohup", "time", "nice", "xargs"}
    while i < len(words):
        if "=" in words[i] and not words[i].startswith("-"):
            i += 1
            continue
        name = os.path.basename(words[i])
        if name not in wrappers:
            break
        i += 1
        while i < len(words) and words[i].startswith("-"):
            option = words[i]
            i += 1
            if option == "--":
                break
            if option in consuming.get(name, set()) and i < len(words):
                if name == "env" and option in {"-C", "--chdir"} and cwd is not None and static_path(words[i]):
                    cwd = core.resolve_path(words[i], cwd)
                i += 1
            elif name == "env" and cwd is not None:
                directory = (option.partition("=")[2] if option.startswith("--chdir=") else
                             option[2:] if option.startswith("-C") else "")
                if static_path(directory):
                    cwd = core.resolve_path(directory, cwd)
    return words[i:], cwd


def git_command(words, cwd):
    i = 1
    while i < len(words) and words[i].startswith("-"):
        option = words[i]
        i += 1
        if option in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}:
            if i >= len(words):
                break
            if option in {"-C", "--work-tree"}:
                cwd = core.resolve_path(words[i], cwd)
            i += 1
        elif option.startswith("--work-tree="):
            cwd = core.resolve_path(option.partition("=")[2], cwd)
        elif option.startswith("-C") and len(option) > 2:
            cwd = core.resolve_path(option[2:], cwd)
    return (words[i] if i < len(words) else ""), words[i + 1:], cwd


def mutating(words):
    words = unwrap(words)
    if not words:
        return False
    name, args = os.path.basename(words[0]), words[1:]
    if name in MUTATORS:
        return True
    if name == "tee":
        return not any(x in {"-a", "--append"} for x in args)
    if name in {"sed", "perl"}:
        return any(x.startswith("--in-place") or (x.startswith("-") and not x.startswith("--") and "i" in x) for x in args)
    if name == "git":
        sub, options, _ = git_command(words, ".")
        return (sub in GIT_MUTATORS or (sub == "read-tree" and any(
                    x.startswith("-") and not x.startswith("--") and "u" in x[1:] for x in options))
                or (sub == "update-index" and any(x.partition("=")[0] in {
                    "--add", "--remove", "--force-remove", "--cacheinfo", "--index-info", "--stdin", "--chmod"
                    } for x in options))
                or (sub == "worktree" and "remove" in options))
    if name == "find":
        return "-delete" in args or any(x in {"-exec", "-execdir"} for x in args)
    if name == "tar":
        return any(x == "--extract" or (not x.startswith("--") and "/" not in x and
                   "x" in x.lstrip("-") and x.lstrip("-").isalpha()) for x in args)
    return False


def redirected(words):
    return [words[i + 1] for i, word in enumerate(words[:-1]) if word in REDIRECTS
            and words[i + 1] not in (os.devnull, "/dev/null", "&1", "&2")]


def destructive(command):
    if oversized(command):
        return True
    commands = parsed_segments(command)
    return commands is None or any(redirected(s) or mutating(s) for s in commands)


def terminal_cwd(args, base=None):
    cwd = core.resolve_path(args["workdir"], base) if args.get("workdir") else base or core.agent_cwd()
    try:
        commands = parsed_segments(args.get("command") or "")
        first, cwd = unwrap_cwd(commands[0], cwd) if commands else ([], cwd)
        if len(first) >= 2 and first[0] == "cd" and static_path(first[1]):
            cwd = core.resolve_path(first[1], cwd)
    except ValueError:
        pass
    return cwd


def static_path(value):
    return bool(value) and not any(c in value for c in "$`*?{}") and value != "-"


def operands(words):
    """Supported unambiguous operands; option arguments are not mistaken for files."""
    name, args = os.path.basename(words[0]), words[1:]
    if name == "dd":
        return [x[3:] for x in args if x.startswith("of=")]
    if name in {"tar", "unzip"}:
        option = "-C" if name == "tar" else "-d"
        return [args[i + 1] for i, x in enumerate(args[:-1]) if x == option] + [
            x.split("=", 1)[1] for x in args if x.startswith("--directory=")]
    consume = {"truncate": {"-s", "--size", "-r", "--reference"}, "sed": {"-e", "-f"},
               "perl": {"-e", "-E"}, "install": {"-m", "-o", "-g", "-t", "--target-directory"},
               "rsync": {"--exclude", "--include", "-e"}}
    result, i = [], 0
    expression = name in {"sed", "perl"} and not any(x in {"-e", "-E", "-f"} for x in args)
    while i < len(args):
        x = args[i]
        i += 1
        if x in consume.get(name, set()):
            if name == "install" and x in {"-t", "--target-directory"} and i < len(args):
                result.append(args[i])
            i += 1
        elif name == "install" and x.startswith("--target-directory="):
            result.append(x.partition("=")[2])
        elif name == "install" and x.startswith("-t") and len(x) > 2:
            result.append(x[2:])
        elif x.startswith("-"):
            continue
        elif expression:
            expression = False
        else:
            result.append(x)
    return result


def entry_key(path):
    """The host's identity for a directory entry: parent fully resolved, last component kept."""
    parent, name = os.path.split(path.rstrip(os.sep) or path)
    while name == "." and parent:
        parent, name = os.path.split(parent)
    if name in ("", ".", ".."):
        return os.path.realpath(path)
    return os.path.join(os.path.realpath(parent or os.curdir), name)


def v4a_link_writes(service, patch, base=None, budget=None):
    """``(header path, pre-patch file)`` for each V4A Update whose write goes through a symlink.

    Mirrors the host: every Update header is resolved on the pre-patch filesystem before any
    operation runs, then operations apply in order and the write follows whatever entry earlier
    Moves, Deletes and Adds left at each hop. Relative link text resolves from the link's current
    parent. Every hop passes the lexical exclusion policy; anything else yields no referent."""
    overlay = {}  # entry key -> pre-patch entry now there, or None (removed, or created by this patch)

    def follow(path):
        linked = False
        for _ in range(40):
            key = entry_key(path)
            entry = overlay.get(key, key)
            if entry is None:
                return None  # Bytes created by this patch, or an entry the host will not find.
            if not os.path.islink(entry):
                # A missing final file means the host's read fails and nothing is written.
                return entry if linked and os.path.exists(entry) else None
            linked = True
            text = os.readlink(entry)
            hop = service.content_path(text if os.path.isabs(text) else os.path.join(os.path.dirname(key), text),
                                       tracked=True, follow_final=False)
            if hop is None:
                return None
            path = str(hop)
        return None

    pairs = []
    for operation, operand in PATCH_HEADER.findall(patch):
        if budget is not None:
            budget.left()
        operation = " ".join(operation.split())
        try:
            if operation == "Move File":
                pair = [p.strip() for p in operand.split("->", 1)]
                if len(pair) == 2:
                    source, destination = (entry_key(core.resolve_path(p, base)) for p in pair)
                    origin = overlay.get(source, source)
                    overlay[source] = None
                    overlay[destination] = origin
            elif operation == "Move to":
                continue
            elif operation == "Delete File":
                overlay[entry_key(core.resolve_path(operand, base))] = None
            elif operation == "Add File":
                resolved = service.content_path(core.resolve_path(operand, base), tracked=True)
                overlay[entry_key(str(resolved) if resolved else core.resolve_path(operand, base))] = None
            elif operation == "Update File":
                lexical = core.resolve_path(operand, base)
                resolved = service.content_path(lexical, tracked=True)  # The host's pre-apply resolution.
                if resolved is None:
                    continue
                linked = entry_key(str(resolved)) != entry_key(lexical)
                target = follow(str(resolved))
                if target is None and linked and os.path.lexists(str(resolved)) and entry_key(str(resolved)) not in overlay:
                    target = str(resolved)
                if target is not None and service.content_path(target, tracked=True, follow_final=False) is not None:
                    pairs.append((lexical, target))
        except (OSError, RuntimeError, ValueError):
            continue
    return pairs


def targets(tool_name, args, base=None, all_terminal=False, content_only=False, *, commands=_UNPARSED):
    if tool_name in ("write_file", "patch"):
        paths = [args["path"]] if args.get("path") else []
        if tool_name == "patch" and args.get("mode") == "patch" and isinstance(args.get("patch"), str):
            for operation, operand in PATCH_HEADER.findall(args["patch"]):
                operation = " ".join(operation.split())
                if operation == "Move File":
                    if not content_only:
                        paths.extend(p.strip() for p in operand.split("->", 1))
                elif not content_only or operation in {"Update File", "Add File"}:
                    paths.append(operand)
        return [core.resolve_path(p, base) for p in dict.fromkeys(paths)], []
    if tool_name != "terminal":
        return [], []
    files, dirs = [], []
    cwd = core.resolve_path(args["workdir"], base) if args.get("workdir") else base or core.agent_cwd()
    if oversized(args.get("command")):
        return [], [cwd]  # Do not parse even a leading cd; conservatively protect the initial repo.
    commands = parsed_segments(args.get("command")) if commands is _UNPARSED else commands
    if commands is None:
        return [], [cwd]  # No operand/cd guesses or non-Git directory copies for an opaque command.
    try:
        for segment in commands:
            # The parent shell opens redirects before env/Git change the child's operand cwd.
            files.extend(core.resolve_path(p, cwd) for p in redirected(segment) if static_path(p))
            command_words = []
            redirect_operand = False
            for word in segment:
                if redirect_operand:
                    redirect_operand = False
                elif word in REDIRECTS:
                    redirect_operand = True
                else:
                    command_words.append(word)
            words, selected_cwd = unwrap_cwd(command_words, cwd)
            if not words:
                continue
            if words[0] == "cd" and len(words) >= 2 and static_path(words[1]):
                cwd = core.resolve_path(words[1], selected_cwd)
                continue
            mutation = mutating(words)
            paths = []
            name = os.path.basename(words[0])
            if name == "git":
                if content_only:
                    continue  # Git checkout/move/delete operands operate on entries.
                sub, options, selected_cwd = git_command(words, selected_cwd)
                if mutation or all_terminal:
                    if "--" in options:
                        paths.extend(options[options.index("--") + 1:])
                    elif sub in {"restore", "rm", "mv"}:
                        paths.extend(operands([sub, *options]))
                    elif sub == "worktree" and "remove" in options:
                        paths.extend(operands(["rm", *options[options.index("remove") + 1:]]))
                    if not paths:
                        dirs.append(selected_cwd)
            elif mutation and (not content_only or name not in {"rm", "rmdir", "unlink", "mv", "chmod", "touch", "find"}):
                paths.extend(operands(words))
            resolved = [core.resolve_path(p, selected_cwd) for p in paths if static_path(p)]
            files.extend(resolved)
            if not resolved and (mutation or all_terminal):
                dirs.append(selected_cwd)
    except (ValueError, TypeError):
        pass
    return list(dict.fromkeys(files)), list(dict.fromkeys(dirs))


def git_state_dirs(args, *, commands=_UNPARSED):
    """Conservatively invalidate for any Git command not known to be read-only."""
    readonly = {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "cat-file",
                "for-each-ref", "show-ref", "describe", "rev-list", "check-ignore", "check-attr", "help", "version"}
    cwd = core.resolve_path(args["workdir"]) if args.get("workdir") else core.agent_cwd()
    commands = parsed_segments(args.get("command")) if commands is _UNPARSED else commands
    if commands is None:
        return [cwd]  # Unknown Git effects: discard only the initial repository's turn coverage.
    result = []
    for segment in commands:
        words, selected = unwrap_cwd(segment, cwd)
        if not words:
            continue
        if words[0] == "cd" and len(words) >= 2 and static_path(words[1]):
            cwd = core.resolve_path(words[1], selected)
        elif os.path.basename(words[0]) == "git":
            sub, _, selected = git_command(words, selected)
            if sub and sub not in readonly:
                result.append(selected)
    return result


def make_inline_hook(service, seen=None):
    """Worker-only implementation. Never run filesystem work in the host callback."""
    seen = {} if seen is None else seen
    mutex = threading.Lock()

    def on_pre_tool_call(tool_name, args, task_id="", **kwargs):
        try:
            if tool_name not in ("write_file", "patch", "terminal") or not isinstance(args, dict):
                return None
            budget = core.Budget(service.settings["hook_budget_seconds"])
            if tool_name == "terminal" and oversized(args.get("command")):
                with mutex:
                    if not getattr(service, "_large_command_warned", False):
                        service._large_command_warned = True
                        service.log(dict(kind="parser", warning="Command exceeds 65536 characters; protecting cwd repo without parsing"))
            commands = _UNPARSED
            invalidated = set()
            if tool_name == "terminal":
                commands = parsed_segments(args.get("command"))
                if commands is None:
                    # One fixed-size diagnostic per degraded call; never include command/exception text.
                    service.log(dict(kind="parser", code="command_parse_fallback",
                                     warning="Command tokenization failed; attempting initial cwd repository protection only"))
                for directory in git_state_dirs(args, commands=commands):
                    budget.left()
                    repo = service.toplevel(directory, budget)
                    if repo:
                        invalidated.add(repo)
                if invalidated:
                    with mutex:
                        for key in list(seen):
                            if key[2] in invalidated:
                                del seen[key]
            files, dirs = targets(tool_name, args, commands=commands)
            v4a = tool_name == "patch" and args.get("mode") == "patch" and isinstance(args.get("patch"), str)
            if v4a:
                # V4A headers are modelled in operation order; only a separate ``path`` argument is direct.
                content_paths = [core.resolve_path(args["path"])] if args.get("path") else []
            else:
                content_paths, _ = targets(tool_name, args, content_only=True, commands=commands)
            writes = []
            for path in content_paths:
                budget.left()
                target = service.content_path(path, tracked=True)
                if target is not None and core.canonical_file(path).is_symlink():
                    writes.append((path, str(target)))
            if v4a:
                writes.extend(v4a_link_writes(service, args["patch"], budget=budget))
            referents = []
            for path, target in writes:
                referents.append(target)
                service.log(dict(kind="link-write", path=str(core.canonical_file(path)), referent=target))
            files = list(dict.fromkeys([*files, *referents]))
            repos = {}
            for path in files + dirs:
                budget.left()
                kind, repo = service.classify(path, budget)
                if kind == "git":
                    repos.setdefault(repo, []).append(path)
                elif kind == "file" and path in files:
                    service.backup_file(path, "Before " + tool_name, budget)
            for repo, paths in repos.items():
                budget.left()
                key = (kwargs.get("session_id") or task_id, kwargs.get("turn_id") or "", repo)
                paths = [str(core.canonical_file(path)) for path in paths]
                if key[1]:
                    head = service.git(repo, ["rev-parse", "--verify", "--quiet", "HEAD"], budget, check=False).strip()
                    branch = service.git(repo, ["symbolic-ref", "--quiet", "HEAD"], budget, check=False).strip()
                    key = (*key, head, branch)
                    with mutex:
                        coverage = seen.get(key, set())
                        paths = [path for path in paths if path not in coverage]
                    if not paths:
                        continue
                result = service.snapshot_repo(repo, paths, "Before " + tool_name, budget)
                if key[1] and repo not in invalidated and not result.get("error") and not result.get("skipped"):
                    with mutex:
                        if key not in seen and len(seen) >= 4096:
                            seen.pop(next(iter(seen)))
                        seen.setdefault(key, set()).update(result.get("covered_paths", ()))
        except Exception as exc:
            try:
                service.log(dict(kind="hook", tool=tool_name, error=f"{type(exc).__name__}: {exc}"[:300]))
            except Exception:
                pass
        return None

    return on_pre_tool_call
