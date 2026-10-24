"""Index stat caches and private hash copies must not make full snapshots scale with repository size."""
import os

from .conftest import git, shadow


def snapshot_full(service, repo, target, monkeypatch, opened):
    real_open = os.open

    def counting_open(path, *args, **kwargs):
        name = os.fspath(path)
        if name.startswith(str(repo) + os.sep) and ".git" not in name.split(os.sep):
            opened.append(name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)
    try:
        return service.snapshot_repo(str(repo), [str(target)], "test")
    finally:
        monkeypatch.setattr(os, "open", real_open)


def set_index_dev(repo, dev):
    """Rewrite only the cached st_dev of every index entry (as after a reboot renumbers volumes)."""
    git(repo, "update-index", "--index-version", "2")
    path = repo / ".git/index"
    raw = bytearray(path.read_bytes())
    count = int.from_bytes(raw[8:12], "big")
    offset = 12
    for _ in range(count):
        raw[offset + 16:offset + 20] = dev.to_bytes(4, "big")
        flags = int.from_bytes(raw[offset + 60:offset + 62], "big")
        name_end = raw.index(b"\0", offset + 62)
        assert name_end - (offset + 62) == flags & 0xFFF
        offset += (62 + (flags & 0xFFF) + 8) & ~7
    import hashlib
    raw[-20:] = hashlib.sha1(bytes(raw[:-20])).digest()
    path.write_bytes(bytes(raw))


def test_device_renumbering_does_not_rehash_clean_files(service, repo, monkeypatch):
    for i in range(30):
        (repo / f"clean-{i}.txt").write_text(f"clean {i}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "clean files")
    target = repo / "tracked.txt"
    actual = os.lstat(target).st_dev & 0xFFFFFFFF
    set_index_dev(repo, (actual + 7) & 0xFFFFFFFF)
    assert "dev: %d" % ((actual + 7) & 0xFFFFFFFF) in git(repo, "ls-files", "--debug", "--", "clean-0.txt")
    assert git(repo, "status", "--porcelain") == ""  # Git itself ignores st_dev on this platform.
    target.write_text("dirty before tool\n")
    opened = []
    result = snapshot_full(service, repo, target, monkeypatch, opened)
    assert not result.get("error") and result["mode"] == "full", result
    assert git(repo, "show", shadow(repo) + ":tracked.txt") == "dirty before tool"
    assert [os.path.basename(p) for p in opened] == ["tracked.txt"], opened


def test_real_content_change_with_same_device_is_still_saved(service, repo, monkeypatch):
    (repo / "other.txt").write_text("v1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "other")
    (repo / "other.txt").write_text("v2 longer\n")
    opened = []
    result = snapshot_full(service, repo, repo / "tracked.txt", monkeypatch, opened)
    assert not result.get("error"), result
    assert git(repo, "show", shadow(repo) + ":other.txt") == "v2 longer"


def test_hash_copies_live_beside_the_repository_not_in_storage(service, repo, monkeypatch, plugin):
    seen = []
    real = plugin.core.tempfile.TemporaryDirectory

    def recording(*args, **kwargs):
        seen.append(kwargs.get("dir"))
        return real(*args, **kwargs)

    monkeypatch.setattr(plugin.core.tempfile, "TemporaryDirectory", recording)
    (repo / "tracked.txt").write_text("changed\n")
    result = service.snapshot_repo(str(repo), [str(repo / "tracked.txt")], "test")
    assert not result.get("error"), result
    git_dir = os.path.realpath(repo / ".git")
    assert seen and all(os.path.realpath(d).startswith(git_dir + os.sep) or os.path.realpath(d) == git_dir
                        for d in seen), seen
    assert not [p for p in os.listdir(git_dir) if p.startswith("hermes-autosave-index-")]
    assert not any(p.startswith("index-") for p in os.listdir(service.state))


def test_leftover_private_index_from_killed_worker_is_removed(service, repo):
    leftover = repo / ".git" / "hermes-autosave-index-killed"
    leftover.mkdir()
    (leftover / "blob-0").write_text("stale copy\n")
    outside = repo.parent / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep\n")
    (repo / ".git" / "hermes-autosave-index-link").symlink_to(outside, target_is_directory=True)
    (repo / "tracked.txt").write_text("changed\n")
    result = service.snapshot_repo(str(repo), [str(repo / "tracked.txt")], "test")
    assert not result.get("error"), result
    assert not leftover.exists()
    assert (outside / "keep").read_text() == "keep\n"
    assert (repo / ".git" / "hermes-autosave-index-link").is_symlink()
