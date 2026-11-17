"""Tests for the update orchestrator.

Updates run only when the wired network already has internet. These
tests cover version sorting, GitHub release parsing, storage layout,
the status file, and the ethernet-only branch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raspimidihub import update_flow as uf

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_orders_by_major_minor_patch(self):
        assert uf.parse_version("1.0.0") < uf.parse_version("1.0.1")
        assert uf.parse_version("1.0.10") > uf.parse_version("1.0.9")
        assert uf.parse_version("2.0.0") > uf.parse_version("1.99.99")

    def test_strips_v_prefix(self):
        assert uf.parse_version("v2.0.5") == uf.parse_version("2.0.5")

    def test_pre_release_sorts_below_release(self):
        # 2.0.0-alpha1 should be older than 2.0.0
        assert uf.parse_version("2.0.0-alpha1") < uf.parse_version("2.0.0")
        # And older than its successor too.
        assert uf.parse_version("2.0.0-alpha1") < uf.parse_version("2.0.1")

    def test_short_versions_pad_to_three(self):
        # '2' should equal '2.0.0' — patch versions default to 0.
        assert uf.parse_version("2") == uf.parse_version("2.0.0")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _seed_storage(tmp_path: Path, versions: list[str]) -> Path:
    """Create the storage dir with one fake deb (and changelog) per
    version. mtime is monotonic-by-call-order so newer-than ordering
    matches list order."""
    storage = tmp_path / "updates"
    storage.mkdir()
    import time as _t
    for i, v in enumerate(versions):
        deb = storage / f"raspimidihub_{v}-1_all.deb"
        deb.write_bytes(b"\0" * 16)
        cl = storage / f"raspimidihub_{v}-1_all.changelog.md"
        cl.write_text(f"# {v}\n\nNotes for {v}.")
        # Stagger mtimes so test results don't depend on filesystem
        # tie-break behaviour.
        ts = _t.time() - (len(versions) - i)
        import os as _os
        _os.utime(deb, (ts, ts))
        _os.utime(cl, (ts, ts))
    return storage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Redirect UPDATES_DIR to a temp dir for the test."""
    d = tmp_path / "updates"
    monkeypatch.setattr(uf, "UPDATES_DIR", d)
    return d


class TestListStoredVersions:
    def test_empty_dir_returns_empty(self, storage):
        assert uf.list_stored_versions() == []

    def test_creates_dir_on_first_call(self, storage):
        assert not storage.exists()
        uf.list_stored_versions()
        assert storage.exists()

    def test_returns_newest_first(self, tmp_path, monkeypatch):
        d = _seed_storage(tmp_path, ["1.0.0", "2.0.0", "1.5.0"])
        monkeypatch.setattr(uf, "UPDATES_DIR", d)
        result = uf.list_stored_versions()
        assert [r["version"] for r in result] == ["2.0.0", "1.5.0", "1.0.0"]

    def test_includes_changelog_text(self, tmp_path, monkeypatch):
        d = _seed_storage(tmp_path, ["1.0.0"])
        monkeypatch.setattr(uf, "UPDATES_DIR", d)
        [entry] = uf.list_stored_versions()
        assert "Notes for 1.0.0" in entry["changelog"]

    def test_skips_non_matching_filenames(self, tmp_path, monkeypatch):
        d = tmp_path / "updates"
        d.mkdir()
        (d / "raspimidihub_1.0.0-1_all.deb").write_bytes(b"x")
        (d / "stray.deb").write_bytes(b"x")
        (d / "README.md").write_text("hi")
        monkeypatch.setattr(uf, "UPDATES_DIR", d)
        result = uf.list_stored_versions()
        assert [r["version"] for r in result] == ["1.0.0"]

    def test_changelog_optional(self, tmp_path, monkeypatch):
        d = tmp_path / "updates"
        d.mkdir()
        (d / "raspimidihub_1.0.0-1_all.deb").write_bytes(b"x")
        monkeypatch.setattr(uf, "UPDATES_DIR", d)
        [entry] = uf.list_stored_versions()
        assert entry["changelog"] == ""


