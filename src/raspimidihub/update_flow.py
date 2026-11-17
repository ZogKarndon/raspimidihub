"""Update fetch orchestrator.

The hub is Ethernet-only. An update runs when the wired network
(or a USB-tethered phone) can already reach GitHub. There is no
Wi-Fi client switch and no access-point restore.

State is exposed via `/run/raspimidihub/update-status` (JSON). The
UI polls it. Each step writes a breadcrumb so progress is precise.

Storage: `/var/lib/raspimidihub/updates/` keeps deb + sibling
`.changelog.md` files. Newest 3 retained; older pruned per call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import subprocess
import time
import urllib.request
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

UPDATES_DIR = Path("/var/lib/raspimidihub/updates")
STATUS_FILE = Path("/run/raspimidihub/update-status")
INTERNET_PROBE_URL = "https://api.github.com/zen"  # tiny, no rate-limit
INTERNET_PROBE_TIMEOUT_SEC = 4.0
DOWNLOAD_TIMEOUT_SEC = 90.0
KEEP_VERSIONS = 3
WATCHDOG_SECONDS = 180


# --- Status file ----------------------------------------------------------

def write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(STATUS_FILE)


def read_status() -> dict[str, Any]:
    if not STATUS_FILE.is_file():
        return {"step": "idle"}
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"step": "idle"}


# --- Internet reachability ------------------------------------------------

# A plain-HTTP host that reliably returns a Date header. Used for
# bootstrapping the system clock from cold boot — the Pi has no RTC,
# our system-prepare disables NTP, so until the first manual sync the
# clock is stuck at last-shutdown. TLS to api.github.com then fails
# with "certificate not yet valid". Plain HTTP avoids that catch-22.
HTTP_TIME_BOOTSTRAP_URL = "http://www.google.com"
HTTP_TIME_BOOTSTRAP_TIMEOUT_SEC = 5.0


def sync_clock_from_http(url: str = HTTP_TIME_BOOTSTRAP_URL,
                          timeout: float = HTTP_TIME_BOOTSTRAP_TIMEOUT_SEC,
                          drift_threshold_s: float = 60.0) -> bool:
    """Bootstrap the system clock from an HTTP Date header.

    Idempotent: only steps the clock if the local clock is more than
    `drift_threshold_s` seconds off the server-reported time (default
    60 s — tight enough to preempt any TLS / signed-API drift issues
    that show up before the seconds add up). Returns True iff the
    clock was actually adjusted.

    Plain HTTP (not HTTPS) so a wildly-wrong local clock doesn't bork
    the TLS handshake we'd otherwise need to fetch the same Date.
    Setting CLOCK_REALTIME requires CAP_SYS_TIME — raspimidihub.service
    runs as root, so this is fine in production; logs a warning and
    no-ops if it ever isn't.
    """
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    try:
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "raspimidihub-clock-sync"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            date_str = resp.headers.get("Date")
        if not date_str:
            return False
        remote = parsedate_to_datetime(date_str)
    except Exception as e:
        log.info("HTTP time bootstrap failed (no Date or unreachable): %s", e)
        return False

    local = datetime.now(timezone.utc)
    drift = abs((remote - local).total_seconds())
    if drift < drift_threshold_s:
        log.debug("Clock drift %.1fs < %.0fs threshold — leaving clock alone",
                  drift, drift_threshold_s)
        return False

    try:
        time.clock_settime(time.CLOCK_REALTIME, remote.timestamp())
        log.info("System clock synced from %s Date header — drift was %.0fs "
                 "(local %s → remote %s)",
                 url, drift, local.isoformat(), remote.isoformat())
        return True
    except (PermissionError, OSError) as e:
        log.warning("Couldn't step CLOCK_REALTIME (need CAP_SYS_TIME): %s", e)
        return False


def probe_internet(timeout: float = INTERNET_PROBE_TIMEOUT_SEC) -> bool:
    """Sync probe of GitHub's `/zen` endpoint. Returns True if reachable
    (any 2xx/3xx). Called from an executor by async callers."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            INTERNET_PROBE_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "raspimidihub"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 400
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info("Internet probe %s in %dms via %s",
                 "ok" if ok else f"HTTP {resp.status}", elapsed_ms,
                 INTERNET_PROBE_URL)
        return ok
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info("Internet probe failed in %dms: %s", elapsed_ms, e)
        return False


