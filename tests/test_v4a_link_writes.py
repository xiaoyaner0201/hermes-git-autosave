"""V4A link writes follow the host: Update headers resolve on the pre-patch filesystem, then
operations apply in order and the write follows whatever entry earlier operations left there."""
import importlib
import os

import pytest

from .conftest import git

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")


def cli_for(plugin):
    return importlib.import_module(plugin.__name__ + ".cli")


def patch_args(*operations):
    return {"mode": "patch", "patch": "*** Begin Patch\n" + "\n".join(operations) + "\n*** End Patch"}


def committed_link(repo, name, text):
    link = repo / name
    link.symlink_to(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return link


def writes(plugin, service, args, base=None):
    hooks = importlib.import_module(plugin.__name__ + ".hooks")
    return [os.path.realpath(t) for _, t in hooks.v4a_link_writes(service, args["patch"], base=base)]


def outside(tmp_path, name, text="IRREPLACEABLE-PREEDIT\n"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_move_then_update_protects_referent(plugin, service, repo, tmp_path):
    victim = outside(tmp_path, "victim")
    link = committed_link(repo, "link", str(victim))
    moved = repo / "moved"
    plugin.make_hook(service)("patch", patch_args(f"*** Move File: {link} -> {moved}", f"*** Update File: {moved}"),
                              turn_id="one")
    os.rename(link, moved)  # What the host does next: move the entry, then write through it.
    moved.resolve().write_text("AFTER-PATCH\n")
    cli = cli_for(plugin)
    assert cli.find(service, str(victim))["versions"]
    cli.restore(service, str(victim))
    assert victim.read_text() == "IRREPLACEABLE-PREEDIT\n"


def test_relative_link_text_resolves_from_new_parent(plugin, service, repo, tmp_path):
    (repo / "sub").mkdir()
    data = outside(repo / "sub", "data")
    link = committed_link(repo, "link", "sub/data")  # Valid at repo/, dangling-relative at repo/sub/.
    args = patch_args(f"*** Move File: {link} -> {repo / 'sub' / 'moved'}", f"*** Update File: {repo / 'sub' / 'moved'}")
    assert writes(plugin, service, args) == []  # Host would read repo/sub/sub/data, which does not exist.
    args = patch_args(f"*** Move File: {link} -> {repo / 'moved'}", f"*** Update File: {repo / 'moved'}")
    assert writes(plugin, service, args) == [os.path.realpath(data)]


def test_chained_moves_follow_original_link(plugin, service, repo, tmp_path):
    victim = outside(tmp_path, "victim")
    link = committed_link(repo, "link", str(victim))
    a, b = repo / "a", repo / "b"
    args = patch_args(f"*** Move File: {link} -> {a}", f"*** Move File: {a} -> {b}", f"*** Update File: {b}")
    assert writes(plugin, service, args) == [os.path.realpath(victim)]


def test_move_then_update_regular_file_has_no_referent(plugin, service, repo):
    (repo / "regular").write_text("R")
    destination = repo / "moved"
    args = patch_args(f"*** Move File: {repo / 'regular'} -> {destination}", f"*** Update File: {destination}")
    assert writes(plugin, service, args) == []


def test_excluded_referent_not_copied(plugin, service, repo, tmp_path):
    secret = tmp_path / "home" / ".aws" / "credentials"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("SYNTHETIC-SECRET")
    link = committed_link(repo, "link", str(secret))
    moved = repo / "moved"
    plugin.make_hook(service)("patch", patch_args(f"*** Move File: {link} -> {moved}", f"*** Update File: {moved}"),
                              turn_id="one")
    assert service.file_versions(str(secret)) == []


# Pre-resolved Update targets, entry identity, later hops and Delete-then-Add.

def test_pre_resolved_update_target_wins_over_incoming_link(plugin, service, repo, tmp_path):
    """Delete moved; Move link -> moved; Update moved: the host rewrote the Update to moved's
    pre-patch referent before applying, so that file (not the incoming link's) is edited."""
    victim, other = outside(tmp_path, "victim"), outside(tmp_path, "other", "OTHER\n")
    committed_link(repo, "moved", str(victim))
    link = committed_link(repo, "link", str(other))
    moved = repo / "moved"
    args = patch_args(f"*** Delete File: {moved}", f"*** Move File: {link} -> {moved}", f"*** Update File: {moved}")
    assert writes(plugin, service, args) == [os.path.realpath(victim)]
    plugin.make_hook(service)("patch", args, turn_id="one")
    assert cli_for(plugin).find(service, str(victim))["versions"]
    assert service.file_versions(str(other)) == []


@pytest.mark.parametrize("spelling", ["./moved", "{repo}/./moved", "sub/../moved"])
def test_equivalent_spellings_share_entry_identity(plugin, service, repo, tmp_path, spelling):
    (repo / "sub").mkdir()
    victim = outside(tmp_path, "victim")
    committed_link(repo, "link", str(victim))
    args = patch_args("*** Move File: link -> moved", f"*** Update File: {spelling.format(repo=repo)}")
    assert writes(plugin, service, args, base=str(repo)) == [os.path.realpath(victim)]


def test_later_hops_use_overlay(plugin, service, repo, tmp_path):
    victim, other = outside(tmp_path, "victim"), outside(tmp_path, "other")
    middle = committed_link(repo, "middle", str(other))
    replacement = committed_link(repo, "replacement", str(victim))
    link = committed_link(repo, "link", str(middle))
    args = patch_args(f"*** Move File: {middle} -> {repo / 'old-middle'}", f"*** Move File: {replacement} -> {middle}",
                      f"*** Move File: {link} -> {repo / 'moved'}", f"*** Update File: {repo / 'moved'}")
    assert writes(plugin, service, args) == [os.path.realpath(victim)]


@pytest.mark.parametrize("tail", [[], ["*** Update File: {moved}\n@@\n-ADDED\n+X"]])
def test_delete_then_add_is_a_new_regular_entry(plugin, service, repo, tmp_path, tail):
    victim = outside(tmp_path, "victim")
    link = committed_link(repo, "link", str(victim))
    moved = repo / "moved"
    args = patch_args(f"*** Move File: {link} -> {moved}", f"*** Delete File: {moved}", f"*** Add File: {moved}\n+ADDED",
                      *(t.format(moved=moved) for t in tail))
    assert writes(plugin, service, args) == []
    plugin.make_hook(service)("patch", args, turn_id="one")
    assert service.file_versions(str(victim)) == []


def test_update_of_moved_away_source_keeps_pre_resolved_target(plugin, service, repo, tmp_path):
    victim = outside(tmp_path, "victim")
    link = committed_link(repo, "link", str(victim))
    args = patch_args(f"*** Move File: {link} -> {repo / 'moved'}", f"*** Update File: {link}")
    assert writes(plugin, service, args) == [os.path.realpath(victim)]


def test_plain_update_through_link_unchanged(plugin, service, repo, tmp_path):
    victim = outside(tmp_path, "victim")
    link = committed_link(repo, "link", str(victim))
    assert writes(plugin, service, patch_args(f"*** Update File: {link}")) == [os.path.realpath(victim)]
    (repo / "plain").write_text("P")
    assert writes(plugin, service, patch_args(f"*** Update File: {repo / 'plain'}")) == []
