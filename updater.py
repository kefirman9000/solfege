"""
Updates from GitHub, only when the user asks or accepts.

Only published GitHub Releases are offered (not every change pushed to the code), and a download
is installed only if its SHA-256 checksum matches the one GitHub lists for it. Before anything is
replaced, the current version is backed up so it can be rolled back from the Help menu.

Your data folder (settings, error log, backups), the private Python environment and
your own edited genres.json are never overwritten.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

REPO = "kefirman9000/solfege"
BRANCH = "main"
APP_DIR = Path(__file__).resolve().parent
VERSION_FILE = APP_DIR / "VERSION"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"{PAGE_URL}/releases"
BACKUP_DIR = APP_DIR / "data" / "app-backups"
KEEP_APP_BACKUPS = 3

# never touched by an update or a rollback
KEEP = {"data", ".venv", ".git", ".github"}
# the user may have edited these; a changed copy from GitHub is saved next to it instead
USER_EDITABLE = {"genres.json"}


def _ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, timeout: int = 20, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "Solfege-updater"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as r:
        return r.read()


def _parse(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.strip().lstrip("vV").split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def local_version() -> str:
    try:
        return VERSION_FILE.read_text("utf-8").strip() or "0"
    except OSError:
        return "0"


def latest_release() -> dict:
    """{"version", "notes", "zip_url", "sha256", "page"} for the newest published release."""
    rel = json.loads(_get(API_LATEST, timeout=10, accept="application/vnd.github+json"))
    assets = rel.get("assets", [])
    zip_asset = next((a for a in assets if a.get("name", "").lower().endswith(".zip")), None)
    if not zip_asset:
        raise RuntimeError("The latest release has no download attached yet. Try again in a few minutes.")
    sha = ""
    digest = zip_asset.get("digest") or ""
    if digest.startswith("sha256:"):
        sha = digest.split(":", 1)[1]
    if not sha:  # older releases: a SHA256SUMS.txt file next to the zip
        sums = next((a for a in assets if a.get("name", "").upper().startswith("SHA256SUMS")), None)
        if sums:
            for line in _get(sums["browser_download_url"], timeout=20).decode().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[-1].lstrip("*") == zip_asset["name"]:
                    sha = parts[0]
    return {"version": rel.get("tag_name", "").lstrip("vV"), "notes": rel.get("body") or "",
            "zip_url": zip_asset["browser_download_url"], "sha256": sha.lower(),
            "page": rel.get("html_url") or RELEASES_URL}


def check() -> tuple[bool, str, dict]:
    """Returns (newer_available, local_version, release_info). Raises on network problems."""
    loc = local_version()
    rel = latest_release()
    return _parse(rel["version"]) > _parse(loc), loc, rel


# --------------------------------------------------------------------------- #
# Backups of the program itself, for rolling back
# --------------------------------------------------------------------------- #
def _app_files(root: Path):
    for f in root.rglob("*"):
        rel = f.relative_to(root)
        if f.is_file() and rel.parts[0] not in KEEP and "__pycache__" not in rel.parts \
                and not f.name.endswith((".updating", ".tmp")):
            yield f, rel


def backup_current() -> Path:
    dest = BACKUP_DIR / local_version()
    if dest.exists():
        shutil.rmtree(dest)
    for f, rel in _app_files(APP_DIR):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest / rel)
    olds = sorted((d for d in BACKUP_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for d in olds[:-KEEP_APP_BACKUPS]:
        shutil.rmtree(d, ignore_errors=True)
    return dest


def backups() -> list[str]:
    """Versions that can be rolled back to, newest first."""
    if not BACKUP_DIR.is_dir():
        return []
    ds = sorted((d for d in BACKUP_DIR.iterdir() if d.is_dir() and (d / "VERSION").exists()),
                key=lambda d: d.stat().st_mtime, reverse=True)
    return [d.name for d in ds]


def _install_tree(files: dict[str, bytes]) -> dict:
    """Write {relative path: content} over the app folder. Returns what changed."""
    old_req = (APP_DIR / "requirements.txt").read_bytes() if (APP_DIR / "requirements.txt").exists() else b""
    kept: list[str] = []
    for rel_s, blob in files.items():
        rel = Path(rel_s)
        if not rel.parts or rel.parts[0] in KEEP or ".." in rel.parts:
            continue
        dest = APP_DIR / rel
        if rel.as_posix() in USER_EDITABLE and dest.exists():
            if dest.read_bytes() != blob:
                dest.with_name(dest.name + ".new").write_bytes(blob)
                kept.append(rel.as_posix())
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".updating")
        tmp.write_bytes(blob)
        tmp.replace(dest)
    new_req = (APP_DIR / "requirements.txt").read_bytes() if (APP_DIR / "requirements.txt").exists() else b""
    changed = new_req != old_req
    if changed:  # the launcher reinstalls packages on next start
        (APP_DIR / ".venv" / "installed.txt").unlink(missing_ok=True)
    shutil.rmtree(APP_DIR / "__pycache__", ignore_errors=True)
    return {"version": local_version(), "requirements_changed": changed, "kept": kept}


def apply_update(rel: dict, progress=None) -> dict:
    """Download the release, check its checksum, back up the current version, then install."""
    if (APP_DIR / ".git").exists():
        raise RuntimeError("This copy is a git checkout. Use 'git pull' instead.")
    if not rel.get("sha256"):
        raise RuntimeError("This release has no checksum, so it can't be verified. It was not installed.")
    progress and progress("Downloading the update…")
    data = _get(rel["zip_url"], timeout=180)
    progress and progress("Checking the download…")
    got = hashlib.sha256(data).hexdigest()
    if got != rel["sha256"]:
        raise RuntimeError("The download didn't match its checksum (damaged or tampered with), so nothing "
                           "was installed.")
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not names:
            raise RuntimeError("The update was empty.")
        root = names[0].split("/")[0] + "/" if all(n.startswith(names[0].split("/")[0] + "/") for n in names) else ""
        for name in names:
            files[name[len(root):]] = z.read(name)
    if "engine.py" not in files or "VERSION" not in files:
        raise RuntimeError("The update doesn't look like Solfège, so it was not installed.")
    progress and progress("Backing up the current version…")
    backup_current()
    progress and progress("Installing the update…")
    return _install_tree(files)


def rollback(version: str) -> dict:
    """Put a backed-up version back. The version being replaced is backed up first."""
    src = BACKUP_DIR / version
    if not (src / "VERSION").exists():
        raise RuntimeError(f"There is no backup of version {version}.")
    files = {rel.as_posix(): f.read_bytes() for f, rel in _app_files(src)}
    backup_current()
    # genres.json: the backup's copy is the user's own, so put it back as-is
    info = _install_tree({k: v for k, v in files.items() if k not in USER_EDITABLE})
    for k in USER_EDITABLE:
        if k in files:
            (APP_DIR / k).write_bytes(files[k])
    return info