# --- GitHub release list --------------------------------------------------

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/wamdam/raspimidihub/releases?per_page=20"
)
GITHUB_RELEASES_TIMEOUT_SEC = 10.0


def parse_version(s: str) -> tuple:
    """Sortable tuple from a version string. '2.0.10' < '2.0.11' < '3.0.0';
    pre-release tags ('2.0.0-alpha1') sort below the same plain version
    (so a '-alpha1' tag is correctly older than its release counterpart)."""
    s = s.lstrip("v")
    parts = s.replace("-", ".").split(".")
    nums: list[int] = []
    pre = ""
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            pre = p
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums) + (pre or "~",)  # '~' sorts after alpha/beta


def fetch_github_releases() -> list[dict[str, Any]]:
    """Synchronous GET against the GitHub releases endpoint, parsed into
    release entries our UI can render. Caller is responsible for being
    online — the orchestrator's `run()` only invokes work callables once
    that's been verified.

    Returns one dict per release with: version, changelog, deb_url,
    prerelease. Releases without a `raspimidihub_*_all.deb` asset are
    skipped (drafts, accidentally-tagged commits)."""
    t0 = time.monotonic()
    rq = urllib.request.Request(
        GITHUB_RELEASES_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "raspimidihub"},
    )
    log.info("Fetching GitHub releases: %s", GITHUB_RELEASES_URL)
    with urllib.request.urlopen(rq, timeout=GITHUB_RELEASES_TIMEOUT_SEC) as resp:
        releases = json.loads(resp.read())

    out: list[dict[str, Any]] = []
    skipped_drafts = 0
    skipped_no_asset = 0
    for rel in releases:
        if rel.get("draft"):
            skipped_drafts += 1
            continue
        tag = rel.get("tag_name", "")
        ver_str = tag.lstrip("v")
        deb_url = ""
        for asset in rel.get("assets", []):
            name = asset.get("name", "")
            if name.startswith("raspimidihub_") and name.endswith("_all.deb"):
                deb_url = asset.get("browser_download_url", "")
                break
        if not deb_url:
            skipped_no_asset += 1
            continue
        out.append({
            "version": ver_str,
            "changelog": rel.get("body", ""),
            "deb_url": deb_url,
            "prerelease": rel.get("prerelease", False),
        })
    out.sort(key=lambda e: parse_version(e["version"]), reverse=True)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("GitHub returned %d release(s) in %dms (drafts skipped: %d, "
             "no-deb-asset skipped: %d)",
             len(out), elapsed_ms, skipped_drafts, skipped_no_asset)
    for entry in out:
        pre = " [pre-release]" if entry["prerelease"] else ""
        log.info("  - v%s%s → %s", entry["version"], pre, entry["deb_url"])
    return out


# --- Storage --------------------------------------------------------------

@contextlib.contextmanager
def _rw_rootfs():
    """Remount / read-write for the duration of the with-block, then
    back to ro. Required because UPDATES_DIR lives on the rosetup'd
    rootfs which is mounted ro under normal operation."""
    r = subprocess.run(["mount", "-o", "remount,rw", "/"],
                       check=False, capture_output=True, text=True,
                       timeout=5)
    if r.returncode != 0:
        log.error("remount rw / failed (rc=%d): %s", r.returncode,
                  (r.stderr or "").strip())
        # Continue on purpose: the write inside the with-block then fails
        # with a visible OSError instead of failing silently in here.
    try:
        yield
    finally:
        r = subprocess.run(["mount", "-o", "remount,ro", "/"],
                           check=False, capture_output=True, text=True,
                           timeout=5)
        if r.returncode != 0:
            # The root fs is left read-write — the appliance's read-only
            # guarantee is gone until the next boot; make it loud.
            log.error("remount ro / FAILED (rc=%d): %s — root fs left "
                      "read-write", r.returncode, (r.stderr or "").strip())