class TestPruneStored:
    def test_keeps_n_newest(self, tmp_path, monkeypatch):
        d = _seed_storage(tmp_path, ["1.0.0", "2.0.0", "3.0.0", "4.0.0"])
        monkeypatch.setattr(uf, "UPDATES_DIR", d)
        removed = uf.prune_stored(keep=2)
        assert sorted(removed) == [
            "raspimidihub_1.0.0-1_all.deb",
            "raspimidihub_2.0.0-1_all.deb",
        ]
        remaining = [v["version"] for v in uf.list_stored_versions()]
        assert remaining == ["4.0.0", "3.0.0"]

    def test_no_op_when_under_threshold(self, tmp_path, monkeypatch):
        d = _seed_storage(tmp_path, ["1.0.0", "2.0.0"])
        monkeypatch.setattr(uf, "UPDATES_DIR", d)
        assert uf.prune_stored(keep=3) == []

    def test_also_deletes_changelog(self, tmp_path, monkeypatch):
        d = _seed_storage(tmp_path, ["1.0.0", "2.0.0"])
        monkeypatch.setattr(uf, "UPDATES_DIR", d)
        uf.prune_stored(keep=1)
        assert not (d / "raspimidihub_1.0.0-1_all.changelog.md").exists()


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------

class TestStatusFile:
    def test_round_trip(self, tmp_path, monkeypatch):
        f = tmp_path / "update-status"
        monkeypatch.setattr(uf, "STATUS_FILE", f)
        uf.write_status({"step": "downloading", "version": "2.0.0"})
        assert uf.read_status() == {"step": "downloading", "version": "2.0.0"}

    def test_read_returns_idle_when_missing(self, tmp_path, monkeypatch):
        f = tmp_path / "update-status"
        monkeypatch.setattr(uf, "STATUS_FILE", f)
        assert uf.read_status() == {"step": "idle"}

    def test_read_returns_idle_when_corrupt(self, tmp_path, monkeypatch):
        f = tmp_path / "update-status"
        f.write_text("{not json")
        monkeypatch.setattr(uf, "STATUS_FILE", f)
        assert uf.read_status() == {"step": "idle"}

    def test_write_is_atomic(self, tmp_path, monkeypatch):
        # The atomic-rename pattern means a partially-written file
        # never appears at STATUS_FILE — only the .tmp does.
        f = tmp_path / "update-status"
        monkeypatch.setattr(uf, "STATUS_FILE", f)
        uf.write_status({"step": "x"})
        assert json.loads(f.read_text())["step"] == "x"
        # No leftover .tmp after a successful write.
        assert not f.with_suffix(".tmp").exists()


# ---------------------------------------------------------------------------
# Orchestrator branching
# ---------------------------------------------------------------------------

def _make_config() -> SimpleNamespace:
    return SimpleNamespace()


def _redirect_status(monkeypatch, tmp_path):
    monkeypatch.setattr(uf, "STATUS_FILE", tmp_path / "update-status")


class TestUpdateFetcher:
    def test_uses_current_network_when_reachable(self, monkeypatch, tmp_path):
        _redirect_status(monkeypatch, tmp_path)
        monkeypatch.setattr(uf, "probe_internet", lambda timeout=4.0: True)
        fetcher = uf.UpdateFetcher(None, _make_config())

        called = []

        async def work():
            called.append(True)
            return "result"

        result = asyncio.run(fetcher.run(work))
        assert result == "result"
        assert called == [True]
        assert uf.read_status()["step"] == "done"

    def test_no_internet_aborts_without_using_wifi(self, monkeypatch, tmp_path):
        _redirect_status(monkeypatch, tmp_path)
        monkeypatch.setattr(uf, "probe_internet", lambda timeout=4.0: False)
        fetcher = uf.UpdateFetcher(None, _make_config())

        async def work():
            raise AssertionError("work shouldn't run")

        with pytest.raises(uf.NoInternetError) as exc:
            asyncio.run(fetcher.run(work))
        assert "ethernet" in exc.value.message.lower()
        assert "wi-fi" in exc.value.message.lower()
        status = uf.read_status()
        assert status["step"] == "error-no-internet"
