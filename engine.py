"""
Solfège engine.

Sorts music files into Genre › Artist › Album folders (or another layout) from the tags inside
them. Everything runs on your own computer; nothing is uploaded, and nothing is ever overwritten
or deleted while sorting.
"""
from __future__ import annotations

import csv
import hashlib
import itertools
import json
import os
import re
import shutil
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
GENRES_FILE = APP_DIR / "genres.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "error-log.txt"

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".aac", ".ogg", ".oga", ".opus", ".wav",
              ".aif", ".aiff", ".aifc", ".wma", ".ape", ".wv", ".mpc"}
# formats whose tags can be written safely (fill-in from file names, artist renames)
TAG_WRITABLE = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus"}
# files that travel with a track: same name, different ending
SIDECAR_EXTS = (".lrc", ".cue")
COVER_NAMES = ("cover", "folder", "front", "albumart", "album")
COVER_EXTS = (".jpg", ".jpeg", ".png")

INDEX_NAME = ".solfege-index.json"
LIBRARY_NAME = ".solfege-library.json"
DUPES_NAME = ".solfege-duplicates.json"
JOURNAL_NAME = ".solfege-last-run.json"
LOGS_NAME = ".solfege-logs"
DUPLICATES_DIR = "Duplicates"
UNSORTED = "Unsorted"
UNKNOWN_ARTIST = "Unknown Artist"
COMPILATIONS = "Compilations"
OTHER_GENRES = "Other Genres"
VARIOUS = {"various artists", "various", "va", "various artist", "v.a.", "v/a"}


# --------------------------------------------------------------------------- #
# Safe file writing and settings
# --------------------------------------------------------------------------- #
_TMP_COUNTER = itertools.count()


def _retry_locked(fn, *args):
    """Windows often locks a file for a moment right after it changes (antivirus, OneDrive,
    a music player reading it). Wait and retry instead of failing."""
    import time
    waits = (0.2, 0.5, 1.0, 2.0, 3.0)
    for i in range(len(waits) + 1):
        try:
            return fn(*args)
        except PermissionError as e:
            locked = getattr(e, "winerror", None) in (5, 32, 33) or os.name != "nt"
            if not locked or i == len(waits):
                raise
            time.sleep(waits[i])


def atomic_write_text(path: Path, text: str) -> None:
    """Write a temp file, flush it to disk, then swap it in: a crash leaves the old or the new
    version, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{next(_TMP_COUNTER)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _retry_locked(os.replace, tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    for old in path.parent.glob(path.name + ".*.tmp"):
        try:
            old.unlink()
        except OSError:
            pass


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return default
    except Exception:
        try:  # damaged: keep it aside instead of overwriting it
            path.replace(path.with_name(path.name + ".damaged"))
        except OSError:
            pass
        return default


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_settings(s: dict) -> None:
    atomic_write_text(SETTINGS_FILE, json.dumps(s, indent=2))


# --------------------------------------------------------------------------- #
# Finding music
# --------------------------------------------------------------------------- #
def is_audio(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("._")


def collect_tracks(paths, skip_under: Path | None = None, on_found=None, should_stop=None) -> list[Path]:
    """Expand dropped files/folders (recursively) into a de-duplicated list of music files.
    The output folder itself is skipped, so dropping a parent folder doesn't sort sorted music."""
    found: list[Path] = []
    seen: set[str] = set()
    skip = None
    if skip_under is not None:
        try:
            skip = skip_under.resolve()
        except OSError:
            skip = None

    def add(p: Path):
        k = os.path.normcase(str(p))
        if k not in seen:
            seen.add(k)
            found.append(p)

    for raw in paths:
        p = Path(raw)
        if should_stop and should_stop():
            break
        if p.is_file():
            if is_audio(p):
                add(p)
            continue
        if not p.is_dir():
            continue
        for root, dirs, files in os.walk(p):
            if should_stop and should_stop():
                break
            r = Path(root)
            if skip is not None:
                try:
                    if r.resolve() == skip or skip in r.resolve().parents:
                        dirs[:] = []
                        continue
                except OSError:
                    pass
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                f = r / name
                if is_audio(f):
                    add(f)
            if on_found:
                on_found(len(found), str(r))
    if on_found:
        on_found(len(found), "")
    return found


def sidecars_of(path: Path) -> list[Path]:
    out = []
    for ext in SIDECAR_EXTS:
        for cand in (path.with_suffix(ext), path.with_suffix(ext.upper())):
            if cand.exists() and cand not in out:
                out.append(cand)
                break
    return out


def find_cover(folder: Path) -> Path | None:
    try:
        for f in folder.iterdir():
            if f.is_file() and f.stem.lower() in COVER_NAMES and f.suffix.lower() in COVER_EXTS:
                return f
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
FIELDS = ("artist", "albumartist", "album", "title", "genre", "date", "tracknumber", "discnumber",
          "compilation")
_KEYS = {
    "artist": ("artist", "TPE1", "Author", "Artist", "©ART"),
    "albumartist": ("albumartist", "TPE2", "WM/AlbumArtist", "Album Artist", "AlbumArtist", "aART"),
    "album": ("album", "TALB", "WM/AlbumTitle", "Album", "©alb"),
    "title": ("title", "TIT2", "Title", "©nam"),
    "genre": ("genre", "TCON", "WM/Genre", "Genre", "©gen"),
    "date": ("date", "originaldate", "TDRC", "TYER", "TDOR", "WM/Year", "Year", "©day"),
    "tracknumber": ("tracknumber", "TRCK", "WM/TrackNumber", "Track"),
    "discnumber": ("discnumber", "TPOS", "WM/PartOfSet", "Disc"),
    "compilation": ("compilation", "TCMP", "cpil"),
}


def _id3_genre(s: str) -> str:
    """ID3 genres can be stored as numbers: "(17)" or "17" mean Rock."""
    m = re.fullmatch(r"\(?(\d{1,3})\)?(.*)", s.strip())
    if not m:
        return s
    if m.group(2).strip():
        return m.group(2).strip()
    try:
        from mutagen._constants import GENRES
        n = int(m.group(1))
        return GENRES[n] if n < len(GENRES) else s
    except Exception:
        return s


def _text(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "genres"):  # an ID3 genre frame
        g = list(v.genres)
        return str(g[0]) if g else ""
    if isinstance(v, (list, tuple)):
        if not v:
            return ""
        v = v[0]
    if isinstance(v, tuple):  # MP4 track numbers are (number, total)
        v = v[0]
    s = str(v)
    return s.split("\x00")[0].strip()