# Accept an optional pre-release tag after the patch number, in either
# the suffix form (3.0.0a, 3.0.0b2) or the hyphen-separated form
# (5.0.0-alpha1, 5.0.0-rc2) — so prereleases land in the stored list and
# get parsed by parse_version (which sorts them below the matching plain
# version). The deb *filename* uses the hyphen form to match the git tag;
# only the deb's internal Version field uses a tilde (for dpkg/apt).
_DEB_NAME_RE = re.compile(
    r"^raspimidihub_(?P<ver>[0-9]+\.[0-9]+\.[0-9]+(?:-?[a-z]+[0-9]*)?)"
    r"-(?P<rev>\d+)_all\.deb$"
)


def _version_tuple(version: str) -> tuple:
    """Sort key for `list_stored_versions`. Delegates to parse_version
    so 2.0.9 < 3.0.0a < 3.0.0 — same ordering the update-check uses."""
    return parse_version(version)


def list_stored_versions() -> list[dict[str, Any]]:
    """One entry per stored deb, newest first. Each entry has version,
    deb_name, deb_path, size_bytes, downloaded_at (epoch), changelog
    (sibling .changelog.md contents, '' if absent)."""
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for deb in UPDATES_DIR.glob("*.deb"):
        m = _DEB_NAME_RE.match(deb.name)
        if not m:
            continue
        version = m.group("ver")
        changelog_path = deb.with_suffix(".changelog.md")
        try:
            stat = deb.stat()
        except OSError:
            continue
        out.append({
            "version": version,
            "deb_name": deb.name,
            "deb_path": str(deb),
            "size_bytes": stat.st_size,
            "downloaded_at": int(stat.st_mtime),
            "changelog": (
                changelog_path.read_text(errors="replace")
                if changelog_path.is_file() else ""
            ),
        })
    out.sort(key=lambda e: _version_tuple(e["version"]), reverse=True)
    return out


def prune_stored(keep: int = KEEP_VERSIONS,
                 protect: Iterable[str] = ()) -> list[str]:
    """Delete all but the N newest stored debs (and their changelogs).
    Versions listed in `protect` are kept regardless of position —
    used so the running version always survives prune (otherwise a
    user who's upgraded N+1 times can no longer one-tap-rollback to
    the version they're currently running).

    Returns the removed deb names. Wrapped in rw remount because
    UPDATES_DIR is on the read-only rootfs."""
    versions = list_stored_versions()
    protect_set = set(protect)
    keep_set = {v["version"] for v in versions[:keep]} | protect_set
    if all(v["version"] in keep_set for v in versions):
        return []
    removed: list[str] = []
    with _rw_rootfs():
        for entry in versions:
            if entry["version"] in keep_set:
                continue
            deb = Path(entry["deb_path"])
            cl = deb.with_suffix(".changelog.md")
            for p in (deb, cl):
                try:
                    p.unlink()
                except OSError:
                    pass
            removed.append(entry["deb_name"])
    log.info("Pruned %d old deb(s) from %s (kept newest %d, protected %s): %s",
             len(removed), UPDATES_DIR, keep, sorted(protect_set), removed)
    return removed


# --- Watchdog -------------------------------------------------------------

def schedule_watchdog(reason: str) -> bool:
    """Schedule the update watchdog unit. The Ethernet-only orchestrator
    does not call this: there is no Wi-Fi mode to fall back from. Kept
    so a console can still arm the same restart."""
    cmd = [
        "systemd-run",
        "--unit=raspimidihub-update-watchdog",
        f"--on-active={WATCHDOG_SECONDS}s",
        "--description", f"raspimidihub update watchdog: {reason}",
        "/usr/local/bin/raspimidihub-update-watchdog",
        reason,
    ]
    try:
        # Replace any leftover watchdog so we always start a fresh deadline.
        subprocess.run(["systemctl", "stop", "raspimidihub-update-watchdog.timer"],
                       check=False, capture_output=True)
        subprocess.run(["systemctl", "stop", "raspimidihub-update-watchdog.service"],
                       check=False, capture_output=True)
        r = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("watchdog schedule failed: %s", r.stderr.strip())
            return False
        log.info("watchdog scheduled for %ds (reason: %s)", WATCHDOG_SECONDS, reason)
        return True
    except OSError as e:
        log.warning("watchdog schedule errored: %s", e)
        return False


