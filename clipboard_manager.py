# -*- coding: utf-8 -*-
"""
클립보드 관리 프로그램 (Python / PySide6 / sqlite3)

기능
  - 클립보드(텍스트/이미지) 변경을 이벤트로 감지해서 자동으로 히스토리에 저장
  - 이미지는 목록에 썸네일로 표시되고, 복사하면 다시 클립보드에 이미지로 들어감
  - 선택한 항목에 2단계 카테고리(대분류/소분류)와 키값 지정
  - 검색: 일반 검색은 내용+카테고리+키 전체 대상
      /k 검색어  →  키값만 검색
      /c 검색어  →  카테고리만 검색
    (Qt라서 한글 조합 중인 글자까지 실시간으로 검색에 반영됨)
  - 전역 단축키 (옵션에서 변경 가능)
      Alt+V  →  미니 UI 팝업: 텍스트 캐럿 옆(없으면 마우스 옆)에 표시
      Alt+C  →  현재 프로그램의 선택 텍스트를 복사해서 히스토리에 수집
  - 트레이 아이콘 클릭  →  전체 UI 열기/숨기기
  - [옵션]: 로그인 시 자동 시작, 단축키 변경

실행:  python clipboard_manager.py   (pythonw 로 실행하면 콘솔 없음)
데이터: 같은 폴더의 clipboard.db (SQLite, 설정 포함)
필요 패키지: PySide6
"""

import hashlib
import os
import sqlite3
import sys
import time
from datetime import datetime

from PySide6.QtCore import (QAbstractNativeEventFilter, QBuffer, QIODevice,
                            QPoint, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QGuiApplication, QIcon,
                           QImage, QKeySequence, QPainter, QPen, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QMessageBox,
                               QPushButton, QSystemTrayIcon, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    import winreg
    from ctypes import wintypes

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clipboard.db")
PREVIEW_LEN = 80            # 목록에 보여줄 내용 길이
SEARCH_DEBOUNCE_MS = 150    # 검색 입력 후 목록 갱신까지 대기
THUMB_W, THUMB_H = 56, 34   # 썸네일 크기

DEFAULT_HOTKEY_SHOW = "alt+v"   # 미니 UI 열기
DEFAULT_HOTKEY_COPY = "alt+c"   # 활성 창에서 복사(수집)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClipboardManager"

WM_HOTKEY = 0x0312
HOTKEY_ID_SHOW, HOTKEY_ID_COPY = 1, 2
HOTKEY_MODS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}


# ==================== Win32 도우미 ====================

if IS_WINDOWS:
    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]


def parse_hotkey(s):
    """'alt+v' 형식을 (modifier flags, virtual key code)로 변환. 실패 시 vk=None"""
    if not IS_WINDOWS:
        return 0, None
    mods, vk = 0, None
    for part in s.lower().split("+"):
        part = part.strip()
        if part in HOTKEY_MODS:
            mods |= HOTKEY_MODS[part]
        elif len(part) == 1:
            vk = ctypes.windll.user32.VkKeyScanW(ord(part)) & 0xFF
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x70 + int(part[1:]) - 1   # F1 = 0x70
    return mods, vk


def get_foreground_caret_pos():
    """포그라운드 창의 텍스트 캐럿 화면 좌표. 없거나 알 수 없으면 None.
    (브라우저 등 자체 캐럿을 그리는 프로그램은 None이 나올 수 있음)"""
    if not IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    tid = user32.GetWindowThreadProcessId(fg, None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return None
    if not info.hwndCaret:
        return None
    r = info.rcCaret
    if r.left == 0 and r.top == 0 and r.right == 0 and r.bottom == 0:
        return None
    pt = wintypes.POINT(r.left, r.bottom)
    if not user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt)):
        return None
    return pt.x, pt.y


def send_ctrl_c_to_foreground():
    """활성 창에 Ctrl+C 를 보내 선택 텍스트를 복사시킨다.
    단축키의 Alt 가 아직 눌려 있으므로 먼저 논리적으로 해제한다."""
    if not IS_WINDOWS:
        return
    user32 = ctypes.windll.user32
    KEYUP = 0x0002
    VK_CONTROL, VK_MENU, VK_SHIFT, VK_C = 0x11, 0x12, 0x10, 0x43
    for vk in (VK_MENU, VK_CONTROL, VK_SHIFT):
        user32.keybd_event(vk, 0, KEYUP, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(VK_C, 0, KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)


# ==================== 시작 프로그램(자동 실행) ====================

def autostart_command():
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enable):
    if not IS_WINDOWS:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass


# ==================== DB ====================

class ClipDB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS clips (
                   id        INTEGER PRIMARY KEY AUTOINCREMENT,
                   kind      TEXT DEFAULT 'text',
                   text      TEXT NOT NULL,
                   image     BLOB,
                   hash      TEXT DEFAULT '',
                   category1 TEXT DEFAULT '',
                   category2 TEXT DEFAULT '',
                   item_key  TEXT DEFAULT '',
                   created   TEXT NOT NULL
               )"""
        )
        # 예전 버전 DB 업그레이드 (텍스트 전용 시절)
        for ddl in ("ALTER TABLE clips ADD COLUMN kind TEXT DEFAULT 'text'",
                    "ALTER TABLE clips ADD COLUMN image BLOB",
                    "ALTER TABLE clips ADD COLUMN hash TEXT DEFAULT ''"):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_key ON clips(item_key)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_cat ON clips(category1, category2)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.conn.commit()

    def get_setting(self, key, default=""):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def add(self, text):
        row = self.conn.execute("SELECT kind, text FROM clips ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[0] == "text" and row[1] == text:
            return None
        cur = self.conn.execute(
            "INSERT INTO clips(kind, text, created) VALUES('text', ?, ?)",
            (text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()
        return cur.lastrowid

    def add_image(self, png_bytes, width, height):
        img_hash = hashlib.md5(png_bytes).hexdigest()
        row = self.conn.execute("SELECT kind, hash FROM clips ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[0] == "image" and row[1] == img_hash:
            return None
        cur = self.conn.execute(
            "INSERT INTO clips(kind, text, image, hash, created) VALUES('image', ?, ?, ?, ?)",
            (f"[이미지 {width}x{height}]", png_bytes, img_hash,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()
        return cur.lastrowid

    def search(self, query):
        sql = ("SELECT id, kind, text, category1, category2, item_key, created "
               "FROM clips")
        params = []
        query = query.strip()
        if query.startswith("/k "):
            sql += " WHERE item_key LIKE ?"
            params.append(f"%{query[3:].strip()}%")
        elif query.startswith("/c "):
            sql += " WHERE (category1 LIKE ? OR category2 LIKE ?)"
            term = f"%{query[3:].strip()}%"
            params += [term, term]
        elif query:
            sql += (" WHERE (text LIKE ? OR category1 LIKE ? "
                    "OR category2 LIKE ? OR item_key LIKE ?)")
            params += [f"%{query}%"] * 4
        sql += " ORDER BY id DESC"
        return self.conn.execute(sql, params).fetchall()

    def get_clip(self, clip_id):
        row = self.conn.execute(
            "SELECT kind, text, image FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        return row if row else (None, None, None)

    def get_image(self, clip_id):
        row = self.conn.execute("SELECT image FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return row[0] if row else None

    def get_category_key(self, clip_id):
        row = self.conn.execute(
            "SELECT category1, category2, item_key FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        return row if row else ("", "", "")

    def set_category_key(self, clip_ids, cat1, cat2, key):
        self.conn.executemany(
            "UPDATE clips SET category1 = ?, category2 = ?, item_key = ? WHERE id = ?",
            [(cat1, cat2, key, cid) for cid in clip_ids],
        )
        self.conn.commit()

    def delete(self, clip_ids):
        self.conn.executemany("DELETE FROM clips WHERE id = ?", [(cid,) for cid in clip_ids])
        self.conn.commit()

    def categories1(self):
        rows = self.conn.execute(
            "SELECT DISTINCT category1 FROM clips WHERE category1 <> '' ORDER BY category1"
        ).fetchall()
        return [r[0] for r in rows]

    def categories2(self, cat1):
        rows = self.conn.execute(
            "SELECT DISTINCT category2 FROM clips "
            "WHERE category1 = ? AND category2 <> '' ORDER BY category2",
            (cat1,),
        ).fetchall()
        return [r[0] for r in rows]


# ==================== 공용 위젯/도우미 ====================

def make_preview(text):
    preview = " ".join(text.split())
    if len(preview) > PREVIEW_LEN:
        preview = preview[:PREVIEW_LEN] + "…"
    return preview


class SearchEdit(QLineEdit):
    """한글 조합(preedit) 중인 글자까지 포함해서 검색어를 알려주는 입력창"""
    searchChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preedit = ""
        self.textChanged.connect(lambda _t: self.searchChanged.emit(self.search_text()))

    def inputMethodEvent(self, event):
        super().inputMethodEvent(event)
        self._preedit = event.preeditString()
        self.searchChanged.emit(self.search_text())

    def search_text(self):
        t = self.text()
        if self._preedit:
            pos = self.cursorPosition()
            t = t[:pos] + self._preedit + t[pos:]
        return t


class ClipTree(QTreeWidget):
    """썸네일 아이콘을 지원하는 클립 목록"""

    def __init__(self, headers, widths, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(headers))
        self.setHeaderLabels(headers)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(THUMB_W, THUMB_H))
        self.setAllColumnsShowFocus(True)
        for i, w in enumerate(widths):
            self.setColumnWidth(i, w)

    def selected_ids(self):
        return [it.data(0, Qt.UserRole) for it in self.selectedItems()]

    def first_id(self):
        sel = self.selected_ids()
        if sel:
            return sel[0]
        if self.topLevelItemCount() > 0:
            return self.topLevelItem(0).data(0, Qt.UserRole)
        return None


def make_thumb_icon(png_bytes):
    """모든 썸네일이 같은 크기가 되도록 여백을 채우고 테두리를 그린 아이콘"""
    img = QImage.fromData(png_bytes)
    if img.isNull():
        return None
    canvas = QPixmap(THUMB_W, THUMB_H)
    canvas.fill(QColor(246, 246, 246))
    scaled = QPixmap.fromImage(img).scaled(
        THUMB_W - 4, THUMB_H - 4, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    p = QPainter(canvas)
    p.drawPixmap((THUMB_W - scaled.width()) // 2, (THUMB_H - scaled.height()) // 2, scaled)
    p.setPen(QPen(QColor(170, 170, 170)))
    p.drawRect(0, 0, THUMB_W - 1, THUMB_H - 1)
    p.end()
    return QIcon(canvas)


def qimage_to_png(img):
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def tray_icon_pixmap():
    """클립보드 모양 트레이 아이콘을 그려서 생성"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(52, 120, 212))
    p.drawRoundedRect(10, 8, 44, 52, 6, 6)
    p.setBrush(QColor(150, 190, 240))
    p.drawRoundedRect(22, 2, 20, 12, 4, 4)
    p.setBrush(QColor(255, 255, 255))
    p.drawRect(18, 24, 28, 4)
    p.drawRect(18, 34, 28, 4)
    p.drawRect(18, 44, 20, 4)
    p.end()
    return pm