def read_tags(path: Path) -> dict:
    """Tags and sound details of one music file. {"error": …} if it can't be read."""
    import mutagen
    try:
        f = mutagen.File(str(path), easy=True)
    except Exception as e:
        return {"error": f"couldn't be read ({type(e).__name__})"}
    if f is None:
        return {"error": "isn't a music file this app can read"}
    meta = {k: "" for k in FIELDS}
    tags = f.tags
    if tags is not None:
        for fld, keys in _KEYS.items():
            for k in keys:
                try:
                    v = tags.get(k)
                except Exception:
                    v = None
                s = _text(v)
                if s:
                    meta[fld] = s
                    break
    meta["genre"] = _id3_genre(meta["genre"]) if meta["genre"] else ""
    info = getattr(f, "info", None)
    meta["length"] = float(getattr(info, "length", 0) or 0)
    meta["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
    meta["sample_rate"] = int(getattr(info, "sample_rate", 0) or 0)
    meta["channels"] = int(getattr(info, "channels", 0) or 0)
    meta["bits"] = int(getattr(info, "bits_per_sample", 0) or 0)
    return meta


def write_tags(path: Path, changes: dict) -> bool:
    """Write tags safely: the change is made to a temporary copy, which is checked to still open
    as music, then swapped in. A crash or full disk can't leave a half-written track."""
    if not changes or path.suffix.lower() not in TAG_WRITABLE:
        return False
    import mutagen
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{next(_TMP_COUNTER)}.tagging")
    try:
        shutil.copy2(path, tmp)
        f = mutagen.File(str(tmp), easy=True)
        if f is None:
            return False
        if f.tags is None:
            f.add_tags()
        for k, v in changes.items():
            try:
                if v:
                    f[k] = [v]
                elif k in f:
                    del f[k]
            except (KeyError, ValueError, TypeError):
                pass  # this format has no such tag
        f.save()
        check = mutagen.File(str(tmp), easy=True)
        before = mutagen.File(str(path), easy=True)
        if check is None or abs(getattr(check.info, "length", 0) - getattr(before.info, "length", 0)) > 1:
            raise RuntimeError("the re-tagged file didn't check out")
        _retry_locked(os.replace, tmp, path)
        return True
    except Exception as e:
        log_error(f"Writing tags to {path.name}", e)
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


_NAME_PATTERNS = [
    re.compile(r"^\s*\d{1,3}\s*[-._)]?\s*(?P<artist>.+?)\s+-\s+(?P<title>.+)$"),  # 01 - Artist - Title
    re.compile(r"^(?P<artist>[^-]+?)\s+-\s+(?P<title>.+)$"),                      # Artist - Title
    re.compile(r"^\s*(?P<track>\d{1,3})\s*[-._)]\s*(?P<title>.+)$"),              # 01 - Title
]


def guess_from_name(path: Path) -> dict:
    """Artist and title from file names like "Artist - Title.mp3" or "03 - Title.mp3"."""
    stem = path.stem.replace("_", " ").strip()
    for pat in _NAME_PATTERNS:
        m = pat.match(stem)
        if m:
            g = {k: " ".join(v.split()) for k, v in m.groupdict().items() if v}
            if g.get("artist", "x").isdigit():  # "2024 - Title" isn't an artist
                continue
            return g
    return {"title": " ".join(stem.split())} if stem else {}


# --------------------------------------------------------------------------- #
# Names and genres
# --------------------------------------------------------------------------- #
def safe_folder(name: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip(" .")
    cleaned = cleaned[:120].strip(" .")
    if cleaned.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", cleaned):
        cleaned += "_"
    return cleaned or "Unnamed"


def tidy(s: str) -> str:
    return " ".join((s or "").split())


def name_key(name: str) -> str:
    """Two spellings of one artist? Capitals, extra spaces, a trailing full stop and
    "Beatles, The" vs "The Beatles" don't matter."""
    n = tidy(name).rstrip(". ").casefold()
    if n.endswith(", the"):
        n = "the " + n[:-5]
    return n


def display_name(name: str) -> str:
    n = tidy(name)
    m = re.fullmatch(r"(?i)(.+),\s*the", n)
    return f"The {m.group(1)}" if m else n


_FEAT = re.compile(r"\s*(?:[\(\[]\s*)?\b(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)


def primary_artist(artist: str) -> str:
    """The main artist: "Artist feat. Guest" → "Artist"; "A; B" or "A / B" (several values) → "A"."""
    a = re.split(r"\s*;\s*|\s+/\s+|\x00", artist or "")[0]
    return tidy(_FEAT.sub("", a)) or tidy(a)


def is_compilation(meta: dict) -> bool:
    return name_key(meta.get("albumartist", "")) in VARIOUS or \
        str(meta.get("compilation", "")).strip().lower() in ("1", "true", "yes")


def year_of(meta: dict) -> str:
    m = re.search(r"(1[89]\d\d|20\d\d)", meta.get("date", "") or "")
    return m.group(1) if m else ""


def _norm_genre(s: str) -> str:
    s = s.casefold().replace("&", " and ").replace("-", " ").replace("'", "")
    s = re.sub(r"[^\w ]+", " ", s)
    s = re.sub(r"\bn\b", "and", s)  # "rock n roll"
    return " ".join(s.split())


def load_genres(path: Path = GENRES_FILE) -> list[tuple[str, str, list[str]]]:
    """[(genre, sub-genre, [names it matches])] from genres.json. Raises ValueError on a typo."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"genres.json has a typo on line {e.lineno}: {e.msg}") from e
    leaves = []
    for genre, subs in data.items():
        if genre.startswith("_") or not isinstance(subs, dict):
            continue
        # "_names": tags that belong in the genre folder itself, not a sub-genre
        top = subs.get("_names", [])
        leaves.append((genre, "", [n for n in top if isinstance(n, str)] + [genre]))
        for sub, names in subs.items():
            if sub.startswith("_"):
                continue
            leaves.append((genre, sub, [n for n in names if isinstance(n, str)]))
    if not leaves:
        raise ValueError("genres.json has no genres in it.")
    return leaves


def match_genre(text: str, leaves) -> tuple[str, str] | None:
    """The genre and sub-genre a genre tag belongs in. A tag holding several genres
    ("Rock; Pop", "Rock/Pop") is matched on the first one that fits. Longest name wins,
    so "progressive rock" beats "rock"."""
    if not text:
        return None
    parts = [p for p in re.split(r"\s*[;/,|]\s*|\x00", text) if p.strip()] or [text]
    for part in parts:
        t = _norm_genre(part)
        best, best_len = None, -1
        for genre, sub, names in leaves:
            for n in names + [sub]:
                nn = _norm_genre(n)
                if not nn:
                    continue
                if t == nn or re.search(rf"(?:^| ){re.escape(nn)}(?: |$)", t):
                    score = len(nn) + (1000 if t == nn else 0)
                    if score > best_len:
                        best, best_len = (genre, sub), score
        if best:
            return best
    return None


# --------------------------------------------------------------------------- #
# Folder layouts and file names
# --------------------------------------------------------------------------- #
LAYOUTS = {
    "genre": "Genre › Sub-genre › Artist › Album",
    "genre_top": "Genre › Artist › Album",
    "artist": "Artist › Album (no genres)",
    "year": "Year › Artist › Album",
    "decade_genre": "Decade › Genre › Artist › Album",
    "genre_only": "Genre › Sub-genre (tracks loose inside)",
    "artist_year": "Artist › Year - Album (albums in release order)",
    "az_artist": "A–Z › Artist › Album",
    "artist_only": "Artist (tracks loose inside, no album folders)",
}
RENAME_MODES = {
    "album": "Add the album (Song (Album).mp3)",
    "artist": "Add the artist (Song (Artist).mp3)",
    "folder": "Add the folder it came from (Song_OldFolder.mp3)",
    "number": "Add a number (Song_1.mp3)",
}
NAMING = {
    "keep": "Keep the original file names",
    "track_title": "Track number - Title (01 - Song.mp3)",
    "artist_title": "Artist - Title (Artist - Song.mp3)",
}
UNKNOWN_OPTIONS = {
    "other": "Get their own folder under Other Genres",
    "unsorted": "Go to Unsorted",
}


def letter_of(artist: str) -> str:
    """The A–Z folder for an artist: "The Beatles" → B, "Édith Piaf" → E, "2Pac" → #."""
    import unicodedata
    n = re.sub(r"(?i)^(the|a|an)\s+", "", tidy(artist)) or tidy(artist)
    c = unicodedata.normalize("NFKD", n[:1])[:1].upper()
    return c if "A" <= c <= "Z" else "#"


def folder_for(output: Path, meta: dict, layout: str) -> Path:
    genre, sub = meta.get("_genre", UNSORTED), meta.get("_sub", "")
    artist, album = meta.get("_artist", UNKNOWN_ARTIST), meta.get("album", "")
    g = [safe_folder(genre)] + ([safe_folder(sub)] if sub else [])
    a = [safe_folder(artist)] + ([safe_folder(album)] if album else [])
    y = year_of(meta)
    ya = [safe_folder(artist)] + ([safe_folder(f"{y} - {album}" if y else album)] if album else [])
    parts = {
        "artist_year": ya,
        "az_artist": ([letter_of(artist)] if artist not in (COMPILATIONS, UNKNOWN_ARTIST) else []) + a,
        "artist_only": [safe_folder(artist)],
        "genre": g + a,
        "genre_top": [safe_folder(genre)] + a,
        "artist": a,
        "year": [y or "Unknown Year"] + a,
        "decade_genre": [f"{y[:3]}0s" if y else "Unknown Decade", safe_folder(genre)] + a,
        "genre_only": g,
    }.get(layout, g + a)
    folder = output
    for p in parts:
        folder = folder / p
    return folder


def _track_no(meta: dict) -> str:
    m = re.match(r"\s*(\d+)", meta.get("tracknumber", "") or "")
    return f"{int(m.group(1)):02d}" if m else ""


def target_name(src: Path, meta: dict, naming: str) -> str:
    title = tidy(meta.get("title", ""))
    if naming == "keep" or not title:
        return src.name
    if naming == "track_title":
        n = _track_no(meta)
        d = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", meta.get("discnumber", "") or "")
        if n and d and (int(d.group(1)) > 1 or int(d.group(2) or 1) > 1):
            n = f"{int(d.group(1))}-{n}"  # several discs: 1-01, 2-01
        stem = f"{n} - {title}" if n else title
    else:
        artist = meta.get("_artist") if meta.get("_artist") not in (None, COMPILATIONS, UNKNOWN_ARTIST) \
            else primary_artist(meta.get("artist", ""))
        stem = f"{artist} - {title}" if artist else title
    return safe_folder(stem) + src.suffix.lower()


def _taken(dest: Path, reserved: set[str] | None) -> bool:
    return dest.exists() or (reserved is not None and os.path.normcase(str(dest)) in reserved)


def unique_dest(folder: Path, name: str, reserved: set[str] | None = None) -> Path:
    dest = folder / name
    stem, ext = Path(name).stem, Path(name).suffix
    i = 1
    while _taken(dest, reserved):
        dest = folder / f"{stem}_{i}{ext}"
        i += 1
    return dest


def clash_dest(src: Path, folder: Path, name: str, meta: dict, mode: str,
               reserved: set[str] | None = None) -> Path:
    """Where a track goes: its name, or (if a different track already has it) a telling new name.
    A number is added on top if even that is taken. Nothing is ever overwritten."""
    dest = folder / name
    if not _taken(dest, reserved):
        return dest
    stem, ext = Path(name).stem, Path(name).suffix
    tag = {"album": meta.get("album", ""), "artist": primary_artist(meta.get("artist", "")),
           "folder": src.parent.name}.get(mode, "")
    if tag:
        name = safe_folder(f"{stem}_{tag}" if mode == "folder" else f"{stem} ({tag})") + ext
    return unique_dest(folder, name, reserved)


# --------------------------------------------------------------------------- #
# Copying and moving, never overwriting, every copy checked
# --------------------------------------------------------------------------- #
class CopyCheckFailed(OSError):
    pass


def file_hash(path: Path) -> str:
    """SHA-256 of the file's bytes: equal only for exact, byte-for-byte copies."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_range(f, start: int, end: int) -> str:
    h = hashlib.sha256()
    f.seek(start)
    left = end - start
    while left > 0:
        chunk = f.read(min(1 << 20, left))
        if not chunk:
            break
        h.update(chunk)
        left -= len(chunk)
    return h.hexdigest()


def audio_hash(path: Path) -> str | None:
    """SHA-256 of only the sound inside an MP3, FLAC or WAV, leaving out the tags. Two files with
    the same sound but different tags (a corrected title, added cover art) get the same value.
    A re-encoded, trimmed or different-bitrate version is different sound and doesn't match.
    None for formats this can't separate (then only exact copies count)."""
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if ext == ".mp3":
                start, end = 0, size
                head = f.read(10)
                while head[:3] == b"ID3" and len(head) == 10:  # ID3v2 (can be repeated)
                    n = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                    start += 10 + n + (10 if head[5] & 0x10 else 0)
                    f.seek(start)
                    head = f.read(10)
                if size >= 128:
                    f.seek(size - 128)
                    if f.read(3) == b"TAG":  # ID3v1
                        end = size - 128
                if end - 32 > start:  # APEv2 footer
                    f.seek(end - 32)
                    foot = f.read(32)
                    if foot[:8] == b"APETAGEX":
                        n = struct.unpack("<I", foot[12:16])[0]
                        has_header = struct.unpack("<I", foot[20:24])[0] & 0x80000000
                        end -= n + (32 if has_header else 0)
                return _hash_range(f, start, end) if end > start else None
            if ext == ".flac":
                head = f.read(4)
                pos = 0
                if head[:3] == b"ID3":
                    f.seek(6)
                    b = f.read(4)
                    pos = 10 + ((b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3])
                    f.seek(pos)
                    head = f.read(4)
                if head != b"fLaC":
                    return None
                pos += 4
                while True:
                    f.seek(pos)
                    bh = f.read(4)
                    if len(bh) < 4:
                        return None
                    last = bh[0] & 0x80
                    pos += 4 + int.from_bytes(bh[1:4], "big")
                    if last:
                        break
                return _hash_range(f, pos, size)
            if ext == ".wav":
                if f.read(4) != b"RIFF":
                    return None
                f.seek(12)
                h = hashlib.sha256()
                found = False
                while True:
                    ch = f.read(8)
                    if len(ch) < 8:
                        break
                    cid, n = ch[:4], struct.unpack("<I", ch[4:])[0]
                    here = f.tell()
                    if cid in (b"fmt ", b"data"):
                        h.update(cid)
                        h.update(bytes.fromhex(_hash_range(f, here, here + n)))
                        found = found or cid == b"data"
                    f.seek(here + n + (n & 1))
                return h.hexdigest() if found else None
    except (OSError, struct.error, IndexError, ValueError):
        return None
    return None


def _same_drive(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _verify_copy(dest: Path, expected: str):
    if file_hash(dest) != expected:
        dest.unlink(missing_ok=True)
        raise CopyCheckFailed("the copy didn't match the original byte for byte (disk or drive problem?), "
                              "so the original was left where it is")


def transfer(src: Path, dest: Path, move: bool, digest: str | None = None, verify: bool = True) -> Path:
    """Copy or move one file to an exact destination that must be free. A move on the same drive is
    a rename; a move to another drive copies, checks the copy, and only then removes the original."""
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move and _same_drive(src, dest.parent):
        _retry_locked(os.rename, src, dest)
        return dest
    try:
        _retry_locked(shutil.copy2, src, dest)
    except Exception:
        dest.unlink(missing_ok=True)  # never leave a partial copy behind
        raise
    if verify or move:
        _verify_copy(dest, digest or file_hash(src))
    if move:
        _retry_locked(src.unlink)
    return dest


def place_file(src: Path, dest: Path, move: bool, digest: str | None = None, verify: bool = True) -> Path:
    """Copy or move a track to dest, bringing its lyrics (.lrc) and cue sheet along."""
    cars = sidecars_of(src)
    transfer(src, dest, move, digest, verify)
    for car in cars:
        target = dest.with_suffix(car.suffix.lower())
        if not target.exists():
            try:
                transfer(car, target, move, verify=verify)
            except Exception as e:
                log_error(f"Bringing {car.name} along", e)
    return dest


def can_use_trash() -> bool:
    try:
        import send2trash  # noqa: F401
        return True
    except ImportError:
        return False


def remove_file(path: Path, trash: bool = True, sidecars: bool = True):
    """Delete a file (and its .lrc/.cue) to the Recycle Bin / Trash when possible."""
    try:
        from send2trash import send2trash
    except ImportError:
        send2trash = None
    for f in [path] + (sidecars_of(path) if sidecars else []):
        if not f.exists():
            continue
        if send2trash and trash:
            send2trash(str(f))
        else:
            f.unlink(missing_ok=True)


def remove_empty_dirs(folder: Path, stop: Path):
    """Remove folders left completely empty, up to (not including) stop. Never removes files."""
    try:
        folder, stop = folder.resolve(), stop.resolve()
    except OSError:
        return
    while folder != stop and stop in folder.parents:
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            return
        folder = folder.parent


def free_space(folder: Path) -> int:
    p = folder
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 1 << 62


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_length(sec: float) -> str:
    sec = int(round(sec or 0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


# --------------------------------------------------------------------------- #
# What's in the output folder: copy index, library, duplicates, undo journal, logs
# --------------------------------------------------------------------------- #
def _rel(output: Path, p: Path) -> str:
    try:
        return p.relative_to(output).as_posix()
    except ValueError:
        return str(p)


def _abs(output: Path, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else output / p


class CopyIndex:
    """Remembers the content hash of every track sorted into one output folder (before any tags
    were filled in), so copies are caught in later runs too. "a:…" keys are sound-only hashes."""

    def __init__(self, output: Path):
        self.output = output
        self.path = output / INDEX_NAME
        self.map: dict[str, str] = _load_json(self.path, {})

    def find(self, key: str | None) -> Path | None:
        if not key:
            return None
        rel = self.map.get(key)
        if not rel:
            return None
        p = _abs(self.output, rel)
        if p.exists():
            return p
        del self.map[key]  # that sorted track was deleted since; not a copy any more
        return None

    def add(self, key: str, dest: Path):
        self.map[key] = _rel(self.output, dest)

    def moved(self, old_rel: str, new_rel: str):
        for k, v in self.map.items():
            if v == old_rel:
                self.map[k] = new_rel

    def drop(self, rel: str):
        for k in [k for k, v in self.map.items() if v == rel]:
            del self.map[k]

    def save(self):
        try:
            atomic_write_text(self.path, json.dumps(self.map))
        except OSError as e:
            log_error("Saving the copy index", e)


class Library:
    """Tags of every sorted track (for the Artists and Search tabs), the folder each album went to
    (so an album is never split across folders) and the spelling used for each artist."""
    KEEP = ("artist", "albumartist", "album", "title", "genre", "date", "tracknumber", "discnumber",
            "length", "bitrate", "_genre", "_sub", "_artist")

    def __init__(self, output: Path):
        self.output = output
        self.path = output / LIBRARY_NAME
        d = _load_json(self.path, {})
        self.tracks: dict[str, dict] = d.get("tracks", {})
        self.albums: dict[str, str] = d.get("albums", {})
        self.names: dict[str, str] = d.get("names", {})

    def canonical(self, name: str) -> str:
        """The spelling already in use for this artist, so "the beatles" joins "The Beatles"."""
        n = display_name(name)
        return self.names.setdefault(name_key(n), n) if n else n

    @staticmethod
    def album_key(layout: str, meta: dict) -> str:
        return "|".join((layout, name_key(meta.get("_artist", "")), name_key(meta.get("album", ""))))

    def add(self, dest: Path, meta: dict):
        self.tracks[_rel(self.output, dest)] = {k: meta.get(k, "") for k in self.KEEP}

    def drop(self, rel: str):
        self.tracks.pop(rel, None)

    def save(self):
        try:
            atomic_write_text(self.path, json.dumps({"tracks": self.tracks, "albums": self.albums,
                                                     "names": self.names}))
        except OSError as e:
            log_error("Saving the library", e)

    def existing(self) -> dict[str, dict]:
        """Tracks still where the library says (deleted ones are forgotten)."""
        gone = [r for r in self.tracks if not _abs(self.output, r).exists()]
        for r in gone:
            del self.tracks[r]
        return self.tracks


class RunJournal:
    """What the most recent sort did, so it can be undone."""

    def __init__(self, output: Path):
        self.path = output / JOURNAL_NAME
        self.entries: list[dict] = []

    def start(self):
        self.entries = []
        self.save()

    def add(self, src: Path, dest: Path, move: bool, kind: str = "sorted"):
        self.entries.append({"src": str(src), "dest": str(dest), "move": move, "kind": kind})
        self.save()

    def save(self):
        atomic_write_text(self.path, json.dumps({"entries": self.entries,
                                                 "when": datetime.now().isoformat(timespec="seconds")}))

    def load(self) -> list[dict]:
        return _load_json(self.path, {}).get("entries", [])


class RunLog:
    """A spreadsheet (CSV, opens in Excel) of what happened to every track in one run."""
    COLS = ["Time", "Track", "From", "To", "Genre", "Sub-genre", "Artist", "Album", "Title", "Result",
            "Problem"]

    def __init__(self, output: Path):
        folder = output / LOGS_NAME
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"sort-{datetime.now():%Y-%m-%d_%H%M%S}.csv"
        self.f = open(self.path, "w", newline="", encoding="utf-8-sig")
        self.w = csv.writer(self.f)
        self.w.writerow(self.COLS)

    def row(self, r: "SortResult"):
        m = r.meta or {}
        self.w.writerow([f"{datetime.now():%H:%M:%S}", r.source.name, str(r.source), str(r.dest or ""),
                         m.get("_genre", ""), m.get("_sub", ""), m.get("_artist", ""), m.get("album", ""),
                         m.get("title", ""), r.outcome(), r.error])
        self.f.flush()

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def _dupes_map(output: Path) -> dict:
    return _load_json(output / DUPES_NAME, {})


def record_duplicate(output: Path, dup: Path, original: Path) -> None:
    m = _dupes_map(output)
    m[_rel(output, dup)] = _rel(output, original)
    atomic_write_text(output / DUPES_NAME, json.dumps(m, indent=1))


def list_duplicates(output: Path) -> list[dict]:
    """Tracks in the Duplicates folder and the sorted track each one matches."""
    folder = output / DUPLICATES_DIR
    m = _dupes_map(output)
    out = []
    if not folder.is_dir():
        return out
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or not is_audio(f):
            continue
        orig = m.get(_rel(output, f))
        op = _abs(output, orig) if orig else None
        out.append({"dup": f, "original": op if op and op.exists() else None,
                    "known": bool(orig)})
    return out


def delete_duplicates(output: Path, paths: list[Path], progress=None) -> tuple[int, int, list[str]]:
    """Delete the given duplicates (to the Recycle Bin / Trash when possible), but only those whose
    matching sorted track still exists. Returns (deleted, kept, problems)."""
    m = _dupes_map(output)
    trash = can_use_trash()
    done = kept = 0
    problems = []
    for i, p in enumerate(paths, 1):
        progress and progress(i, len(paths), p.name)
        orig = m.get(_rel(output, p))
        if not orig or not _abs(output, orig).exists():
            kept += 1
            problems.append(f"{p.name}: its matching sorted track is gone, so it was kept")
            continue
        try:
            remove_file(p, trash)
            m.pop(_rel(output, p), None)
            done += 1
        except Exception as e:
            problems.append(f"{p.name}: {e}")
    atomic_write_text(output / DUPES_NAME, json.dumps(m, indent=1))
    return done, kept, problems


def track_info(path: Path) -> dict:
    info = {"name": path.name, "folder": str(path.parent), "format": path.suffix.upper().lstrip(".")}
    try:
        info["size"] = path.stat().st_size
    except OSError:
        info["size"] = 0
    meta = read_tags(path)
    info.update(meta)
    return info


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #
@dataclass
class SortOptions:
    output: Path
    move: bool = False              # False = copy (originals untouched)
    layout: str = "genre"
    rename: str = "album"           # how a different track with a taken name is renamed
    naming: str = "keep"            # file names: keep, "01 - Title", "Artist - Title"
    unknown_genre: str = "other"    # genres not in genres.json: own folder under Other Genres, or Unsorted
    skip_duplicates: bool = True
    duplicates_folder: bool = True
    same_sound: bool = True         # also catch the same sound with different tags (MP3, FLAC, WAV)
    fill_from_name: bool = True     # missing artist/title read from the file name
    write_tags: bool = True         # ...and written into the sorted track's tags
    covers: bool = True             # copy cover.jpg / folder.jpg into the album folder
    verify: bool = True
    preview: bool = False


@dataclass
class SortResult:
    source: Path
    dest: Path | None = None
    meta: dict = field(default_factory=dict)
    error: str = ""
    duplicate_of: Path | None = None
    duplicate_dest: Path | None = None
    duplicate_kind: str = ""        # "exact" or "sound"
    preview: bool = False
    renamed: bool = False
    filled: list[str] = field(default_factory=list)
    tagged: bool = False

    def outcome(self) -> str:
        if self.error:
            return "Skipped"
        if self.duplicate_of:
            what = "Exact copy" if self.duplicate_kind == "exact" else "Same sound, different tags"
            return f"{what} of {self.duplicate_of.name}"
        verb = "Would go to" if self.preview else "Sorted"
        extra = []
        if self.renamed and self.dest:
            extra.append(f"renamed {self.dest.name}")
        if self.filled:
            extra.append("filled in " + ", ".join(self.filled))
        return verb + (f" ({'; '.join(extra)})" if extra else "")


class Sorter:
    def __init__(self, status=print):
        self.status = status
        self.genres = load_genres()
        self.index: CopyIndex | None = None
        self.lib: Library | None = None
        self.journal: RunJournal | None = None
        self.log: RunLog | None = None
        self.output: Path | None = None
        self.preview = False
        self._reserved: set[str] = set()
        self._album_genre: dict[str, tuple[str, str]] = {}

    def reload_genres(self):
        self.genres = load_genres()

    # ---- run bookkeeping ---- #
    def begin_run(self, output: Path, preview: bool):
        self.output, self.preview = output, preview
        if not preview:
            output.mkdir(parents=True, exist_ok=True)
        self.index = CopyIndex(output)
        self.lib = Library(output)
        self._reserved = set()
        self._album_genre = {}
        self._dirty, self._saved_at = 0, time.monotonic()
        self.journal = None if preview else RunJournal(output)
        if self.journal:
            self.journal.start()
        self.log = None
        if not preview:
            try:
                self.log = RunLog(output)
            except OSError as e:
                log_error("Starting the sort log", e)

    def end_run(self):
        if not self.preview:
            self.index.save()
            self.lib.save()
        if self.log:
            self.log.close()
            self.log = None

    # ---- one track ---- #
    def _describe(self, src: Path, opt: SortOptions, meta: dict) -> dict:
        """Work out genre, artist folder and album for a track (fills meta's _genre, _sub, _artist)."""
        if opt.fill_from_name and (not meta.get("title") or not (meta.get("artist") or meta.get("albumartist"))):
            g = guess_from_name(src)
            for k_meta, k_guess in (("artist", "artist"), ("title", "title"), ("tracknumber", "track")):
                if not meta.get(k_meta) and g.get(k_guess):
                    meta[k_meta] = g[k_guess]
                    meta.setdefault("_filled", []).append(k_meta)
        if is_compilation(meta):
            meta["_artist"] = COMPILATIONS
        else:
            base = primary_artist(meta.get("albumartist", "")) or primary_artist(meta.get("artist", ""))
            meta["_artist"] = self.lib.canonical(base) if base else UNKNOWN_ARTIST
        album_k = "|".join((name_key(meta["_artist"]), name_key(meta.get("album", ""))))
        hit = match_genre(meta.get("genre", ""), self.genres)
        if hit is None and meta.get("genre") and opt.unknown_genre == "other":
            first = display_name(re.split(r"\s*[;/,|]\s*", meta["genre"])[0])
            hit = (OTHER_GENRES, first.title() if first.islower() else first)
        if meta.get("album") and meta["_artist"] != UNKNOWN_ARTIST:
            # an album keeps the genre its first track got, so it's never split up
            if album_k in self._album_genre:
                hit = self._album_genre[album_k]
            elif hit:
                self._album_genre[album_k] = hit
        meta["_genre"], meta["_sub"] = hit if hit else (UNSORTED, "")
        return meta

    def _folder(self, opt: SortOptions, meta: dict) -> Path:
        key = Library.album_key(opt.layout, meta)
        if meta.get("album") and meta["_artist"] != UNKNOWN_ARTIST and opt.layout != "genre_only":
            rel = self.lib.albums.get(key)
            if rel:
                return _abs(self.output, rel)
            folder = folder_for(self.output, meta, opt.layout)
            self.lib.albums[key] = _rel(self.output, folder)
            return folder
        return folder_for(self.output, meta, opt.layout)

    def sort_one(self, src: Path, opt: SortOptions) -> SortResult:
        r = SortResult(source=src, preview=opt.preview)
        try:
            if not src.exists():
                r.error = "the file is no longer there"
                return r
            digest = file_hash(src)
            sound = audio_hash(src) if opt.same_sound else None
            sound_key = f"a:{src.suffix.lower()}:{sound}" if sound else None
            meta = read_tags(src)
            r.meta = meta
            if meta.get("error"):
                r.error = f"{src.name} {meta['error']}; it was left where it is"
                return r
            if opt.skip_duplicates:
                orig = self.index.find(digest)
                kind = "exact"
                if orig is None and sound_key:
                    orig, kind = self.index.find(sound_key), "sound"
                if orig is not None and not _same_path(orig, src):
                    r.duplicate_of, r.duplicate_kind = orig, kind
                    if opt.duplicates_folder:
                        dest = unique_dest(self.output / DUPLICATES_DIR, src.name, self._reserved)
                        r.duplicate_dest = dest
                        if opt.preview:
                            self._reserved.add(os.path.normcase(str(dest)))
                        else:
                            place_file(src, dest, opt.move, digest, opt.verify)
                            record_duplicate(self.output, dest, orig)
                            self.journal.add(src, dest, opt.move, "duplicate")
                    return r
            self._describe(src, opt, meta)
            folder = self._folder(opt, meta)
            name = target_name(src, meta, opt.naming)
            dest = clash_dest(src, folder, name, meta, opt.rename, self._reserved)
            r.renamed = dest.name != src.name
            r.filled = meta.get("_filled", [])
            r.dest = dest
            if opt.preview:
                self._reserved.add(os.path.normcase(str(dest)))
                self.index.add(digest, dest)  # later copies in this preview show as copies
                if sound_key:
                    self.index.add(sound_key, dest)
                return r
            need = src.stat().st_size
            if (not opt.move or not _same_drive(src, _existing(folder))) \
                    and free_space(folder) < need + (64 << 20):
                raise OSError(28, "No space left on the destination drive")
            place_file(src, dest, opt.move, digest, opt.verify)
            self.journal.add(src, dest, opt.move)
            if opt.write_tags and r.filled:
                r.tagged = write_tags(dest, {k: meta[k] for k in r.filled if k in ("artist", "title",
                                                                                      "tracknumber")})
            if opt.covers and meta.get("album") and opt.layout not in ("genre_only", "artist_only"):
                self._bring_cover(src.parent, dest.parent)
            self.index.add(digest, dest)
            if sound_key:
                self.index.add(sound_key, dest)
            self.lib.add(dest, meta)
            return r
        except Exception as e:
            log_error(f"Sorting {src}", e)
            r.error = explain_error(e)
            r.dest = None
            return r
        finally:
            if self.log and not opt.preview:
                try:
                    self.log.row(r)
                except Exception:
                    pass
            if not opt.preview and self.index and (r.dest or r.duplicate_dest):
                self._dirty += 1
                if self._dirty >= 25 or time.monotonic() - self._saved_at > 5:
                    self.index.save()
                    self.lib.save()
                    self._dirty, self._saved_at = 0, time.monotonic()

    def _bring_cover(self, src_dir: Path, dest_dir: Path):
        if find_cover(dest_dir):
            return
        cover = find_cover(src_dir)
        if cover is None:
            return
        target = dest_dir / cover.name.lower()
        try:
            transfer(cover, target, move=False)  # always a copy: the original stays with its album
            self.journal.add(cover, target, False, "cover")
        except Exception as e:
            log_error(f"Copying {cover}", e)


def _existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def space_needed(files: list[Path], output: Path, move: bool) -> tuple[int, int]:
    """(bytes the sort needs on the output drive, bytes free there)."""
    out_probe = output
    while not out_probe.exists() and out_probe != out_probe.parent:
        out_probe = out_probe.parent
    need = 0
    for f in files:
        try:
            if move and _same_drive(f, out_probe):
                continue
            need += f.stat().st_size
        except OSError:
            pass
    return need, free_space(output)


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #
def undo_last(output: Path, progress=None) -> tuple[int, list[str]]:
    """Reverse the most recent sort into output: moved tracks go back where they came from, copies
    are removed (to the Recycle Bin / Trash when possible). Returns (undone, problems)."""
    j = RunJournal(output)
    entries = j.load()
    idx, lib = CopyIndex(output), Library(output)
    dmap = _dupes_map(output)
    done, problems = 0, []
    trash = can_use_trash()
    for i, e in enumerate(reversed(entries), 1):
        src, dest = Path(e["src"]), Path(e["dest"])
        progress and progress(i, len(entries), dest.name)
        if not dest.exists():
            continue
        try:
            if e.get("move"):
                back = src if not src.exists() else unique_dest(src.parent, src.name)
                cars = sidecars_of(dest)
                transfer(dest, back, True)
                for car in cars:
                    t = back.with_suffix(car.suffix)
                    if not t.exists():
                        transfer(car, t, True)
            else:
                remove_file(dest, trash, sidecars=e.get("kind") != "cover")
            rel = _rel(output, dest)
            idx.drop(rel)
            lib.drop(rel)
            dmap.pop(rel, None)
            remove_empty_dirs(dest.parent, output)
            done += 1
        except Exception as ex:
            log_error(f"Undoing {dest}", ex)
            problems.append(f"{dest.name}: {ex}")
    idx.save()
    lib.save()
    atomic_write_text(output / DUPES_NAME, json.dumps(dmap, indent=1))
    if not problems:
        j.start()  # nothing left to undo
    return done, problems


# --------------------------------------------------------------------------- #
# Artists: rename, merge, move tracks to another artist
# --------------------------------------------------------------------------- #
def artists(output: Path) -> list[dict]:
    """[{"name", "key", "tracks": [rel…], "albums": {…}}] for every artist folder in the library."""
    lib = Library(output)
    groups: dict[str, dict] = {}
    for rel, t in lib.existing().items():
        a = t.get("_artist") or UNKNOWN_ARTIST
        g = groups.setdefault(name_key(a), {"name": a, "key": name_key(a), "tracks": [], "albums": set()})
        g["tracks"].append(rel)
        if t.get("album"):
            g["albums"].add(t["album"])
    return sorted(groups.values(), key=lambda g: g["name"].casefold())


def retarget_tracks(output: Path, rels: list[str], new_artist: str, progress=None) -> tuple[int, list[str]]:
    """Give tracks a different artist: their Artist / Album Artist tags change (where the format
    allows) and they move into that artist's folder. Merging two artists is the same thing.
    Nothing is deleted or overwritten: a different track with a taken name is numbered, an exact
    copy goes to Duplicates."""
    lib, idx = Library(output), CopyIndex(output)
    new_artist = lib.canonical(display_name(new_artist))
    dmap = _dupes_map(output)
    done, problems = 0, []
    folders_left: set[Path] = set()
    moves: dict[str, str] = {}  # old path → new path, so Undo last sort still finds them
    for i, rel in enumerate(rels, 1):
        t = lib.tracks.get(rel)
        src = _abs(output, rel)
        progress and progress(i, len(rels), src.name)
        if not t or not src.exists():
            continue
        old_artist = t.get("_artist", UNKNOWN_ARTIST)
        try:
            changes = {}
            if old_artist == COMPILATIONS:  # leaving a compilation for one artist's folder
                changes.update(albumartist=new_artist, compilation="")
            if t.get("albumartist") and name_key(primary_artist(t["albumartist"])) == name_key(old_artist):
                changes["albumartist"] = new_artist
            if not t.get("artist") or name_key(primary_artist(t["artist"])) == name_key(old_artist) \
                    or old_artist == UNKNOWN_ARTIST:
                rest = _FEAT.search(t.get("artist", "") or "")
                changes["artist"] = new_artist + (rest.group(0) if rest else "")
            if src.suffix.lower() in TAG_WRITABLE and changes:
                if not write_tags(src, changes):
                    problems.append(f"{src.name}: its tags couldn't be changed, but it was still moved")
            # the new folder: the artist part of its path swapped for the new artist
            parts = list(Path(rel).parts)
            old_seg = safe_folder(old_artist).casefold()
            seg_i = next((k for k in range(len(parts) - 1) if parts[k].casefold() == old_seg), None)
            if seg_i is None:  # couldn't find the artist folder (genre-only layout): stays put
                new_rel_dir = Path(*parts[:-1]) if len(parts) > 1 else Path()
            else:
                parts[seg_i] = safe_folder(new_artist)
                if seg_i > 0 and parts[seg_i - 1] == letter_of(old_artist) and len(parts[seg_i - 1]) == 1:
                    parts[seg_i - 1] = letter_of(new_artist)  # A–Z layout: the letter folder follows
                new_rel_dir = Path(*parts[:-1])
            dest_dir = output / new_rel_dir
            dest = dest_dir / src.name
            if _same_path(dest_dir, src.parent):
                pass
            elif dest.exists():
                if file_hash(dest) == file_hash(src):
                    dup = unique_dest(output / DUPLICATES_DIR, src.name)
                    place_file(src, dup, True)
                    moves[str(src)] = str(dup)
                    dmap[_rel(output, dup)] = _rel(output, dest)
                    lib.drop(rel)
                    idx.moved(rel, _rel(output, dest))
                    folders_left.add(src.parent)
                    done += 1
                    continue
                dest = unique_dest(dest_dir, src.name)
            if not _same_path(dest_dir, src.parent):
                place_file(src, dest, True)
                moves[str(src)] = str(dest)
                folders_left.add(src.parent)
            new_rel = _rel(output, dest)
            t.update({k: v for k, v in changes.items()})
            t["_artist"] = new_artist
            lib.tracks.pop(rel, None)
            lib.tracks[new_rel] = t
            idx.moved(rel, new_rel)
            for k, v in list(dmap.items()):
                if v == rel:
                    dmap[k] = new_rel
            done += 1
        except Exception as e:
            log_error(f"Moving {src} to {new_artist}", e)
            problems.append(f"{src.name}: {explain_error(e)}")
    # album folders that now hold only cover art follow their tracks; empty folders are tidied
    for d in folders_left:
        try:
            if d.exists() and not any(is_audio(f) for f in d.iterdir()):
                moved_to = _new_home(output, lib, d)
                if moved_to:
                    for f in list(d.iterdir()):
                        if f.is_file() and f.suffix.lower() in COVER_EXTS and not (moved_to / f.name).exists():
                            transfer(f, moved_to / f.name, True)
                            moves[str(f)] = str(moved_to / f.name)
            remove_empty_dirs(d, output)
        except Exception as e:
            log_error(f"Tidying {d}", e)
    if moves:
        j = RunJournal(output)
        entries = j.load()
        if any(e["dest"] in moves for e in entries):
            for e in entries:
                e["dest"] = moves.get(e["dest"], e["dest"])
            j.entries = entries
            j.save()
    for k in list(lib.albums):  # album folders are recomputed from the tracks next time
        if not (output / lib.albums[k]).exists():
            del lib.albums[k]
    lib.save()
    idx.save()
    atomic_write_text(output / DUPES_NAME, json.dumps(dmap, indent=1))
    return done, problems


def _new_home(output: Path, lib: Library, old_dir: Path) -> Path | None:
    """Where the tracks that left old_dir went (if they all went to one folder)."""
    name = old_dir.name.casefold()
    homes = {(_abs(output, r)).parent for r in lib.tracks if _abs(output, r).parent.name.casefold() == name}
    homes.discard(old_dir)
    return homes.pop() if len(homes) == 1 else None


def search(output: Path, text: str, limit: int = 500) -> list[tuple[Path, dict]]:
    """Sorted tracks whose title, artist, album, genre or year contain every word typed."""
    words = [w.casefold() for w in text.split() if w.strip()]
    out = []
    for rel, t in Library(output).existing().items():
        hay = " ".join(str(t.get(k, "")) for k in ("title", "artist", "albumartist", "album", "genre",
                                                    "_genre", "_sub", "date")).casefold() + " " + rel.casefold()
        if all(w in hay for w in words):
            out.append((_abs(output, rel), t))
            if len(out) >= limit:
                break
    out.sort(key=lambda x: (x[1].get("_artist", "").casefold(), x[1].get("album", "").casefold(),
                            _track_no(x[1]) or "999", x[1].get("title", "").casefold()))
    return out


def gather_copies(output: Path, progress=None, should_stop=None) -> tuple[int, list[str]]:
    """Find copies already among the sorted tracks (exact, or same sound with different tags) and
    MOVE the extra ones into Duplicates for review. One of each stays put. Undo puts them back."""
    lib, idx = Library(output), CopyIndex(output)
    files = [f for f in output.rglob("*") if f.is_file() and is_audio(f)
             and DUPLICATES_DIR not in f.relative_to(output).parts[:1]
             and not any(p.startswith(".") for p in f.relative_to(output).parts)]
    by_key: dict[str, Path] = {}
    j = RunJournal(output)
    j.start()
    moved, problems = 0, []
    for i, f in enumerate(sorted(files), 1):
        if should_stop and should_stop():
            break
        progress and progress(i, len(files), f.name)
        try:
            keys = [file_hash(f)]
            s = audio_hash(f)
            if s:
                keys.append(f"a:{f.suffix.lower()}:{s}")
            keeper = next((by_key[k] for k in keys if k in by_key), None)
            if keeper is None:
                for k in keys:
                    by_key[k] = f
                continue
            dest = unique_dest(output / DUPLICATES_DIR, f.name)
            place_file(f, dest, True)
            j.add(f, dest, True, "gathered")
            record_duplicate(output, dest, keeper)
            rel = _rel(output, f)
            lib.drop(rel)
            idx.drop(rel)
            remove_empty_dirs(f.parent, output)
            moved += 1
        except Exception as e:
            log_error(f"Checking {f}", e)
            problems.append(f"{f.name}: {e}")
    lib.save()
    idx.save()
    return moved, problems


# --------------------------------------------------------------------------- #
# Errors: a log file with full details, and plain-language explanations
# --------------------------------------------------------------------------- #
def log_error(context: str, exc: BaseException) -> str:
    import platform
    import traceback
    versions = []
    for mod in ("mutagen", "PySide6"):
        try:
            versions.append(f"{mod} {__import__(mod).__version__}")
        except Exception:
            versions.append(f"{mod} not installed")
    detail = (f"--- {datetime.now():%Y-%m-%d %H:%M:%S}  {context}\n"
              f"Python {platform.python_version()} on {platform.platform()}\n"
              f"{', '.join(versions)}\n"
              + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(detail + "\n")
    except Exception:
        pass
    return detail


def problem_report(note: str = "", log_lines: int = 160) -> str:
    import platform
    ver = (APP_DIR / "VERSION").read_text("utf-8").strip() if (APP_DIR / "VERSION").exists() else "?"
    mods = []
    for mod in ("PySide6", "mutagen", "send2trash"):
        try:
            mods.append(f"{mod} {getattr(__import__(mod), '__version__', '?')}")
        except Exception:
            mods.append(f"{mod} -")
    st = load_settings()
    keep = {k: st.get(k) for k in ("layout", "move", "rename", "naming", "unknown_genre", "verify",
                                   "skip_duplicates", "same_sound", "fill_from_name", "appearance")}
    try:
        log = LOG_FILE.read_text("utf-8", errors="replace").splitlines()[-log_lines:]
    except OSError:
        log = ["(no error log)"]
    text = (f"Solfège {ver}\nPython {platform.python_version()} on {platform.platform()}\n"
            f"{', '.join(mods)}\nSettings: {json.dumps(keep)}\nApp folder: {APP_DIR}\n\n"
            + (f"What happened:\n{note.strip()}\n\n" if note.strip() else "")
            + "Recent errors:\n" + "\n".join(log))
    home = str(Path.home())
    if home and len(home) > 3:
        text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    return text


def explain_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if isinstance(exc, CopyCheckFailed):
        return str(exc)
    if isinstance(exc, ModuleNotFoundError) or "no module named" in low:
        return ("Part of the program didn't install. Delete the '.venv' folder inside the Solfege folder "
                "and open the launcher again to reinstall.")
    if "no space left" in low or "errno 28" in low:
        return "the destination drive is full, so this track was left where it is"
    if isinstance(exc, PermissionError):
        return "another program is using this file (a music player?) or it's read-only; it was left where it is"
    if isinstance(exc, MemoryError):
        return "Your computer ran out of memory. Close other programs and try again."
    return text