def cancel_watchdog() -> None:
    for name in ("raspimidihub-update-watchdog.timer",
                 "raspimidihub-update-watchdog.service"):
        try:
            subprocess.run(["systemctl", "stop", name],
                           check=False, capture_output=True)
        except OSError:
            pass


# --- Download primitive ---------------------------------------------------

def download_release(deb_url: str, dest: Path,
                      changelog_text: str = "") -> tuple[bool, str]:
    """Synchronous deb download to `dest`. Writes `<dest_stem>.changelog.md`
    alongside if changelog_text is given. Wraps the writes in an rw
    rootfs remount because UPDATES_DIR lives on the read-only root.
    Returns (ok, error_message)."""
    tmp = dest.with_suffix(".part")
    log.info("Downloading %s → %s", deb_url, dest)
    t0 = time.monotonic()
    bytes_written = 0
    try:
        req = urllib.request.Request(
            deb_url, headers={"User-Agent": "raspimidihub"},
        )
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            if resp.status >= 400:
                log.warning("Download HTTP %d for %s", resp.status, deb_url)
                return False, f"HTTP {resp.status}"
            # Hold rw open only for the actual disk writes — the network
            # read happens regardless of fs mode.
            with _rw_rootfs():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        bytes_written += len(chunk)
                tmp.replace(dest)
                if changelog_text:
                    try:
                        dest.with_suffix(".changelog.md").write_text(changelog_text)
                    except OSError as e:
                        log.warning("failed to write changelog alongside %s: %s",
                                    dest, e)
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.warning("Download failed after %dms (%d bytes): %s",
                    elapsed_ms, bytes_written, e)
        try:
            with _rw_rootfs():
                tmp.unlink()
        except OSError:
            pass
        return False, str(e)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("Download ok: %s (%d bytes in %dms)",
             dest.name, bytes_written, elapsed_ms)
    return True, ""


# --- Orchestrator ---------------------------------------------------------

class NoInternetError(Exception):
    """Raised when neither the current network nor a fallback can reach
    the internet. Carries a user-facing message the API surfaces verbatim."""
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UpdateFetcher:
    """Run an async callable once the wired network can reach GitHub.

    `wifi_manager` is accepted and ignored so older callers keep
    working. The hub does not switch radios.
    """

    def __init__(self, wifi_manager, config) -> None:
        self.wifi = wifi_manager
        self.config = config

    async def run(self, work: Callable[[], Awaitable[Any]],
                  *, version_label: str = "") -> Any:
        """Execute `work` when Ethernet (or USB tethering) has internet.
        Raises NoInternetError otherwise."""
        loop = asyncio.get_running_loop()
        log.info("UpdateFetcher.run start (label=%s) — ethernet only",
                 version_label or "-")
        write_status({"step": "probing", "version": version_label})

        # Bootstrap the system clock before TLS. The Pi has no RTC and
        # we disable NTP for offline use, so on a cold boot the clock
        # is stuck at last-shutdown. TLS to api.github.com would then
        # fail with "certificate not yet valid". HTTP HEAD (no TLS)
        # to a known time-emitting host fixes that. No-op if drift is
        # < 60 s.
        await loop.run_in_executor(None, sync_clock_from_http)

        if await loop.run_in_executor(None, probe_internet):
            log.info("Path: wired network has internet")
            return await self._run_work(work, version_label=version_label)

        msg = ("No internet on the wired connection. Plug in Ethernet "
               "or a USB-tethered phone and try again. This hub does "
               "not use Wi-Fi.")
        log.warning("Path: aborting — no internet on Ethernet")
        self._abort("error-no-internet", msg)
        raise NoInternetError(msg)

    async def _run_work(self, work, *, version_label: str):
        """Run the callable and write the terminal 'done' status the UI
        matches on to clear its spinner."""
        result = None
        try:
            result = await work()
            return result
        finally:
            done = {"step": "done", "version": version_label}
            if isinstance(result, dict):
                done.update({k: v for k, v in result.items()
                             if k in ("newly_downloaded", "pruned")})
            log.info("UpdateFetcher.run done: %s", done)
            write_status(done)

    def _abort(self, step: str, message: str) -> None:
        write_status({"step": step, "message": message})