# ==================== 다이얼로그 ====================

class CategoryDialog(QDialog):
    """2단계 카테고리 + 키값 입력"""

    def __init__(self, parent, db, cat1="", cat2="", key=""):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("카테고리/키 지정")

        self.cb1 = QComboBox(editable=True)
        self.cb1.addItems(db.categories1())
        self.cb1.setCurrentText(cat1)
        self.cb1.editTextChanged.connect(self._refresh_cat2)

        self.cb2 = QComboBox(editable=True)
        self.cb2.addItems(db.categories2(cat1))
        self.cb2.setCurrentText(cat2)

        self.key_edit = QLineEdit(key)

        form = QFormLayout()
        form.addRow("대분류 (1단계)", self.cb1)
        form.addRow("소분류 (2단계)", self.cb2)
        form.addRow("키값", self.key_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(360, self.sizeHint().height())

    def _refresh_cat2(self, text):
        current = self.cb2.currentText()
        self.cb2.clear()
        self.cb2.addItems(self.db.categories2(text.strip()))
        self.cb2.setCurrentText(current)

    def _accept(self):
        if not self.cb1.currentText().strip() and self.cb2.currentText().strip():
            QMessageBox.information(self, "클립보드 관리",
                                    "소분류를 지정하려면 대분류를 먼저 입력하세요.")
            return
        self.accept()

    def values(self):
        return (self.cb1.currentText().strip(),
                self.cb2.currentText().strip(),
                self.key_edit.text().strip())


class SettingsDialog(QDialog):
    """옵션: 자동 시작 + 전역 단축키"""

    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("옵션")

        self.chk_autostart = QCheckBox("로그인 시 자동 시작 (시작 프로그램 등록)")
        self.chk_autostart.setChecked(autostart_enabled())
        self.chk_autostart.setEnabled(IS_WINDOWS)

        self.show_edit = QLineEdit(db.get_setting("hotkey_show", DEFAULT_HOTKEY_SHOW))
        self.copy_edit = QLineEdit(db.get_setting("hotkey_copy", DEFAULT_HOTKEY_COPY))

        form = QFormLayout()
        form.addRow(self.chk_autostart)
        form.addRow("미니 UI 열기 단축키", self.show_edit)
        form.addRow("복사(수집) 단축키", self.copy_edit)
        hint = QLabel("형식: ctrl/alt/shift/win + 문자 또는 F1~F12  (예: alt+v)\n"
                      "비워두면 해당 단축키를 사용하지 않습니다.")
        hint.setStyleSheet("color: gray")
        form.addRow(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(380, self.sizeHint().height())

    def _save(self):
        show = self.show_edit.text().strip().lower()
        copy = self.copy_edit.text().strip().lower()
        for name, value in (("미니 UI 열기", show), ("복사(수집)", copy)):
            if value and parse_hotkey(value)[1] is None:
                QMessageBox.warning(self, "옵션", f"{name} 단축키 형식이 잘못되었습니다: {value}")
                return
        if show and show == copy:
            QMessageBox.warning(self, "옵션", "두 단축키가 같을 수 없습니다.")
            return
        self.db.set_setting("hotkey_show", show)
        self.db.set_setting("hotkey_copy", copy)
        try:
            set_autostart(self.chk_autostart.isChecked())
        except OSError as e:
            QMessageBox.warning(self, "옵션", f"시작 프로그램 등록 실패: {e}")
        self.accept()


# ==================== 미니 UI ====================

class MiniWindow(QWidget):
    """단축키로 띄우는 소형 팝업 — 검색해서 바로 복사"""

    WIDTH, HEIGHT = 480, 340
    MAX_ROWS = 50

    def __init__(self, manager):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.manager = manager
        self.setWindowTitle("클립보드 검색")

        self.search = SearchEdit()
        self.search.setPlaceholderText("검색  (/k 키검색, /c 카테고리검색)")
        self._debounce = QTimer(self, singleShot=True, interval=SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.refresh)
        self.search.searchChanged.connect(lambda _t: self._debounce.start())
        self.search.returnPressed.connect(self._copy)

        btn_full = QPushButton("전체 UI")
        btn_full.clicked.connect(self._open_full)

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(btn_full)

        self.tree = ClipTree(["", "내용", "키값"], [THUMB_W + 12, 290, 100])
        self.tree.itemDoubleClicked.connect(lambda *_: self._copy())

        hint = QLabel("Enter/더블클릭: 복사하고 닫기    Esc: 닫기")
        hint.setStyleSheet("color: gray")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 6)
        lay.addLayout(top)
        lay.addWidget(self.tree, 1)
        lay.addWidget(hint)

        self.setStyleSheet("MiniWindow { border: 1px solid #999; }")
        self.resize(self.WIDTH, self.HEIGHT)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def toggle(self, anchor=None):
        if self.isVisible():
            self.hide()
        else:
            self.show_at(anchor)

    def show_at(self, anchor=None):
        self.refresh()
        if anchor is not None:
            x, y = anchor[0] + 8, anchor[1] + 8
        else:
            pos = QGuiApplication.primaryScreen().availableGeometry().center()
            from PySide6.QtGui import QCursor
            pos = QCursor.pos()
            x, y = pos.x(), pos.y()
        screen = QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = max(geo.left(), min(x, geo.right() - self.WIDTH - 10))
        y = max(geo.top(), min(y, geo.bottom() - self.HEIGHT - 10))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        self.search.selectAll()

    def refresh(self):
        rows = self.manager.db.search(self.search.search_text())[:self.MAX_ROWS]
        self.tree.clear()
        for cid, kind, text, c1, c2, key, created in rows:
            item = QTreeWidgetItem(["", make_preview(text), key])
            item.setData(0, Qt.UserRole, cid)
            if kind == "image":
                icon = make_thumb_icon(self.manager.db.get_image(cid))
                if icon:
                    item.setIcon(0, icon)
            self.tree.addTopLevelItem(item)

    def _copy(self):
        cid = self.tree.first_id()
        if cid is not None:
            self.manager.copy_clip(cid)
        self.hide()

    def _open_full(self):
        self.hide()
        self.manager.show_full()


# ==================== 전체 UI ====================

class MainWindow(QWidget):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.setWindowTitle("클립보드 관리")
        self.setWindowIcon(QIcon(tray_icon_pixmap()))
        self.resize(900, 540)

        self.search = SearchEdit()
        self.search.setPlaceholderText("검색  (/k 키검색, /c 카테고리검색)")
        self._debounce = QTimer(self, singleShot=True, interval=SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.refresh)
        self.search.searchChanged.connect(lambda _t: self._debounce.start())

        self.hint = QLabel()
        self.hint.setStyleSheet("color: gray")

        top = QHBoxLayout()
        top.addWidget(QLabel("검색"))
        top.addWidget(self.search, 1)
        top.addWidget(self.hint)

        self.tree = ClipTree(["", "내용", "대분류", "소분류", "키값", "저장시간"],
                             [THUMB_W + 12, 340, 100, 100, 110, 140])
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(lambda *_: self.copy_selected())
        QShortcut(QKeySequence(Qt.Key_Delete), self.tree, self.delete_selected)

        btn_copy = QPushButton("복사")
        btn_copy.clicked.connect(self.copy_selected)
        btn_cat = QPushButton("카테고리/키 지정")
        btn_cat.clicked.connect(self.set_category)
        btn_del = QPushButton("삭제")
        btn_del.clicked.connect(self.delete_selected)
        btn_opt = QPushButton("옵션")
        btn_opt.clicked.connect(self.open_settings)
        self.status = QLabel()

        bottom = QHBoxLayout()
        for b in (btn_copy, btn_cat, btn_del, btn_opt):
            bottom.addWidget(b)
        bottom.addStretch(1)
        bottom.addWidget(self.status)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.tree, 1)
        lay.addLayout(bottom)

        self.update_hint()

    def closeEvent(self, event):
        # 트레이가 있으면 종료 대신 숨김
        if self.manager.tray is not None:
            event.ignore()
            self.hide()
        else:
            event.accept()

    def update_hint(self):
        show = self.manager.db.get_setting("hotkey_show", DEFAULT_HOTKEY_SHOW)
        copy = self.manager.db.get_setting("hotkey_copy", DEFAULT_HOTKEY_COPY)
        parts = []
        if show:
            parts.append(f"미니 UI: {show.upper()}")
        if copy:
            parts.append(f"복사수집: {copy.upper()}")
        self.hint.setText("   ".join(parts))

    def refresh(self):
        rows = self.manager.db.search(self.search.search_text())
        self.tree.clear()
        for cid, kind, text, c1, c2, key, created in rows:
            item = QTreeWidgetItem(["", make_preview(text), c1, c2, key, created])
            item.setData(0, Qt.UserRole, cid)
            if kind == "image":
                icon = make_thumb_icon(self.manager.db.get_image(cid))
                if icon:
                    item.setIcon(0, icon)
            self.tree.addTopLevelItem(item)
        self.status.setText(f"{len(rows)}개 항목")

    def copy_selected(self):
        ids = self.tree.selected_ids()
        if ids and self.manager.copy_clip(ids[0]):
            self.status.setText("클립보드에 복사됨")

    def set_category(self):
        ids = self.tree.selected_ids()
        if not ids:
            QMessageBox.information(self, "클립보드 관리", "항목을 먼저 선택하세요.")
            return
        cat1, cat2, key = self.manager.db.get_category_key(ids[0])
        if len(ids) > 1:
            key = ""   # 키값은 항목별 고유값이므로 다중 선택 시 미리 채우지 않음
        dlg = CategoryDialog(self, self.manager.db, cat1, cat2, key)
        if dlg.exec() == QDialog.Accepted:
            self.manager.db.set_category_key(ids, *dlg.values())
            self.manager.refresh_all()

    def delete_selected(self):
        ids = self.tree.selected_ids()
        if not ids:
            return
        ret = QMessageBox.question(self, "클립보드 관리", f"{len(ids)}개 항목을 삭제할까요?")
        if ret == QMessageBox.Yes:
            self.manager.db.delete(ids)
            self.manager.refresh_all()

    def open_settings(self):
        dlg = SettingsDialog(self, self.manager.db)
        if dlg.exec() == QDialog.Accepted:
            self.manager.register_hotkeys()
            self.update_hint()


