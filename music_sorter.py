"""
Solfège — drag music files or folders in, get them sorted into Genre › Artist › Album folders
from the tags inside them. Runs entirely on this computer.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog, QPushButton, QRadioButton, QSpinBox,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import engine
import updater

APP_NAME = "Solfège"
SPONSOR_URL = "https://github.com/sponsors/kefirman9000"
ORANGE = "#d9822b"

STYLE = """
QWidget { font-size: 13px; }
#DropZone { border: 2px dashed #8a9bb0; border-radius: 12px; background: palette(base); }
#DropZone[hover="true"] { border-color: #2f7d6d; background: rgba(47,125,109,0.08); }
#DropTitle { font-size: 18px; font-weight: 600; }
#Hint { color: palette(mid); }
#Card { border: 1px solid palette(mid); border-radius: 8px; }
QPushButton#Primary { background: #2f7d6d; color: white; border: none; border-radius: 6px;
                      padding: 8px 18px; font-weight: 600; }
QPushButton#Primary:disabled { background: #9db8b1; }
QPushButton#Primary:hover:!disabled { background: #276a5c; }
"""


def apply_appearance(app: QApplication, key: str = "system"):
    """Modern Fusion look, light or dark, following Windows unless chosen."""
    app.setStyle("Fusion")
    dark = key == "dark"
    if key == "system":
        try:
            dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except Exception:
            dark = False
    if dark:
        pal = QPalette()
        c = {QPalette.Window: "#202124", QPalette.WindowText: "#e8eaed", QPalette.Base: "#17181a",
             QPalette.AlternateBase: "#26282b", QPalette.Text: "#e8eaed", QPalette.Button: "#2b2d31",
             QPalette.ButtonText: "#e8eaed", QPalette.ToolTipBase: "#2b2d31", QPalette.ToolTipText: "#e8eaed",
             QPalette.Highlight: "#2f7d6d", QPalette.HighlightedText: "#ffffff", QPalette.Link: "#6cc4b0",
             QPalette.PlaceholderText: "#9aa0a6", QPalette.Mid: "#9aa0a6", QPalette.BrightText: "#ffffff"}
        for role, col in c.items():
            pal.setColor(role, QColor(col))
        for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            pal.setColor(QPalette.Disabled, role, QColor("#6b6f75"))
    else:
        pal = app.style().standardPalette()
        pal.setColor(QPalette.Highlight, QColor("#2f7d6d"))
    app.setPalette(pal)


def open_path(path: Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def show_in_folder(path: Path):
    """Open the folder with the file selected (Windows), or just the folder elsewhere."""
    import subprocess
    try:
        if os.name == "nt" and path.exists():
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
        if sys.platform == "darwin" and path.exists():
            subprocess.Popen(["open", "-R", str(path)])
            return
    except Exception:
        pass
    open_path(path.parent if path.suffix else path)


def item(text, data=None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    if data is not None:
        it.setData(Qt.UserRole, data)
    return it


def make_table(headers: list[str], widths: list[int]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    for i, w in enumerate(widths):
        t.setColumnWidth(i, w)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.verticalHeader().setVisible(False)
    t.setSortingEnabled(False)
    return t


# --------------------------------------------------------------------------- #
# Background workers
# --------------------------------------------------------------------------- #
class BusyWorker(QThread):
    """Runs a slow job (renaming, merging, undoing…) off the window's thread."""
    prog = Signal(int, int, str)

    def __init__(self, fn):
        super().__init__()
        self.fn, self.result, self.exc = fn, None, None
        self.stop_requested = False

    def run(self):
        try:
            self.result = self.fn(lambda i, n, t="": self.prog.emit(int(i), int(n), str(t)))
        except Exception as e:
            engine.log_error("Background job", e)
            self.exc = e


class ScanWorker(QThread):
    """Searches dropped folders for music without freezing the window."""
    found = Signal(int, str)

    def __init__(self, paths, skip_under):
        super().__init__()
        self.paths, self.skip_under = paths, skip_under
        self.files: list[Path] = []
        self._stop = False
        self._last = 0.0

    def stop(self):
        self._stop = True

    def _report(self, n, folder):
        now = time.monotonic()
        if now - self._last > 0.1 or not folder:
            self._last = now
            self.found.emit(n, folder)

    def run(self):
        self.files = engine.collect_tracks(self.paths, skip_under=self.skip_under,
                                           on_found=self._report, should_stop=lambda: self._stop)


class SortWorker(QThread):
    status = Signal(str)
    progress = Signal(int, int)
    result = Signal(object)

    def __init__(self, sorter: engine.Sorter, files: list[Path], opt: engine.SortOptions):
        super().__init__()
        self.sorter, self.files, self.opt = sorter, files, opt
        self._stop = False
        self._gate = threading.Event()
        self._gate.set()
        self.done_count = 0

    def stop(self):
        self._stop = True
        self._gate.set()

    def pause(self):
        self._gate.clear()

    def resume(self):
        self._gate.set()

    @property
    def paused(self) -> bool:
        return not self._gate.is_set()

    def run(self):
        total = len(self.files)
        self.sorter.begin_run(self.opt.output, preview=self.opt.preview)
        try:
            self.progress.emit(0, total)
            for i, f in enumerate(self.files, 1):
                if self.paused and not self._stop:
                    self.status.emit(f"Paused after {i - 1} of {total}. Press Resume to carry on.")
                    self._gate.wait()
                if self._stop:
                    self.status.emit("Stopped.")
                    return
                self.status.emit(f"Sorting {f.name}  ({i} of {total})")
                self.result.emit(self.sorter.sort_one(f, self.opt))
                self.done_count = i
                self.progress.emit(i, total)
        finally:
            self.sorter.end_run()


class UpdateCheckWorker(QThread):
    def __init__(self):
        super().__init__()
        self.newer, self.local, self.rel, self.error = False, "", {}, ""

    def run(self):
        try:
            self.newer, self.local, self.rel = updater.check()
        except Exception as e:
            self.error = str(e)


class UpdateApplyWorker(QThread):
    status = Signal(str)

    def __init__(self, rel: dict | None = None, rollback_to: str = ""):
        super().__init__()
        self.rel, self.rollback_to = rel, rollback_to
        self.info: dict = {}
        self.error = ""

    def run(self):
        try:
            if self.rollback_to:
                self.status.emit(f"Rolling back to {self.rollback_to}…")
                self.info = updater.rollback(self.rollback_to)
            else:
                self.info = updater.apply_update(self.rel, self.status.emit)
        except Exception as e:
            engine.log_error("Updating", e)
            self.error = str(e)


class DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(140)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        t = QLabel("Drop music files or folders here")
        t.setObjectName("DropTitle")
        t.setAlignment(Qt.AlignCenter)
        s = QLabel("Folders are searched inside, including sub-folders. MP3, FLAC, M4A, OGG, Opus, WAV, AIFF, "
                   "WMA and more")
        s.setObjectName("Hint")
        s.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        lay.addWidget(s)

    def _hover(self, on: bool):
        self.setProperty("hover", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self.isEnabled():
            e.acceptProposedAction()
            self._hover(True)

    def dragLeaveEvent(self, e):
        self._hover(False)

    def dropEvent(self, e):
        self._hover(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(paths)
            e.acceptProposedAction()


# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1040, 760)
        self.settings = engine.load_settings()
        try:
            self.sorter = engine.Sorter()
            self.genre_error = ""
        except ValueError as e:
            self.sorter = None
            self.genre_error = str(e)
        self.pending: list[Path] = []
        self.worker: SortWorker | None = None
        self.scanner: ScanWorker | None = None
        self.busy: BusyWorker | None = None
        self._scan_queue: list[str] = []
        self._started_at = 0.0
        self._run_counts: dict[str, int] = {}
        self._upd_check: UpdateCheckWorker | None = None
        self._upd_apply: UpdateApplyWorker | None = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_sort_tab(), "Sort music")
        self.tabs.addTab(self._build_artists_tab(), "Artists")
        self.tabs.addTab(self._build_dupes_tab(), "Duplicates")
        self.tabs.addTab(self._build_search_tab(), "Search")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)
        self.setAcceptDrops(True)
        self._build_menu()
        self._update_ready()
        if self.genre_error:
            QTimer.singleShot(300, lambda: QMessageBox.warning(self, "genres.json", self.genre_error +
                                                               "\n\nFix it with Edit genres…, then press "
                                                               "Reload genres."))
        if self.settings.get("check_updates", True):
            QTimer.singleShot(1500, lambda: self.check_updates(quiet=True))

    def _tab_changed(self, i: int):
        if i == 1:
            self.refresh_artists()
        elif i == 2:
            self.refresh_dupes()
        elif i == 3:
            self.search_edit.setFocus()

    # ---- menu ------------------------------------------------------------- #
    def _build_menu(self):
        v = self.menuBar().addMenu("&View")
        grp = QActionGroup(self)
        for key, label in (("system", "Match Windows (light or dark)"), ("light", "Light"), ("dark", "Dark")):
            a = QAction(label, self, checkable=True)
            a.setChecked(self.settings.get("appearance", "system") == key)
            a.triggered.connect(lambda _=False, k=key: self._set_appearance(k))
            grp.addAction(a)
            v.addAction(a)

        m = self.menuBar().addMenu("&Help")
        for label, fn in (("Report a problem…", lambda: self.report_problem()),
                          ("Open the error log", self._open_error_log)):
            a = QAction(label, self)
            a.triggered.connect(fn)
            m.addAction(a)
        m.addSeparator()
        a = QAction("Check for updates…", self)
        a.triggered.connect(lambda: self.check_updates(quiet=False))
        m.addAction(a)
        a = QAction("Roll back to an earlier version…", self)
        a.triggered.connect(self.roll_back)
        m.addAction(a)
        self.a_auto = QAction("Check for updates when the app opens", self, checkable=True)
        self.a_auto.setChecked(self.settings.get("check_updates", True))
        self.a_auto.toggled.connect(lambda on: self._set("check_updates", on))
        m.addAction(self.a_auto)
        a = QAction("Open sort logs (what happened to every track)…", self)
        a.triggered.connect(self._open_logs)
        m.addAction(a)
        m.addSeparator()
        a = QAction("Support Solfège (GitHub Sponsors)…", self)
        a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(SPONSOR_URL)))
        m.addAction(a)
        a = QAction("Project page on GitHub…", self)
        a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(updater.PAGE_URL)))
        m.addAction(a)
        a = QAction(f"About {APP_NAME}", self)
        a.triggered.connect(lambda: QMessageBox.about(
            self, APP_NAME, f"<b>{APP_NAME}</b> {updater.local_version()}<br>Sorts music into genre, artist "
            f"and album folders from its tags, entirely on this computer.<br><br>"
            f"<a href='{updater.PAGE_URL}'>{updater.PAGE_URL}</a><br>"
            f"Enjoying it? <a href='{SPONSOR_URL}'>Support development on GitHub Sponsors</a>."))
        m.addAction(a)

    def _set(self, key, value):
        self.settings[key] = value
        engine.save_settings(self.settings)

    def _set_appearance(self, key: str):
        self._set("appearance", key)
        apply_appearance(QApplication.instance(), key)

    def _open_error_log(self):
        if engine.LOG_FILE.exists():
            open_path(engine.LOG_FILE)
        else:
            QMessageBox.information(self, APP_NAME, "No errors have been logged.")

    def _open_logs(self):
        d = self.output() / engine.LOGS_NAME
        if d.is_dir():
            open_path(d)
        else:
            QMessageBox.information(self, "Sort logs", "No sort logs in this output folder yet.")

    # ---- Sort tab ---------------------------------------------------------- #
    def _build_sort_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(10)
        self.drop = DropZone()
        self.drop.dropped.connect(self.add_paths)
        lay.addWidget(self.drop)

        row = QHBoxLayout()
        b_files = QPushButton("Add music…")
        b_files.clicked.connect(self.pick_files)
        b_dir = QPushButton("Add folder…")
        b_dir.clicked.connect(self.pick_folder)
        self.b_clear = QPushButton("Clear list")
        self.b_clear.clicked.connect(self.clear_pending)
        self.ready_lbl = QLabel()
        for b in (b_files, b_dir, self.b_clear):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.ready_lbl)
        lay.addLayout(row)

        out = QHBoxLayout()
        out.addWidget(QLabel("Sorted music goes to:"))
        self.out_edit = QLineEdit(self.settings.get("output", str(Path.home() / "Music" / "Sorted Music")))
        self.out_edit.editingFinished.connect(lambda: self._set("output", self.out_edit.text().strip()))
        out.addWidget(self.out_edit, 1)
        b_out = QPushButton("Change…")
        b_out.clicked.connect(self.pick_output)
        b_open = QPushButton("Open")
        b_open.clicked.connect(lambda: self.output().is_dir() and open_path(self.output()))
        self.b_undo = QPushButton("Undo last sort…")
        self.b_undo.setToolTip("Put moved tracks back where they came from, or remove the copies made by the "
                               "last sort.")
        self.b_undo.clicked.connect(self.undo_last)
        for b in (b_out, b_open, self.b_undo):
            out.addWidget(b)
        lay.addLayout(out)

        g = QGridLayout()
        g.setHorizontalSpacing(14)
        self.r_copy = QRadioButton("Copy (keep originals where they are)")
        self.r_move = QRadioButton("Move")
        grp = QButtonGroup(self)
        grp.addButton(self.r_copy)
        grp.addButton(self.r_move)
        (self.r_move if self.settings.get("move") else self.r_copy).setChecked(True)
        cm = QHBoxLayout()
        cm.addWidget(self.r_copy)
        cm.addWidget(self.r_move)
        cm.addStretch()
        g.addLayout(cm, 0, 0, 1, 2)

        def combo(options: dict, key: str, default: str, tip: str) -> QComboBox:
            c = QComboBox()
            for k, label in options.items():
                c.addItem(label, k)
            c.setCurrentIndex(max(c.findData(self.settings.get(key, default)), 0))
            c.setToolTip(tip)
            return c

        self.layout_box = combo(engine.LAYOUTS, "layout", "genre",
                                "How sorted music is arranged. An album always stays together in one folder, "
                                "even if its tracks have different genre tags.\nTracks with no album stay in "
                                "the artist folder itself. Albums by Various Artists go in Compilations.")
        self.rename_box = combo(engine.RENAME_MODES, "rename", "album",
                                "When a folder already has a different track with this name, the new one is "
                                "renamed this way. Nothing is ever overwritten.")
        self.naming_box = combo(engine.NAMING, "naming", "keep",
                                "How sorted tracks are named. Tracks with no title tag keep their name.")
        self.unknown_box = combo(engine.UNKNOWN_OPTIONS, "unknown_genre", "other",
                                 "Genre tags that aren't in genres.json. Tracks with no genre tag at all go to "
                                 "Unsorted.")
        for r, (label, box) in enumerate((("Folders:", self.layout_box), ("Same name, different track:",
                                                                          self.rename_box),
                                          ("File names:", self.naming_box),
                                          ("Genres not in the list:", self.unknown_box)), start=1):
            g.addWidget(QLabel(label), r, 0)
            g.addWidget(box, r, 1)

        def check(text, key, default, tip):
            c = QCheckBox(text)
            c.setChecked(self.settings.get(key, default))
            c.setToolTip(tip)
            return c

        self.c_dupes = check("Skip copies of the same track", "skip_duplicates", True,
                             "Byte-for-byte identical files are sorted only once, including copies of tracks "
                             "sorted in earlier runs. A different version (other bitrate, re-encoded, remastered, "
                             "trimmed) is a different file and is always sorted.")
        self.c_sound = check("Also catch the same sound with different tags", "same_sound", True,
                             "MP3, FLAC and WAV: a file whose sound is identical but whose tags differ (a fixed "
                             "title, added cover art) counts as a copy too. Only files of the same type are "
                             "compared.")
        self.c_dupfolder = check("Put copies in a Duplicates folder", "duplicates_folder", True,
                                 "Copies are gathered in one Duplicates folder for you to review (copied or moved "
                                 "like the rest). Nothing is ever deleted. Untick to leave them where they are.")
        self.c_dupes.toggled.connect(self.c_sound.setEnabled)
        self.c_dupes.toggled.connect(self.c_dupfolder.setEnabled)
        self.c_sound.setEnabled(self.c_dupes.isChecked())
        self.c_dupfolder.setEnabled(self.c_dupes.isChecked())
        self.c_fill = check("Fill in missing artist/title from the file name", "fill_from_name", True,
                            "\"Artist - Title.mp3\" or \"03 - Title.mp3\": used when the tags are empty, and "
                            "written into the sorted track (in Copy mode only the copy is changed).")
        self.c_covers = check("Copy album art (cover.jpg, folder.jpg) into album folders", "covers", True,
                              "The cover picture next to an album is copied along with it. Pictures embedded "
                              "inside tracks travel with them anyway.")
        self.c_verify = check("Check every copy against the original", "verify", True,
                              "Reads each copied track back and compares it byte for byte with the original. "
                              "Moves to another drive are always checked before the original is removed.")
        self.c_preview = QCheckBox("Preview only (nothing is copied, moved or changed)")
        self.c_preview.setToolTip("Shows in the results where every track would go. Your tracks stay on the "
                                  "list afterwards, so you can untick this and sort for real.")
        self.c_auto = check("Auto-start sorting when music is dropped", "auto_start", False,
                            "Off: dropped music waits on the list until you press Sort music.\nOn: sorting "
                            "starts by itself (Copy mode only; Move always waits for you).")
        boxes = [self.c_dupes, self.c_sound, self.c_dupfolder, self.c_fill, self.c_covers, self.c_verify,
                 self.c_preview, self.c_auto]
        for i, c in enumerate(boxes):
            g.addWidget(c, i // 2, 2 + i % 2)
        gb = QHBoxLayout()
        b_genres = QPushButton("Edit genres…")
        b_genres.clicked.connect(lambda: open_path(engine.GENRES_FILE))
        b_reload = QPushButton("Reload genres")
        b_reload.clicked.connect(self.reload_genres)
        gb.addWidget(b_genres)
        gb.addWidget(b_reload)
        gb.addStretch()
        g.addLayout(gb, 4, 2, 1, 2)
        g.setColumnStretch(3, 1)
        lay.addLayout(g)

        run = QHBoxLayout()
        self.b_start = QPushButton("Sort music")
        self.b_start.setObjectName("Primary")
        self.b_start.clicked.connect(self.start)
        self.b_pause = QPushButton("Pause")
        self.b_pause.setEnabled(False)
        self.b_pause.setToolTip("Pause after the current track. You can keep dropping music in while paused.")
        self.b_pause.clicked.connect(self.pause_clicked)
        self.b_stop = QPushButton("Stop")
        self.b_stop.setEnabled(False)
        self.b_stop.setToolTip("Finish the current track and put the rest back on the list.")
        self.b_stop.clicked.connect(self.stop_clicked)
        self.bar = QProgressBar()
        self.bar.setMinimumHeight(24)
        self.bar.setFormat("")
        for b in (self.b_start, self.b_pause, self.b_stop):
            run.addWidget(b)
        run.addWidget(self.bar, 1)
        lay.addLayout(run)
        srow = QHBoxLayout()
        self.status = QLabel("Ready. Everything runs on this computer.")
        self.status.setObjectName("Hint")
        self.eta = QLabel("")
        self.eta.setObjectName("Hint")
        srow.addWidget(self.status, 1)
        srow.addWidget(self.eta)
        lay.addLayout(srow)

        self.table = make_table(["Track", "Genre", "Sub-genre", "Artist", "Album", "Result"],
                                [230, 120, 150, 150, 160])
        self.table.cellDoubleClicked.connect(self._open_result)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._result_menu)
        lay.addWidget(self.table, 1)
        tip = QLabel("Double-click a result to open the folder it went into. Right-click to play it.")
        tip.setObjectName("Hint")
        lay.addWidget(tip)
        return w

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def output(self) -> Path:
        return Path(self.out_edit.text().strip() or Path.home() / "Music" / "Sorted Music").expanduser()

    def options(self) -> engine.SortOptions:
        return engine.SortOptions(
            output=self.output(), move=self.r_move.isChecked(), layout=self.layout_box.currentData(),
            rename=self.rename_box.currentData(), naming=self.naming_box.currentData(),
            unknown_genre=self.unknown_box.currentData(), skip_duplicates=self.c_dupes.isChecked(),
            duplicates_folder=self.c_dupfolder.isChecked(), same_sound=self.c_sound.isChecked(),
            fill_from_name=self.c_fill.isChecked(), write_tags=self.c_fill.isChecked(),
            covers=self.c_covers.isChecked(), verify=self.c_verify.isChecked(),
            preview=self.c_preview.isChecked())

    def _save_settings(self):
        s = self.settings
        s.update(output=self.out_edit.text().strip(), move=self.r_move.isChecked(),
                 layout=self.layout_box.currentData(), rename=self.rename_box.currentData(),
                 naming=self.naming_box.currentData(), unknown_genre=self.unknown_box.currentData(),
                 skip_duplicates=self.c_dupes.isChecked(), same_sound=self.c_sound.isChecked(),
                 duplicates_folder=self.c_dupfolder.isChecked(), fill_from_name=self.c_fill.isChecked(),
                 covers=self.c_covers.isChecked(), verify=self.c_verify.isChecked(),
                 auto_start=self.c_auto.isChecked(), min_tracks=self.s_min.value())
        try:
            engine.save_settings(s)
        except OSError as e:
            engine.log_error("Saving settings", e)

    def pick_files(self):
        exts = " ".join(f"*{e}" for e in sorted(engine.AUDIO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add music", str(Path.home() / "Music"),
                                                f"Music ({exts});;All files (*)")
        self.add_paths(files)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of music", str(Path.home() / "Music"))
        if d:
            self.add_paths([d])

    def pick_output(self):
        d = QFileDialog.getExistingDirectory(self, "Where should sorted music go?", str(self.output()))
        if d:
            self.out_edit.setText(d)
            self._set("output", d)
            self.refresh_artists()
            self.refresh_dupes()

    def add_paths(self, paths):
        paths = [p for p in paths if p]
        if not paths:
            return
        if self.scanner:
            self._scan_queue += paths
            return
        self.scanner = ScanWorker(paths, self.output())
        self.scanner.found.connect(lambda n, d: self.status.setText(
            f"Looking for music… {n:,} found" + (f"  ({Path(d).name})" if d else "")))
        self.scanner.finished.connect(self._scan_done)
        if self.worker is None:
            self.bar.setRange(0, 0)
            self.b_stop.setEnabled(True)
        self.scanner.start()

    def _scan_done(self):
        files = self.scanner.files if self.scanner else []
        stopped = self.scanner._stop if self.scanner else False
        self.scanner = None
        known = {os.path.normcase(str(p)) for p in self.pending}
        new = [f for f in files if os.path.normcase(str(f)) not in known]
        self.pending += new
        if self.worker is None:
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
            self.b_stop.setEnabled(False)
        if self._scan_queue and not stopped:
            q, self._scan_queue = self._scan_queue, []
            self.add_paths(q)
            return
        self._scan_queue = []
        self.status.setText(f"Added {len(new):,} tracks." if new else "No new music found there.")
        self._update_ready()
        if new and self.c_auto.isChecked() and self.r_copy.isChecked() and self.worker is None:
            self.start()

    def clear_pending(self):
        self.pending = []
        self._update_ready()

    def _update_ready(self):
        n = len(self.pending)
        self.ready_lbl.setText(f"{n:,} tracks waiting" if n else "No music waiting")
        self.b_start.setEnabled(bool(n) and self.worker is None and self.sorter is not None)
        self.b_clear.setEnabled(bool(n) and self.worker is None)

    def reload_genres(self):
        try:
            if self.sorter is None:
                self.sorter = engine.Sorter()
            else:
                self.sorter.reload_genres()
            self.genre_error = ""
            self.status.setText(f"Genres reloaded: {len(self.sorter.genres)} genre and sub-genre folders.")
            self._update_ready()
        except ValueError as e:
            QMessageBox.warning(self, "genres.json", str(e))

    # ---- running ---- #
    def start(self):
        if self.worker or not self.pending or self.sorter is None:
            return
        opt = self.options()
        if opt.move and not opt.preview:
            if QMessageBox.question(self, "Move tracks?",
                                    f"Move {len(self.pending):,} tracks into {opt.output}?\n\nThey'll no longer "
                                    "be in their current folders (Undo last sort puts them back).") \
                    != QMessageBox.Yes:
                return
        if not opt.preview:
            need, free = engine.space_needed(self.pending, opt.output, opt.move)
            if need > free:
                if QMessageBox.warning(self, "Not enough space",
                                       f"This needs about {engine.human_size(need)}, but only "
                                       f"{engine.human_size(free)} is free there.\n\nSort anyway? Tracks that "
                                       "don't fit are left where they are.",
                                       QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                    return
        self._save_settings()
        files, self.pending = self.pending, []
        self.table.setRowCount(0)
        self._run_counts = {"sorted": 0, "copies": 0, "skipped": 0}
        self.worker = SortWorker(self.sorter, files, opt)
        self.worker.status.connect(self.status.setText)
        self.worker.progress.connect(self._progress)
        self.worker.result.connect(self._on_result)
        self.worker.finished.connect(self._sort_done)
        self._started_at = time.monotonic()
        self.bar.setRange(0, len(files))
        self.b_start.setEnabled(False)
        self.b_clear.setEnabled(False)
        self.b_pause.setEnabled(True)
        self.b_pause.setText("Pause")
        self.b_stop.setEnabled(True)
        self.b_undo.setEnabled(False)
        self.worker.start()
        self._update_ready()

    def _progress(self, i, total):
        self.bar.setValue(i)
        self.bar.setFormat(f"{i:,} of {total:,} tracks")
        pct = i * 100 // max(total, 1)
        self.setWindowTitle(f"{pct}% · {APP_NAME}" + (" (paused)" if self.worker and self.worker.paused else ""))
        if i:
            left = (time.monotonic() - self._started_at) / i * (total - i)
            self.eta.setText(f"about {int(left // 60)} min {int(left % 60)} s left" if left >= 5 else "")

    def _on_result(self, r: engine.SortResult):
        m = r.meta or {}
        row = self.table.rowCount()
        self.table.insertRow(row)
        where = r.dest or r.duplicate_dest or r.source
        self.table.setItem(row, 0, item(r.source.name, str(where)))
        self.table.setItem(row, 1, item(m.get("_genre", "")))
        self.table.setItem(row, 2, item(m.get("_sub", "")))
        self.table.setItem(row, 3, item(m.get("_artist", "")))
        self.table.setItem(row, 4, item(m.get("album", "")))
        res = item(r.error or r.outcome(), str(r.duplicate_of) if r.duplicate_of else None)
        if r.error:
            res.setForeground(QColor("#c0392b"))
            self._run_counts["skipped"] += 1
        elif r.duplicate_of:
            res.setForeground(QColor(ORANGE))
            res.setToolTip(f"Matches {r.duplicate_of}" + (f"\nPut in {r.duplicate_dest}" if r.duplicate_dest else
                                                         "\nLeft where it is"))
            self._run_counts["copies"] += 1
        else:
            self._run_counts["sorted"] += 1
        self.table.setItem(row, 5, res)
        if row % 20 == 0:
            self.table.scrollToBottom()

    def _sort_done(self):
        w = self.worker
        self.worker = None
        if w._stop and w.done_count < len(w.files):  # Stop: the rest go back on the list
            self.pending = w.files[w.done_count:] + self.pending
        if w.opt.preview:
            self.pending = w.files + [p for p in self.pending if p not in w.files]
        c = self._run_counts
        what = "Preview finished" if w.opt.preview else "Done"
        self.status.setText(f"{what}: {c['sorted']:,} sorted, {c['copies']:,} copies, {c['skipped']:,} skipped."
                            + ("  Your tracks are still on the list." if w.opt.preview else ""))
        self.setWindowTitle(APP_NAME)
        self.eta.setText("")
        self.b_pause.setEnabled(False)
        self.b_stop.setEnabled(False)
        self.b_undo.setEnabled(True)
        self.table.scrollToBottom()
        self._update_ready()
        if self.pending and self.c_auto.isChecked() and self.r_copy.isChecked() and not w._stop \
                and not w.opt.preview:
            QTimer.singleShot(300, self.start)

    def pause_clicked(self):
        w = self.worker
        if not w:
            return
        if w.paused:
            w.resume()
            self.b_pause.setText("Pause")
            self.setWindowTitle(self.windowTitle().replace(" (paused)", ""))
        else:
            w.pause()
            self.b_pause.setText("Resume")
            self.setWindowTitle(self.windowTitle() + " (paused)")

    def stop_clicked(self):
        if self.scanner:
            self.scanner.stop()
        if self.worker:
            self.worker.stop()
            self.b_pause.setEnabled(False)
            self.status.setText("Stopping after the current track…")

    def _open_result(self, row, _col):
        it = self.table.item(row, 0)
        if it:
            p = Path(it.data(Qt.UserRole))
            show_in_folder(p)

    def _result_menu(self, pos):
        it = self.table.itemAt(pos)
        if not it:
            return
        p = Path(self.table.item(it.row(), 0).data(Qt.UserRole))
        menu = QMenu(self)
        menu.addAction("Play", lambda: open_path(p))
        menu.addAction("Show in folder", lambda: show_in_folder(p))
        dup = self.table.item(it.row(), 5).data(Qt.UserRole)
        if dup:
            menu.addAction("Show the sorted track it matches", lambda: show_in_folder(Path(dup)))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ---- background jobs with a progress window ---- #
    def run_busy(self, title: str, fn, done):
        """Run fn(progress) in the background behind a progress window, then done(result)."""
        if self.busy or self.worker:
            QMessageBox.information(self, APP_NAME, "Wait for the current job to finish first.")
            return
        dlg = QProgressDialog(title, None, 0, 0, self)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        w = self.busy = BusyWorker(fn)

        def prog(i, n, t):
            dlg.setRange(0, n)
            dlg.setValue(i)
            dlg.setLabelText(f"{title}\n{t}" if t else title)

        def fin():
            dlg.close()
            self.busy = None
            if w.exc:
                QMessageBox.warning(self, APP_NAME, "That didn't finish. Your music is safe.\n\n"
                                    + engine.explain_error(w.exc))
                return
            done(w.result)

        w.prog.connect(prog)
        w.finished.connect(fin)
        w.start()

    def undo_last(self):
        out = self.output()
        entries = engine.RunJournal(out).load()
        if not entries:
            QMessageBox.information(self, "Undo", "There's no sort to undo in this output folder.")
            return
        moved = sum(1 for e in entries if e.get("move"))
        copies = len(entries) - moved
        trash = "to the Recycle Bin / Trash" if engine.can_use_trash() else "for good"
        if QMessageBox.question(self, "Undo last sort?",
                                f"Undo the last sort into {out}?\n\n"
                                + (f"• {moved:,} moved tracks go back where they came from\n" if moved else "")
                                + (f"• {copies:,} copies are removed ({trash}); the originals stay put\n"
                                   if copies else "")) != QMessageBox.Yes:
            return

        def done(res):
            n, problems = res
            self.status.setText(f"Undone: {n:,} files.")
            if problems:
                QMessageBox.warning(self, "Undo", f"{len(problems)} couldn't be undone:\n\n"
                                    + "\n".join(problems[:15]))
            self.refresh_artists()
            self.refresh_dupes()

        self.run_busy("Undoing the last sort…", lambda p: engine.undo_last(out, p), done)

    # ---- Artists tab ------------------------------------------------------- #
    def _build_artists_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        intro = QLabel("Every artist in your sorted music. Double-click an artist to rename them: their tags "
                       "and folder change together. If one artist shows up under two spellings, select both "
                       "and press Merge selected. Click an artist to see their tracks; select tracks that "
                       "belong to someone else and move them.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        top = QHBoxLayout()
        self.a_filter = QLineEdit()
        self.a_filter.setPlaceholderText("Find an artist…")
        self.a_filter.textChanged.connect(self._fill_artists)
        top.addWidget(self.a_filter, 1)
        top.addWidget(QLabel("Show artists with at least"))
        self.s_min = QSpinBox()
        self.s_min.setRange(1, 999)
        self.s_min.setValue(int(self.settings.get("min_tracks", 1)))
        self.s_min.setSuffix(" tracks")
        self.s_min.setToolTip("Hide artists with only a track or two. They're only hidden from this list.")
        self.s_min.valueChanged.connect(self._fill_artists)
        top.addWidget(self.s_min)
        lay.addLayout(top)

        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.alist = QListWidget()
        self.alist.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.alist.itemDoubleClicked.connect(lambda _it: self.rename_artist())
        self.alist.currentItemChanged.connect(lambda *_: self._fill_tracks())
        self.alist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.alist.customContextMenuRequested.connect(self._artist_menu)
        ll.addWidget(self.alist, 1)
        brow = QHBoxLayout()
        for label, fn in (("Rename…", self.rename_artist), ("Merge selected", self.merge_artists),
                          ("Show folder", self._artist_folder), ("Refresh", self.refresh_artists)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            brow.addWidget(b)
        ll.addLayout(brow)
        self.a_status = QLabel("")
        self.a_status.setObjectName("Hint")
        ll.addWidget(self.a_status)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.t_title = QLabel("Pick an artist")
        self.t_title.setObjectName("DropTitle")
        rl.addWidget(self.t_title)
        self.ttable = make_table(["#", "Title", "Album", "Length", "File"], [40, 220, 170, 60])
        self.ttable.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.ttable.cellDoubleClicked.connect(lambda r, _c: open_path(Path(self.ttable.item(r, 0)
                                                                           .data(Qt.UserRole))))
        self.ttable.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ttable.customContextMenuRequested.connect(self._track_menu)
        rl.addWidget(self.ttable, 1)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Move selected to:"))
        self.move_box = QComboBox()
        self.move_box.setMinimumWidth(220)
        mrow.addWidget(self.move_box, 1)
        b_move = QPushButton("Move")
        b_move.clicked.connect(self.move_tracks)
        mrow.addWidget(b_move)
        rl.addLayout(mrow)
        hint = QLabel("Double-click a track to play it. Moving changes the track's Artist tag and puts it in "
                      "that artist's folder; nothing is deleted.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        rl.addWidget(hint)
        split.addWidget(right)
        split.setSizes([340, 640])
        lay.addWidget(split, 1)
        self._artists: list[dict] = []
        return w

    def refresh_artists(self):
        if self.busy:
            return
        out = self.output()
        self._artists = engine.artists(out) if (out / engine.LIBRARY_NAME).exists() else []
        self._fill_artists()

    def _fill_artists(self):
        keep = {it.data(Qt.UserRole) for it in self.alist.selectedItems()}
        self.alist.blockSignals(True)
        self.alist.clear()
        text = self.a_filter.text().strip().casefold()
        shown = 0
        for a in self._artists:
            if len(a["tracks"]) < self.s_min.value() or (text and text not in a["name"].casefold()):
                continue
            n, al = len(a["tracks"]), len(a["albums"])
            it = QListWidgetItem(f"{a['name']}   ·   {n:,} track{'s' * (n != 1)}"
                                 + (f", {al} album{'s' * (al != 1)}" if al else ""))
            it.setData(Qt.UserRole, a["key"])
            if a["name"] in (engine.UNKNOWN_ARTIST, engine.COMPILATIONS):
                f = it.font()
                f.setItalic(True)
                it.setFont(f)
            self.alist.addItem(it)
            it.setSelected(a["key"] in keep)
            shown += 1
        self.alist.blockSignals(False)
        total = sum(len(a["tracks"]) for a in self._artists)
        self.a_status.setText(f"{len(self._artists):,} artists, {total:,} tracks" +
                              (f" ({len(self._artists) - shown:,} hidden)" if shown < len(self._artists) else "")
                              if self._artists else "Nothing sorted into this output folder yet.")
        self.move_box.clear()
        for a in self._artists:
            if a["name"] not in (engine.UNKNOWN_ARTIST, engine.COMPILATIONS):
                self.move_box.addItem(a["name"], a["name"])
        self.move_box.insertItem(0, "New artist…", "__new__")
        self.move_box.setCurrentIndex(0)
        self._fill_tracks()

    def _artist(self, key) -> dict | None:
        return next((a for a in self._artists if a["key"] == key), None)

    def _fill_tracks(self):
        it = self.alist.currentItem()
        a = self._artist(it.data(Qt.UserRole)) if it else None
        self.ttable.setRowCount(0)
        if not a:
            self.t_title.setText("Pick an artist")
            return
        self.t_title.setText(a["name"])
        lib = engine.Library(self.output())
        rows = sorted(a["tracks"], key=lambda r: (lib.tracks.get(r, {}).get("album", "").casefold(),
                                                  engine._track_no(lib.tracks.get(r, {})) or "999", r))
        for rel in rows:
            t = lib.tracks.get(rel, {})
            r = self.ttable.rowCount()
            self.ttable.insertRow(r)
            self.ttable.setItem(r, 0, item(engine._track_no(t), str(self.output() / rel)))
            self.ttable.item(r, 0).setData(Qt.UserRole + 1, rel)
            self.ttable.setItem(r, 1, item(t.get("title") or Path(rel).stem))
            self.ttable.setItem(r, 2, item(t.get("album", "")))
            self.ttable.setItem(r, 3, item(engine.fmt_length(t.get("length", 0))))
            self.ttable.setItem(r, 4, item(rel))

    def _artist_menu(self, pos):
        it = self.alist.itemAt(pos)
        if not it:
            return
        menu = QMenu(self)
        menu.addAction("Rename…", self.rename_artist)
        into = menu.addMenu("Merge into")
        key = it.data(Qt.UserRole)
        for a in self._artists:
            if a["key"] != key and a["name"] not in (engine.UNKNOWN_ARTIST, engine.COMPILATIONS):
                into.addAction(a["name"], lambda n=a["name"], k=key: self._retarget(self._artist(k)["tracks"], n))
        menu.addAction("Show folder", self._artist_folder)
        menu.exec(self.alist.viewport().mapToGlobal(pos))

    def _track_menu(self, pos):
        it = self.ttable.itemAt(pos)
        if not it:
            return
        p = Path(self.ttable.item(it.row(), 0).data(Qt.UserRole))
        menu = QMenu(self)
        menu.addAction("Play", lambda: open_path(p))
        menu.addAction("Show in folder", lambda: show_in_folder(p))
        menu.exec(self.ttable.viewport().mapToGlobal(pos))

    def rename_artist(self):
        it = self.alist.currentItem()
        a = self._artist(it.data(Qt.UserRole)) if it else None
        if not a:
            return
        if a["name"] == engine.COMPILATIONS:
            QMessageBox.information(self, "Rename", "Compilations holds albums by Various Artists, so it can't "
                                    "be renamed. To give a track an artist, select it on the right and use "
                                    "Move selected to.")
            return
        if a["name"] == engine.UNKNOWN_ARTIST:
            QMessageBox.information(self, "Rename", "These tracks have no artist tag, so they're probably by "
                                    "different people. Select the tracks on the right that belong together "
                                    "and use Move selected to › New artist… instead.")
            return
        name, ok = QInputDialog.getText(self, "Rename artist", f"New name for {a['name']}:", text=a["name"])
        name = engine.tidy(name)
        if not ok or not name or name == a["name"]:
            return
        other = next((x for x in self._artists if x["key"] == engine.name_key(name) and x is not a), None)
        msg = (f"{other['name']} already exists, so {a['name']} will be merged into them." if other else "")
        if QMessageBox.question(self, "Rename artist",
                                f"Rename {a['name']} to {name}?\n\n{msg}\n\nTheir {len(a['tracks']):,} tracks "
                                "get the new Artist / Album Artist tags and move into that folder. Nothing is "
                                "deleted.") != QMessageBox.Yes:
            return
        self._retarget(a["tracks"], name)

    def merge_artists(self):
        sel = [self._artist(it.data(Qt.UserRole)) for it in self.alist.selectedItems()]
        sel = [a for a in sel if a and a["name"] not in (engine.UNKNOWN_ARTIST, engine.COMPILATIONS)]
        if len(sel) < 2:
            QMessageBox.information(self, "Merge", "Select two or more artists first (Ctrl-click), then press "
                                    "Merge selected.")
            return
        names = [a["name"] for a in sorted(sel, key=lambda a: -len(a["tracks"]))]
        keep, ok = QInputDialog.getItem(self, "Merge artists", "Keep which name?", names, 0, True)
        keep = engine.tidy(keep)
        if not ok or not keep:
            return
        rels = [r for a in sel if a["name"] != keep for r in a["tracks"]]
        self._retarget(rels, keep)

    def move_tracks(self):
        rels = sorted({self.ttable.item(i.row(), 0).data(Qt.UserRole + 1) for i in self.ttable.selectedItems()})
        if not rels:
            QMessageBox.information(self, "Move", "Select the tracks to move first.")
            return
        target = self.move_box.currentData()
        if target == "__new__":
            target, ok = QInputDialog.getText(self, "New artist", "Who is it by?")
            target = engine.tidy(target)
            if not ok or not target:
                return
        self._retarget(rels, target)

    def _retarget(self, rels: list[str], name: str):
        out = self.output()

        def done(res):
            n, problems = res
            self.a_status.setText(f"{n:,} tracks now under {name}.")
            if problems:
                QMessageBox.warning(self, "Artists", "\n".join(problems[:20]))
            self.refresh_artists()
            for i in range(self.alist.count()):
                if self.alist.item(i).data(Qt.UserRole) == engine.name_key(name):
                    self.alist.setCurrentRow(i)
                    break

        self.run_busy(f"Moving tracks to {name}…", lambda p: engine.retarget_tracks(out, rels, name, p), done)

    def _artist_folder(self):
        it = self.alist.currentItem()
        a = self._artist(it.data(Qt.UserRole)) if it else None
        if a and a["tracks"]:
            show_in_folder(self.output() / a["tracks"][0])

    # ---- Duplicates tab ----------------------------------------------------- #
    def _build_dupes_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        intro = QLabel("Copies found while sorting wait in the Duplicates folder. Click one to compare it with "
                       "the sorted track it matches; differences are in orange. Nothing is deleted unless you "
                       "confirm, and then it goes to the Recycle Bin / Trash.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.dlist = QListWidget()
        self.dlist.currentRowChanged.connect(self._show_dupe)
        ll.addWidget(self.dlist, 1)
        r1 = QHBoxLayout()
        for label, fn in (("Tick all", lambda: self._tick_all(True)), ("Untick all", lambda: self._tick_all(False)),
                          ("Delete ticked…", self.delete_ticked)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            r1.addWidget(b)
        ll.addLayout(r1)
        b_find = QPushButton("Find copies in sorted folders…")
        b_find.setToolTip("Compares every sorted track and moves extra copies into Duplicates for review. "
                          "Nothing is deleted; Undo last sort puts them back.")
        b_find.clicked.connect(self.gather_copies)
        ll.addWidget(b_find)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        cards = QHBoxLayout()
        self.card_dup, self.card_orig = QLabel(), QLabel()
        for c in (self.card_dup, self.card_orig):
            c.setObjectName("Card")
            c.setTextFormat(Qt.RichText)
            c.setWordWrap(True)
            c.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            c.setMargin(12)
            c.setMinimumWidth(260)
            cards.addWidget(c, 1)
        rl.addLayout(cards, 1)
        prow = QHBoxLayout()
        self.b_play_dup = QPushButton("Play duplicate")
        self.b_play_dup.clicked.connect(lambda: self._cur_dupe() and open_path(self._cur_dupe()["dup"]))
        self.b_play_orig = QPushButton("Play sorted track")
        self.b_play_orig.clicked.connect(lambda: self._cur_dupe() and self._cur_dupe()["original"]
                                         and open_path(self._cur_dupe()["original"]))
        prow.addWidget(self.b_play_dup)
        prow.addWidget(self.b_play_orig)
        rl.addLayout(prow)
        nrow = QHBoxLayout()
        b_prev = QPushButton("◀ Previous")
        b_prev.clicked.connect(lambda: self.dlist.setCurrentRow(max(self.dlist.currentRow() - 1, 0)))
        b_next = QPushButton("Next ▶")
        b_next.clicked.connect(lambda: self.dlist.setCurrentRow(min(self.dlist.currentRow() + 1,
                                                                    self.dlist.count() - 1)))
        self.b_del_one = QPushButton("Delete this duplicate…")
        self.b_del_one.clicked.connect(self.delete_current)
        nrow.addWidget(b_prev)
        nrow.addWidget(b_next)
        nrow.addStretch()
        nrow.addWidget(self.b_del_one)
        rl.addLayout(nrow)
        self.d_status = QLabel("")
        self.d_status.setObjectName("Hint")
        rl.addWidget(self.d_status)
        split.addWidget(right)
        split.setSizes([320, 660])
        lay.addWidget(split, 1)
        self._dupes: list[dict] = []
        return w

    def refresh_dupes(self):
        if self.busy:
            return
        self._dupes = engine.list_duplicates(self.output())
        self.dlist.blockSignals(True)
        self.dlist.clear()
        for d in self._dupes:
            it = QListWidgetItem(d["dup"].name)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            if not d["original"]:
                it.setToolTip("Its matching sorted track is gone, so this one can't be deleted from here.")
            self.dlist.addItem(it)
        self.dlist.blockSignals(False)
        self.d_status.setText(f"{len(self._dupes):,} duplicates." if self._dupes else "No duplicates. 🎵")
        if self._dupes:
            self.dlist.setCurrentRow(0)
        else:
            self.card_dup.setText("")
            self.card_orig.setText("")

    def _cur_dupe(self) -> dict | None:
        i = self.dlist.currentRow()
        return self._dupes[i] if 0 <= i < len(self._dupes) else None

    def _show_dupe(self, _row=None):
        d = self._cur_dupe()
        if not d:
            return
        a = engine.track_info(d["dup"])
        b = engine.track_info(d["original"]) if d["original"] else None
        fields = [("Name", "name"), ("Title", "title"), ("Artist", "artist"), ("Album", "album"),
                  ("Genre", "genre"), ("Year", "date"), ("Length", "length"), ("Bitrate", "bitrate"),
                  ("Sample rate", "sample_rate"), ("Size", "size"), ("Format", "format"), ("Folder", "folder")]

        def fmt(info, k):
            v = info.get(k, "")
            if k == "length":
                return engine.fmt_length(v)
            if k == "bitrate":
                return f"{v // 1000} kbps" if v else ""
            if k == "sample_rate":
                return f"{v / 1000:g} kHz" if v else ""
            if k == "size":
                return engine.human_size(v)
            return str(v)

        def card(info, other, head):
            rows = []
            for label, k in fields:
                v = fmt(info, k)
                diff = other is not None and v != fmt(other, k)
                style = f" style='color:{ORANGE};font-weight:600'" if diff else ""
                rows.append(f"<tr><td style='padding-right:10px'><b>{label}</b></td>"
                            f"<td{style}>{v.replace('&', '&amp;').replace('<', '&lt;')}</td></tr>")
            return f"<h3>{head}</h3><table>{''.join(rows)}</table>"

        self.card_dup.setText(card(a, b, "Duplicate · can be deleted"))
        self.card_orig.setText(card(b, a, "Sorted track · stays") if b else
                               "<h3>Sorted track</h3>It's no longer there, so this duplicate is kept.")
        self.b_del_one.setEnabled(bool(d["original"]))
        self.b_play_orig.setEnabled(bool(d["original"]))

    def _tick_all(self, on: bool):
        for i in range(self.dlist.count()):
            self.dlist.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _delete(self, paths: list[Path]):
        if not paths:
            return
        where = "to the Recycle Bin / Trash" if engine.can_use_trash() else \
            "FOR GOOD (install send2trash to use the Recycle Bin)"
        if QMessageBox.question(self, "Delete duplicates?",
                                f"Delete {len(paths):,} duplicate{'s' * (len(paths) != 1)} {where}?\n\nOnly "
                                "copies whose matching sorted track still exists are deleted.") \
                != QMessageBox.Yes:
            return
        out = self.output()
        row = self.dlist.currentRow()

        def done(res):
            n, kept, problems = res
            self.refresh_dupes()
            self.dlist.setCurrentRow(min(row, self.dlist.count() - 1))
            self.d_status.setText(f"Deleted {n:,}." + (f" Kept {kept:,}." if kept else ""))
            if problems:
                QMessageBox.information(self, "Duplicates", "\n".join(problems[:20]))

        self.run_busy("Deleting duplicates…", lambda p: engine.delete_duplicates(out, paths, p), done)

    def delete_current(self):
        d = self._cur_dupe()
        if d and d["original"]:
            self._delete([d["dup"]])

    def delete_ticked(self):
        paths = [self._dupes[i]["dup"] for i in range(self.dlist.count())
                 if self.dlist.item(i).checkState() == Qt.Checked]
        if not paths:
            QMessageBox.information(self, "Duplicates", "Tick the duplicates to delete first (or Tick all).")
            return
        self._delete(paths)

    def gather_copies(self):
        out = self.output()
        if not out.is_dir():
            return
        if QMessageBox.question(self, "Find copies in sorted folders?",
                                "Compare every sorted track and move extra copies into Duplicates for review?"
                                "\n\nOne of each stays where it is. Nothing is deleted, and Undo last sort puts "
                                "them all back.") != QMessageBox.Yes:
            return

        def done(res):
            n, problems = res
            self.refresh_dupes()
            self.d_status.setText(f"Moved {n:,} copies into Duplicates." if n else "No copies found.")
            if problems:
                QMessageBox.information(self, "Duplicates", "\n".join(problems[:20]))

        self.run_busy("Comparing sorted tracks…", lambda p: engine.gather_copies(out, p), done)

    # ---- Search tab --------------------------------------------------------- #
    def _build_search_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        intro = QLabel("Find sorted tracks by title, artist, album, genre or year. Every word you type must "
                       "match: \"queen 1977\", \"miles davis blue\", \"christmas\".")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("e.g. beatles abbey road")
        self.search_edit.returnPressed.connect(self.run_search)
        b = QPushButton("Search")
        b.setObjectName("Primary")
        b.clicked.connect(self.run_search)
        row.addWidget(self.search_edit, 1)
        row.addWidget(b)
        lay.addLayout(row)
        self.stable = make_table(["Title", "Artist", "Album", "Genre", "Year", "Length"],
                                 [230, 170, 190, 140, 60])
        self.stable.cellDoubleClicked.connect(lambda r, _c: open_path(Path(self.stable.item(r, 0)
                                                                           .data(Qt.UserRole))))
        self.stable.setContextMenuPolicy(Qt.CustomContextMenu)
        self.stable.customContextMenuRequested.connect(self._search_menu)
        lay.addWidget(self.stable, 1)
        self.s_status = QLabel("Double-click a track to play it.")
        self.s_status.setObjectName("Hint")
        lay.addWidget(self.s_status)
        return w

    def run_search(self):
        text = self.search_edit.text().strip()
        if not text:
            return
        hits = engine.search(self.output(), text)
        self.stable.setRowCount(0)
        for p, t in hits:
            r = self.stable.rowCount()
            self.stable.insertRow(r)
            self.stable.setItem(r, 0, item(t.get("title") or p.stem, str(p)))
            self.stable.setItem(r, 1, item(t.get("artist") or t.get("_artist", "")))
            self.stable.setItem(r, 2, item(t.get("album", "")))
            self.stable.setItem(r, 3, item(" › ".join(x for x in (t.get("_genre"), t.get("_sub")) if x)))
            self.stable.setItem(r, 4, item(engine.year_of(t)))
            self.stable.setItem(r, 5, item(engine.fmt_length(t.get("length", 0))))
        self.s_status.setText(f"{len(hits):,} tracks." if hits else "Nothing matched. Try fewer words.")

    def _search_menu(self, pos):
        it = self.stable.itemAt(pos)
        if not it:
            return
        p = Path(self.stable.item(it.row(), 0).data(Qt.UserRole))
        menu = QMenu(self)
        menu.addAction("Play", lambda: open_path(p))
        menu.addAction("Show in folder", lambda: show_in_folder(p))
        menu.exec(self.stable.viewport().mapToGlobal(pos))

    # ---- updates and problem reports ---------------------------------------- #
    def check_updates(self, quiet: bool):
        """Look for a newer release on GitHub. Nothing is downloaded unless the user says yes."""
        if self._upd_check or self._upd_apply:
            return
        w = self._upd_check = UpdateCheckWorker()

        def done():
            self._upd_check = None
            if w.error:
                if not quiet:
                    QMessageBox.information(self, "Check for updates",
                                            f"Couldn't reach GitHub to check for updates.\n\n{w.error}")
                return
            if not w.newer:
                if not quiet:
                    QMessageBox.information(self, "Check for updates", f"You have the latest version ({w.local}).")
                return
            remote = w.rel.get("version", "")
            if self.worker or self.busy:
                self.status.setText(f"Version {remote} is available. Use Help › Check for updates when sorting "
                                    "is finished.")
                return
            box = QMessageBox(QMessageBox.Question, "Update available",
                              f"{APP_NAME} {remote} is available (you have {w.local}).\n\nDownload and install it "
                              "now? Your settings and genres are kept, the download is checked before "
                              "installing, and the current version is backed up (Help › Roll back).", parent=self)
            notes = (w.rel.get("notes") or "").strip()
            if notes:
                box.setDetailedText(notes)
            b_yes = box.addButton("Update now", QMessageBox.AcceptRole)
            box.addButton("Not now", QMessageBox.RejectRole)
            b_page = box.addButton("What's new…", QMessageBox.HelpRole)
            box.exec()
            if box.clickedButton() is b_page:
                QDesktopServices.openUrl(QUrl(w.rel.get("page") or updater.RELEASES_URL))
            elif box.clickedButton() is b_yes:
                self._apply_update(w.rel)

        w.finished.connect(done)
        w.start()

    def roll_back(self):
        if self._upd_check or self._upd_apply or self.worker:
            return
        versions = updater.backups()
        if not versions:
            QMessageBox.information(self, "Roll back", "There is no earlier version saved yet. One is saved "
                                    "automatically each time you update.")
            return
        ver, ok = QInputDialog.getItem(self, "Roll back to an earlier version",
                                       f"You have {updater.local_version()}. Go back to:", versions, 0, False)
        if ok and ver:
            self._apply_update(None, rollback_to=ver)

    def _apply_update(self, rel=None, rollback_to: str = ""):
        w = self._upd_apply = UpdateApplyWorker(rel, rollback_to)
        w.status.connect(self.status.setText)
        self.bar.setRange(0, 0)

        def done():
            self._upd_apply = None
            self.bar.setRange(0, 1)
            if w.error:
                self.status.setText("The update didn't finish.")
                QMessageBox.warning(self, "Update failed", f"The update didn't finish:\n\n{w.error}\n\n"
                                    "Nothing was changed. You can try again later.")
                return
            extra = ""
            if w.info.get("requirements_changed"):
                extra += "\n\nThis update needs new parts, which install the next time you start the app."
            if w.info.get("kept"):
                extra += ("\n\nYour edited " + ", ".join(w.info["kept"]) + " was kept; the new version was saved "
                          "next to it with .new on the end.")
            self.status.setText(f"Updated to {w.info.get('version')}. Restart to use it.")
            if QMessageBox.question(self, "Updated", f"Updated to {w.info.get('version')}.{extra}\n\nRestart now?") \
                    == QMessageBox.Yes:
                self._restart()

        w.finished.connect(done)
        w.start()

    def _restart(self):
        import subprocess
        self._save_settings()
        launcher = engine.APP_DIR / ("Start Solfege (Windows).bat" if os.name == "nt"
                                     else "Start Solfege (Mac).command")
        try:
            if launcher.exists() and (engine.APP_DIR / ".venv").exists():
                if os.name == "nt":
                    subprocess.Popen(["cmd", "/c", str(launcher)], cwd=str(engine.APP_DIR),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    subprocess.Popen(["/bin/bash", str(launcher)], cwd=str(engine.APP_DIR))
            else:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=str(engine.APP_DIR))
        except Exception as e:
            QMessageBox.warning(self, "Restart", f"Please start {APP_NAME} again yourself.\n\n{e}")
        self._lock_release()
        QApplication.quit()

    def _lock_release(self):
        lock = getattr(self, "lock", None)
        if lock:
            lock.unlock()

    def report_problem(self, note: str = ""):
        from PySide6.QtWidgets import QPlainTextEdit
        dlg = QDialog(self)
        dlg.setWindowTitle("Report a problem")
        dlg.resize(760, 540)
        v = QVBoxLayout(dlg)
        intro = QLabel("Describe what you were doing, then send the report. Read it first: it holds version and "
                       "error details (your Windows user name is hidden), but track and folder names can appear "
                       "in it. Nothing is sent unless you choose to.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        what = QPlainTextEdit()
        what.setPlaceholderText("What happened? e.g. \"I merged two artists and it froze.\"")
        what.setPlainText(note)
        what.setMaximumHeight(90)
        v.addWidget(what)
        body = QPlainTextEdit()
        body.setPlainText(engine.problem_report())
        body.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(body, 1)
        row = QHBoxLayout()
        b_copy, b_save, b_issue, b_close = (QPushButton("Copy report"), QPushButton("Save as file…"),
                                            QPushButton("Open a GitHub issue"), QPushButton("Close"))
        b_issue.setObjectName("Primary")
        for b in (b_copy, b_save, b_issue):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(b_close)
        v.addLayout(row)

        def full():
            n = what.toPlainText().strip()
            return (f"What happened:\n{n}\n\n" if n else "") + body.toPlainText()

        def save():
            fn, _ = QFileDialog.getSaveFileName(dlg, "Save report", str(Path.home() / "solfege-report.txt"),
                                                "Text (*.txt)")
            if fn:
                Path(fn).write_text(full(), "utf-8")

        def issue():
            from urllib.parse import quote
            text = full()
            QApplication.clipboard().setText(text)
            title = (what.toPlainText().strip().splitlines() or ["Problem report"])[0][:80]
            short = text if len(text) < 5500 else text[:5500] + "\n\n… (cut short: the full report is on your " \
                                                                "clipboard, paste it below)"
            QDesktopServices.openUrl(QUrl(f"{updater.PAGE_URL}/issues/new?title={quote(title)}"
                                          f"&body={quote(chr(96) * 3 + chr(10) + short + chr(10) + chr(96) * 3)}"))

        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(full()))
        b_save.clicked.connect(save)
        b_issue.clicked.connect(issue)
        b_close.clicked.connect(dlg.accept)
        dlg.exec()

    def closeEvent(self, e):
        if self.worker or self.busy:
            if QMessageBox.question(self, APP_NAME, "Sorting is still running. Stop after the current track and "
                                    "close?") != QMessageBox.Yes:
                e.ignore()
                return
            if self.worker:
                self.worker.stop()
                self.worker.wait(30000)
            if self.busy:
                self.busy.wait(60000)
        self._save_settings()
        super().closeEvent(e)


def install_error_handler(win):
    """Unexpected errors: log them, say so in plain words and offer a report. The app keeps running."""
    seen: set[str] = set()

    def show(exc_type, exc, tb):
        if exc_type is KeyboardInterrupt:
            return
        exc = exc if exc is not None else exc_type()
        exc.__traceback__ = tb
        engine.log_error("Unexpected error", exc)
        sig = f"{exc_type.__name__}:{str(exc)[:120]}"
        if sig in seen:
            return
        seen.add(sig)
        box = QMessageBox(QMessageBox.Warning, APP_NAME, "Something went wrong, but your music is safe and the "
                          "app is still running.\n\n" + engine.explain_error(exc), parent=win)
        b_rep = box.addButton("Report it…", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is b_rep:
            win.report_problem()

    def hook(exc_type, exc, tb):
        try:
            sys.__excepthook__(exc_type, exc, tb)
            if threading.current_thread() is threading.main_thread():
                show(exc_type, exc, tb)
            else:
                exc = exc if exc is not None else exc_type()
                exc.__traceback__ = tb
                engine.log_error("Unexpected error (background)", exc)
        except Exception:
            pass

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    settings = engine.load_settings()
    apply_appearance(app, settings.get("appearance", "system"))
    try:  # follow Windows when it switches between light and dark
        app.styleHints().colorSchemeChanged.connect(
            lambda _s: engine.load_settings().get("appearance", "system") == "system"
            and apply_appearance(app, "system"))
    except Exception:
        pass
    app.setStyleSheet(STYLE)

    # only one copy at a time: two would both write the same library and copy index
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(engine.DATA_DIR / "solfege.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already open. Look for its window on the taskbar.")
        sys.exit(0)

    win = MainWindow()
    win.lock = lock
    install_error_handler(win)
    win.show()
    code = app.exec()
    lock.unlock()
    sys.exit(code)


if __name__ == "__main__":
    main()