# --- Concrete work callable: fetch + download newer releases ---------------

async def download_newer_releases(current_version: str,
                                   include_prereleases: bool = False
                                   ) -> dict[str, Any]:
    """Async work the orchestrator runs once internet is reachable.

    Pulls the GitHub releases list, downloads every release strictly
    newer than `current_version` that isn't already on disk, and
    prunes back to KEEP_VERSIONS. Each step writes a status breadcrumb
    so the UI poll can show precise progress.

    `include_prereleases` controls whether GitHub prereleases (alpha /
    beta tags) are eligible for download. Default False — the stable
    channel — matches how Chrome / Firefox separate channels.

    Always ensures the running version's own deb stays on disk (and
    survives prune) so a one-tap rollback is possible after upgrade.

    Designed to be passed into `UpdateFetcher.run()`:
        await fetcher.run(lambda: download_newer_releases(
            __version__, include_prereleases=cfg["include_prereleases"]))
    """
    loop = asyncio.get_running_loop()
    log.info("download_newer_releases: current=%s prereleases=%s, fetching list...",
             current_version, include_prereleases)
    write_status({"step": "fetching-release-list"})
    releases = await loop.run_in_executor(None, fetch_github_releases)
    current = parse_version(current_version)
    stored_versions = {v["version"] for v in list_stored_versions()}
    log.info("download_newer_releases: %d already on disk: %s",
             len(stored_versions), sorted(stored_versions))

    # Rollback insurance: the user can switch back to the running
    # version only if its deb is on disk. After a fresh install via
    # apt the deb may not be in /var/lib/raspimidihub/updates/ yet —
    # download it once on the next check. No-op on subsequent checks.
    if current_version not in stored_versions:
        for rel in releases:
            if rel["version"] == current_version:
                log.info("  v%s: downloading (running version, for rollback)",
                         current_version)
                dest = UPDATES_DIR / f"raspimidihub_{current_version}-1_all.deb"
                write_status({"step": "downloading", "version": current_version})
                ok, err = await loop.run_in_executor(
                    None, download_release,
                    rel["deb_url"], dest, rel["changelog"])
                if ok:
                    stored_versions.add(current_version)
                else:
                    log.warning("  v%s: rollback-deb download failed: %s",
                                current_version, err)
                break

    newly_downloaded: list[str] = []
    for rel in releases:
        ver = rel["version"]
        if rel.get("prerelease") and not include_prereleases:
            log.info("  v%s: skipping (prerelease; include_prereleases=False)",
                     ver)
            continue
        if parse_version(ver) <= current:
            log.info("  v%s: skipping (not newer than current v%s)",
                     ver, current_version)
            continue
        if ver in stored_versions:
            log.info("  v%s: skipping (already on disk)", ver)
            continue
        log.info("  v%s: downloading", ver)
        dest = UPDATES_DIR / f"raspimidihub_{ver}-1_all.deb"
        write_status({"step": "downloading", "version": ver})
        ok, err = await loop.run_in_executor(
            None, download_release, rel["deb_url"], dest, rel["changelog"])
        if not ok:
            log.warning("  v%s: download failed: %s", ver, err)
            write_status({"step": "error-download",
                          "version": ver,
                          "message": f"Download failed: {err}"})
            continue
        newly_downloaded.append(ver)

    pruned = prune_stored(protect=[current_version])
    log.info("download_newer_releases: result newly_downloaded=%s pruned=%s",
             newly_downloaded, pruned)
    return {
        "newly_downloaded": newly_downloaded,
        "pruned": pruned,
    }