# ==================== 전역 단축키 (RegisterHotKey + Qt 네이티브 이벤트 필터) ====================

class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def nativeEventFilter(self, event_type, message):
        if IS_WINDOWS and event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                if msg.wParam == HOTKEY_ID_SHOW:
                    self.manager.on_hotkey_show()
                elif msg.wParam == HOTKEY_ID_COPY:
                    self.manager.on_hotkey_copy()
                return True, 0
        return False, 0


# ==================== 매니저 (전체 조립) ====================

class Manager:
    def __init__(self, app):
        self.app = app
        self.db = ClipDB(DB_PATH)
        self._suppress_capture = False
        self.registered_hotkeys = []

        self.main = MainWindow(self)
        self.mini = MiniWindow(self)
        self.main.refresh()

        # ---- 클립보드 감시 (폴링 없이 이벤트) ----
        self.clipboard = QApplication.clipboard()
        # dataChanged 직후엔 클립보드가 잠겨 있을 수 있어 잠깐 뒤에 읽는다
        self._capture_timer = QTimer(self.main, singleShot=True, interval=120)
        self._capture_timer.timeout.connect(self.capture_clipboard)
        self.clipboard.dataChanged.connect(self._capture_timer.start)

        # ---- 트레이 ----
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon(tray_icon_pixmap()), self.app)
            self.tray.setToolTip("클립보드 관리")
            menu = QMenu()
            act_open = QAction("열기/숨기기 (전체 UI)", menu)
            act_open.triggered.connect(self.toggle_full)
            act_quit = QAction("종료", menu)
            act_quit.triggered.connect(self.quit)
            menu.addAction(act_open)
            menu.addSeparator()
            menu.addAction(act_quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(
                lambda reason: self.toggle_full()
                if reason == QSystemTrayIcon.Trigger else None)
            self.tray.show()

        # ---- 전역 단축키 ----
        self._hotkey_filter = HotkeyFilter(self)
        self.app.installNativeEventFilter(self._hotkey_filter)
        self.register_hotkeys()

    # ---------- 단축키 ----------
    def register_hotkeys(self):
        if not IS_WINDOWS:
            return
        user32 = ctypes.windll.user32
        for hk_id in self.registered_hotkeys:
            user32.UnregisterHotKey(None, hk_id)
        self.registered_hotkeys = []
        pairs = ((HOTKEY_ID_SHOW, self.db.get_setting("hotkey_show", DEFAULT_HOTKEY_SHOW)),
                 (HOTKEY_ID_COPY, self.db.get_setting("hotkey_copy", DEFAULT_HOTKEY_COPY)))
        for hk_id, key in pairs:
            if not key:
                continue
            mods, vk = parse_hotkey(key)
            if vk and user32.RegisterHotKey(None, hk_id, mods, vk):
                self.registered_hotkeys.append(hk_id)
            # 실패(다른 프로그램이 사용 중 등)해도 나머지 기능은 정상 동작

    def on_hotkey_show(self):
        # 미니 UI가 뜨기 전, 상대 앱이 아직 포그라운드일 때 캐럿 위치를 읽는다
        anchor = get_foreground_caret_pos()
        self.mini.toggle(anchor)

    def on_hotkey_copy(self):
        send_ctrl_c_to_foreground()

    # ---------- 클립보드 ----------
    def capture_clipboard(self):
        if self._suppress_capture:
            self._suppress_capture = False
            return
        md = self.clipboard.mimeData()
        if md is None:
            return
        added = None
        if md.hasText() and md.text():
            added = self.db.add(md.text())
        elif md.hasImage():
            img = self.clipboard.image()
            if not img.isNull():
                png = qimage_to_png(img)
                added = self.db.add_image(png, img.width(), img.height())
        if added is not None:
            self.refresh_all()

    def copy_clip(self, clip_id):
        kind, text, image = self.db.get_clip(clip_id)
        if kind is None:
            return False
        self._suppress_capture = True
        if kind == "image" and image is not None:
            self.clipboard.setImage(QImage.fromData(image))
        else:
            self.clipboard.setText(text)
        return True

    # ---------- UI ----------
    def refresh_all(self):
        self.main.refresh()
        if self.mini.isVisible():
            self.mini.refresh()

    def show_full(self):
        self.main.show()
        self.main.raise_()
        self.main.activateWindow()

    def toggle_full(self):
        if self.main.isVisible():
            self.main.hide()
        else:
            self.show_full()

    def quit(self):
        if IS_WINDOWS:
            user32 = ctypes.windll.user32
            for hk_id in self.registered_hotkeys:
                user32.UnregisterHotKey(None, hk_id)
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    manager = Manager(app)
    manager.show_full()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
