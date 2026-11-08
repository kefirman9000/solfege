# Solfège

*solfège (n.): do, re, mi… the way music is sorted into notes you can name. This one sorts your files.*

Drag music files or folders onto the window and Solfège files them into **genre**, **sub-genre**,
**artist** and **album** folders, read from the tags already inside each track. It's the music
sibling of [Sortilège](https://github.com/kefirman9000/sortilege) and works the same way: the same
layout, the same safety rules, nothing ever overwritten or deleted while sorting.

Everything runs on your own computer. Nothing is uploaded anywhere, and no internet connection is
needed except to check for updates (only when you say yes).

## What you get

```
Sorted Music/
├── Rock/
│   ├── Classic Rock/
│   │   └── Queen/
│   │       └── News of the World/
│   ├── Alternative & Indie/
│   └── Nirvana/ …          ← a track tagged just "Rock" stays in the genre folder itself
├── Hip-Hop & Rap/
├── Other Genres/
│   └── Mongolian Folk/     ← a genre tag that isn't in the list (or send these to Unsorted)
├── Compilations/           ← albums by Various Artists, kept together
├── Unsorted/               ← tracks with no genre tag (still in Artist › Album folders)
└── Duplicates/             ← copies, waiting for you to review
```

- **An album always stays together.** Its first track decides the folder, so an album whose tracks
  carry different genre tags is never split up.
- **Album Artist comes first.** "Artist feat. Guest" goes in Artist's folder, and an album with
  guest spots isn't scattered across several artist folders.
- **One artist, one folder.** "the beatles", "The Beatles" and "Beatles, The" all go in the same
  folder (capitals, extra spaces and a trailing full stop don't matter).
- A track with no album stays in the artist folder itself. A track with no artist goes to
  **Unknown Artist** (unless the file name says who it is, see below).

### Folder layouts

The **Folders** menu chooses how sorted music is arranged:

| Choice | Example |
|---|---|
| Genre › Sub-genre › Artist › Album | `Rock/Classic Rock/Queen/News of the World/` |
| Genre › Artist › Album | `Rock/Queen/News of the World/` |
| Artist › Album (no genres) | `Queen/News of the World/` |
| Year › Artist › Album | `1977/Queen/News of the World/` |
| Decade › Genre › Artist › Album | `1970s/Rock/Queen/News of the World/` |
| Genre › Sub-genre (tracks loose inside) | `Rock/Classic Rock/` |

### File names

- **Keep the original file names** (default)
- **Track number - Title**: `01 - We Will Rock You.mp3` (multi-disc albums: `2-01 - …`)
- **Artist - Title**: `Queen - We Will Rock You.mp3`

### Tracks with the same name

Different tracks often share a name (`Intro.mp3`, `Track 01.mp3`). Nothing is ever overwritten:
choose under **Same name, different track** how the newcomer is renamed:

- **Add the album** (default): `Intro (Live in Paris).mp3`
- **Add the artist**: `Intro (Queen).mp3`
- **Add the folder it came from**: `Intro_OldFolder.mp3`
- **Add a number**: `Intro_1.mp3`

A number is added on top if even the new name is taken. Copies of the same track aren't renamed;
they're caught by **Skip copies** instead.

### Missing tags

With **Fill in missing artist/title from the file name** on (the default), a track with empty
tags named `Daft Punk - One More Time.mp3`, `03 - Daft Punk - One More Time.mp3` or
`03 - One More Time.mp3` gets its artist, title and track number from the name. They're written
into the sorted track's tags (MP3, FLAC, M4A, OGG and Opus). In **Copy** mode only the copy is
changed; your original is untouched.

Lyrics (`.lrc`) and cue sheets (`.cue`) with the same name travel with their track. Album art
(`cover.jpg`, `folder.jpg`…) next to an album is copied into its new album folder; art embedded
inside tracks travels with them anyway.

### Formats

MP3, FLAC, M4A/AAC/ALAC, OGG Vorbis, Opus, WAV, AIFF, WMA, APE, WavPack and Musepack are sorted.
Tags are read with [mutagen](https://mutagen.readthedocs.io), installed automatically.

## Setting it up

1. Install **Python 3.10 or newer** from https://www.python.org/downloads/
   (on Windows, tick **"Add python.exe to PATH"** in the installer).
2. Download the latest `solfege-x.y.z.zip` from the [Releases page](https://github.com/kefirman9000/solfege/releases)
   and unzip it somewhere permanent, such as your Documents folder.
3. Double-click the launcher:
   - Windows: **Start Solfege (Windows).bat**
   - Mac: **Start Solfege (Mac).command** (the first time, right-click it and choose **Open**)

The first launch installs the parts it needs (a minute or two). Later launches open straight away.

## Using it

1. Drop music files or folders onto the big box (or use **Add music…** / **Add folder…**).
2. Choose where the sorted music goes.
3. Pick **Copy** (originals stay put; safest) or **Move**.
4. Press **Sort music**. Double-click any row in the results to open its folder; right-click to play it.

Dropping music never starts sorting by itself: it waits on the list until you press **Sort music**.
Tick **Auto-start sorting when music is dropped** to change that (Copy mode only; Move always asks).

**Pause** stops after the current track; **Resume** carries on. **Stop** finishes the current track
and puts the rest back on the list. The progress bar shows "120 of 340 tracks" with the time left,
and the percentage also shows in the window title, so you can see it from the taskbar.

### Safety features

Solfège handles what may be your only copies of your music, so it's built to never lose any:

- **Nothing is ever overwritten or deleted while sorting.** Name clashes get a new name; copies go
  to Duplicates; deleting duplicates only happens in the Duplicates tab when you confirm, and goes
  to the Recycle Bin / Trash.
- **Every copy is checked.** With **Check every copy against the original** on (the default), each
  copied track is read back and compared byte for byte with the original. A **Move** to another drive
  copies, checks, and only then removes the original; a move on the same drive is just a rename.
- **Tags are written safely.** The change is made to a temporary copy, checked that it still plays
  the same length, and only then swapped in.
- **Preview only** shows exactly where every track would go, including renames and copies, without
  copying, moving or changing anything. Your tracks stay on the list so you can then sort for real.
- **Disk space** is checked before a sort starts.
- **Undo last sort** reverses the most recent run (it still works after you rename artists).
- **Sort logs:** every run writes a spreadsheet (CSV, opens in Excel) of what happened to each track.
  Open them from **Help › Open sort logs**. They live in a hidden `.solfege-logs` folder in your
  sorted folder.
- Unreadable or damaged files are skipped and left exactly where they were, even in Move mode.
- **Only one copy of the app runs at a time.**

### Copies of the same track

With **Skip copies of the same track** on (the default), a track is sorted only once even if you have
several copies of it. This works across runs too, so dropping a folder you've already sorted only adds
what's new.

- **Exact copies:** byte-for-byte identical files.
- **Same sound, different tags** (MP3, FLAC and WAV; can be turned off): the sound itself is
  identical, but the tags differ, like a copy where someone fixed the title or added cover art. Only
  files of the same type are compared.

A different **version** is never a copy: another bitrate, a re-encode, a remaster, a FLAC and an MP3
of the same song, a trimmed or edited file. Those are all sorted normally, so both are kept.

With **Put copies in a Duplicates folder** on (the default), copies go into one `Duplicates` folder
next to your sorted music for review. Untick it to leave copies where they are.

### Reviewing duplicates

Open the **Duplicates** tab. Click a duplicate and it appears **side by side** with the sorted track it
matches: title, artist, album, length, bitrate, sample rate, size, format and folder, with anything that
differs in orange. **Play** either one to listen.

- **Delete this duplicate…** removes the one you're looking at; **Previous / Next** step through.
- Tick several (or **Tick all**) and press **Delete ticked…**.
- A duplicate is only deleted if the sorted track it matches still exists.
- **Find copies in sorted folders…** checks everything already sorted, keeps one of each and **moves**
  the extras into Duplicates. Nothing is deleted, and Undo last sort puts them back.

### Artists

The **Artists** tab lists every artist in your sorted music with their track and album counts.

- **Rename:** double-click an artist. Their Artist / Album Artist tags change and their tracks move
  into the new folder together. Renaming to a name that already exists merges them.
- **Merge:** if one artist shows up under two spellings ("Beyonce" and "Beyoncé"), select both
  (Ctrl-click) and press **Merge selected**, or right-click › **Merge into**.
- **Fix tracks:** click an artist to see their tracks. Select ones that belong to someone else and
  pick who under **Move selected to** (anyone you already have, or **New artist…**). This is how you
  sort out **Unknown Artist** too.
- **Show artists with at least N tracks** hides one-off artists from the list (only from the list).

Nothing is deleted by any of this. If a moved track is byte-for-byte identical to one already in the
new folder, the extra goes to Duplicates.

### Changing the genres

Press **Edit genres…** to open `genres.json`. Each genre holds sub-genres, and each sub-genre lists the
genre tags that belong in it:

```json
"Rock": {
  "_names": ["rock", "rock and roll"],
  "Classic Rock": ["classic rock", "southern rock", "blues rock"],
  "Punk": ["punk", "pop punk", "post punk"]
}
```

`_names` are tags that go in the genre folder itself. Capitals, `&` vs `and` and dashes don't matter,
and the most specific match wins ("progressive rock" beats "rock"). A tag with several genres
("Rock; Pop") uses the first that fits. Add, rename or remove anything, save, then press
**Reload genres**. If the file has a typo, the app tells you which line.

**Genres not in the list** decides where other genre tags go: their own folder under **Other Genres**
(default), or **Unsorted**.

### Searching

The **Search** tab finds sorted tracks by title, artist, album, genre or year. Every word must match:
"queen 1977", "miles davis blue", "christmas". Double-click to play.

### Undoing a sort

**Undo last sort…** (next to the output folder) reverses the most recent run into that folder: moved
tracks go back where they came from, and copies are removed (to the Recycle Bin / Trash). Tags filled in
on moved tracks stay.

## Updates

Solfège checks GitHub for a newer **release** when it opens, and **only downloads it if you say yes**.
Only published releases are offered, never work in progress. Each download is checked against its
SHA-256 checksum before anything is installed, and the version you had is backed up first:
**Help › Roll back to an earlier version…** puts it back (the last 3 are kept). Use
**Help › Check for updates…** any time, and turn the automatic check off in the same menu.
Updating keeps your `data` folder and the private Python environment. If you've edited `genres.json`,
yours is kept and the new one is saved next to it as `genres.json.new`.

## Look

**View › Match Windows / Light / Dark** switches the look; by default it follows Windows.

## Support

If Solfège saves you time, you can support its development through
[GitHub Sponsors](https://github.com/sponsors/kefirman9000). Thank you!

## When something goes wrong

If something unexpected happens, Solfège says so in plain words and keeps running; your music is safe.
Choose **Report it…** there, or **Help › Report a problem…** any time. You'll see the report first
(version, system details and recent errors; your Windows user name is hidden) and can copy it, save it,
or open a pre-filled GitHub issue. Nothing is sent unless you choose to.

## For the maintainer: publishing an update

Bump the number in `VERSION` and push to `main`. A GitHub Action builds `solfege-<version>.zip`, writes
its checksum, and publishes the release; apps then offer it. Pushes that don't change `VERSION` are
never offered to users.
