# -*- coding: utf-8 -*-
"""
클립보드 관리 프로그램 (Python / tkinter / sqlite3 — 표준 라이브러리만 사용)

기능
  - 클립보드(텍스트/이미지) 변경을 감지해서 자동으로 히스토리에 저장
  - 이미지는 목록에 썸네일로 표시되고, 복사하면 다시 클립보드에 이미지로 들어감
  - 선택한 항목에 2단계 카테고리(대분류/소분류)와 키값 지정
  - 검색: 일반 검색은 내용+카테고리+키 전체 대상
      /k 검색어  →  키값만 검색
      /c 검색어  →  카테고리만 검색
  - 더블클릭 또는 [복사] 버튼으로 항목을 다시 클립보드에 복사
  - 창을 닫으면 트레이로 숨고, 트레이 아이콘으로 열기/종료
  - 전역 단축키 (옵션에서 변경 가능)
      Alt+V  →  어디서든 미니 UI 팝업 (검색해서 바로 복사)
      Alt+C  →  현재 프로그램의 선택 텍스트를 복사해서 히스토리에 수집
  - 트레이 아이콘 클릭  →  전체 UI 열기/숨기기
  - [옵션]: 로그인 시 자동 시작, 단축키 변경

실행:  python clipboard_manager.py
데이터: 같은 폴더의 clipboard.db (SQLite, 설정 포함)
선택 패키지: pystray + pillow (트레이 아이콘) — 없으면 일반 창 모드로 동작
전역 단축키는 Win32 RegisterHotKey(ctypes)라서 추가 패키지가 필요 없음
"""

import hashlib
import io
import os
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageGrab, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pystray
    HAS_TRAY = HAS_PIL   # pystray 아이콘 이미지에 PIL 필요
except ImportError:
    HAS_TRAY = False

IS_WINDOWS = sys.platform == "win32"
# 전역 단축키는 Win32 RegisterHotKey 사용 (표준 라이브러리 ctypes만 필요)
HAS_HOTKEY = IS_WINDOWS
if IS_WINDOWS:
    import ctypes
    import winreg
    from ctypes import wintypes

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clipboard.db")
POLL_MS = 500          # 클립보드 확인 주기 (ms)
FLAG_POLL_MS = 100     # 트레이/단축키 요청 확인 주기 (ms)
PREVIEW_LEN = 80       # 목록에 보여줄 내용 길이
SEARCH_DEBOUNCE_MS = 250   # 검색 입력 후 목록 갱신까지 대기 (한글 조합 중 버벅임 방지)

DEFAULT_HOTKEY_SHOW = "alt+v"   # 창 열기/숨기기
DEFAULT_HOTKEY_COPY = "alt+c"   # 활성 창에서 복사(수집)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClipboardManager"


if IS_WINDOWS:
    class COMPOSITIONFORM(ctypes.Structure):
        _fields_ = [("dwStyle", wintypes.DWORD),
                    ("ptCurrentPos", wintypes.POINT),
                    ("rcArea", wintypes.RECT)]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", wintypes.RECT)]


def attach_ime_fix(widget):
    """Windows Tk는 팝업/툴윈도우에서 한글 IME 조합창 좌표를 제대로 못 잡아
    조합 글자가 창 왼쪽 위에 뜨는 버그가 있다. 포커스를 받거나 키를 누를 때마다
    IMM32 API로 조합창을 입력 캐럿 위치로 직접 옮겨서 해결한다."""
    if not IS_WINDOWS:
        return

    def fix(_event=None):
        try:
            user32 = ctypes.windll.user32
            imm32 = ctypes.windll.imm32
            imm32.ImmGetContext.restype = ctypes.c_void_p
            imm32.ImmGetContext.argtypes = [wintypes.HWND]
            imm32.ImmReleaseContext.argtypes = [wintypes.HWND, ctypes.c_void_p]
            imm32.ImmSetCompositionWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

            hwnd = user32.GetFocus()   # 실제 Windows 포커스(=IME가 붙은) 창
            if not hwnd:
                return
            # 캐럿의 화면 좌표 → 포커스 창의 클라이언트 좌표
            try:
                bx, by, bw, bh = widget.bbox("insert")
            except Exception:
                bx, by = 2, 2
            pt = wintypes.POINT(widget.winfo_rootx() + bx,
                                widget.winfo_rooty() + by)
            user32.ScreenToClient(hwnd, ctypes.byref(pt))

            himc = imm32.ImmGetContext(hwnd)
            if not himc:
                return
            CFS_POINT = 0x0002
            cf = COMPOSITIONFORM()
            cf.dwStyle = CFS_POINT
            cf.ptCurrentPos = pt
            imm32.ImmSetCompositionWindow(himc, ctypes.byref(cf))
            imm32.ImmReleaseContext(hwnd, himc)
        except Exception:
            pass  # IME 보정 실패가 입력 자체를 막으면 안 됨

    widget.bind("<FocusIn>", fix, add="+")
    widget.bind("<KeyPress>", fix, add="+")
    widget.bind("<KeyRelease>", fix, add="+")
    widget.bind("<ButtonRelease-1>", fix, add="+")


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


# ---------- 시작 프로그램(로그인 시 자동 실행) 등록 ----------
def autostart_command():
    """콘솔 창 없이 실행되도록 pythonw.exe 를 우선 사용"""
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
                pass  # 이미 있는 컬럼
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
        # 직전 항목과 같으면 저장하지 않음
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

    def get_text(self, clip_id):
        row = self.conn.execute("SELECT text FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return row[0] if row else None

    def get_clip(self, clip_id):
        """(kind, text, image_bytes) 반환"""
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


class CategoryDialog(tk.Toplevel):
    """2단계 카테고리 + 키값 입력 다이얼로그"""

    def __init__(self, parent, db, cat1="", cat2="", key=""):
        super().__init__(parent)
        self.db = db
        self.result = None

        self.title("카테고리/키 지정")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="대분류 (1단계)").grid(row=0, column=0, sticky="w")
        self.cb1 = ttk.Combobox(frame, width=38, values=db.categories1())
        self.cb1.set(cat1)
        self.cb1.grid(row=1, column=0, pady=(2, 8))
        self.cb1.bind("<<ComboboxSelected>>", self._refresh_cat2)
        self.cb1.bind("<KeyRelease>", self._refresh_cat2)

        ttk.Label(frame, text="소분류 (2단계)").grid(row=2, column=0, sticky="w")
        self.cb2 = ttk.Combobox(frame, width=38, values=db.categories2(cat1))
        self.cb2.set(cat2)
        self.cb2.grid(row=3, column=0, pady=(2, 8))

        ttk.Label(frame, text="키값").grid(row=4, column=0, sticky="w")
        self.key_entry = ttk.Entry(frame, width=41)
        self.key_entry.insert(0, key)
        self.key_entry.grid(row=5, column=0, pady=(2, 12))

        btns = ttk.Frame(frame)
        btns.grid(row=6, column=0, sticky="e")
        ttk.Button(btns, text="확인", command=self._ok).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="취소", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.cb1.focus_set()

        # 부모 창 중앙에 배치
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _refresh_cat2(self, _event=None):
        self.cb2["values"] = self.db.categories2(self.cb1.get().strip())

    def _ok(self):
        cat1 = self.cb1.get().strip()
        cat2 = self.cb2.get().strip()
        key = self.key_entry.get().strip()
        if cat2 and not cat1:
            messagebox.showinfo("클립보드 관리", "소분류를 지정하려면 대분류를 먼저 입력하세요.",
                                parent=self)
            return
        self.result = (cat1, cat2, key)
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """옵션: 자동 시작 + 전역 단축키 설정"""

    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.result = False

        self.title("옵션")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        chk = ttk.Checkbutton(frame, text="로그인 시 자동 시작 (시작 프로그램 등록)",
                              variable=self.autostart_var)
        chk.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        if not IS_WINDOWS:
            chk.state(["disabled"])

        ttk.Label(frame, text="창 열기/숨기기 단축키").grid(row=1, column=0, sticky="w")
        self.show_entry = ttk.Entry(frame, width=20)
        self.show_entry.insert(0, db.get_setting("hotkey_show", DEFAULT_HOTKEY_SHOW))
        self.show_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(frame, text="복사(수집) 단축키").grid(row=2, column=0, sticky="w")
        self.copy_entry = ttk.Entry(frame, width=20)
        self.copy_entry.insert(0, db.get_setting("hotkey_copy", DEFAULT_HOTKEY_COPY))
        self.copy_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(frame, foreground="gray",
                  text="형식: ctrl/alt/shift/win + 문자 또는 F1~F12  (예: alt+v, ctrl+shift+f9)\n"
                       "비워두면 해당 단축키를 사용하지 않습니다.").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 12))

        btns = ttk.Frame(frame)
        btns.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="저장", command=self._save).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="취소", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())

        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _save(self):
        show = self.show_entry.get().strip().lower()
        copy = self.copy_entry.get().strip().lower()

        for name, value in (("창 열기/숨기기", show), ("복사(수집)", copy)):
            if value:
                mods, vk = ClipboardManager._parse_hotkey(value)
                if not vk:
                    messagebox.showwarning(
                        "옵션", f"{name} 단축키 형식이 잘못되었습니다: {value}", parent=self)
                    return
        if show and show == copy:
            messagebox.showwarning("옵션", "두 단축키가 같을 수 없습니다.", parent=self)
            return

        self.db.set_setting("hotkey_show", show)
        self.db.set_setting("hotkey_copy", copy)
        try:
            set_autostart(self.autostart_var.get())
        except OSError as e:
            messagebox.showwarning("옵션", f"시작 프로그램 등록 실패: {e}", parent=self)

        self.result = True
        self.destroy()


class ClipboardManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("클립보드 관리")
        self.geometry("860x520")
        self.db = ClipDB(DB_PATH)
        self._last_clip = None        # 마지막으로 본 텍스트 (자기 복사 무시용)
        self._last_image_hash = None  # 마지막으로 본 이미지 해시
        self._last_seq = None         # 클립보드 시퀀스 번호 (변경 감지)

        # 트레이/단축키 스레드에서 오는 요청 (tkinter는 메인 스레드에서만 조작해야 함)
        self._mini_toggle_request = threading.Event()   # 단축키 → 미니 UI
        self._mini_anchor = None                        # 미니 UI를 띄울 좌표 (캐럿 위치)
        self._full_toggle_request = threading.Event()   # 트레이 → 전체 UI
        self._quit_request = threading.Event()
        self._tray_icon = None

        self._build_ui()
        self.mini = MiniWindow(self)
        self._refresh()
        self._setup_tray()
        self._setup_hotkey()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_clip = self.after(POLL_MS, self._poll_clipboard)
        self._after_flags = self.after(FLAG_POLL_MS, self._poll_requests)

    # ---------- 트레이 아이콘 ----------
    def _setup_tray(self):
        if not HAS_TRAY:
            return
        menu = pystray.Menu(
            pystray.MenuItem("열기/숨기기 (전체 UI)", lambda: self._full_toggle_request.set(),
                             default=True),
            pystray.MenuItem("종료", lambda: self._quit_request.set()),
        )
        self._tray_icon = pystray.Icon("clipboard_manager", self._tray_image(),
                                       "클립보드 관리", menu)
        # run_detached()는 비데몬 스레드라 메인이 비정상 종료하면 프로세스가
        # 남는다 — 데몬 스레드로 직접 실행해서 항상 함께 종료되게 한다
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    @staticmethod
    def _tray_image():
        # 간단한 클립보드 모양 아이콘을 그려서 사용
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((10, 8, 54, 60), radius=6, fill=(52, 120, 212, 255))
        d.rounded_rectangle((22, 2, 42, 14), radius=4, fill=(150, 190, 240, 255))
        d.rectangle((18, 24, 46, 28), fill=(255, 255, 255, 255))
        d.rectangle((18, 34, 46, 38), fill=(255, 255, 255, 255))
        d.rectangle((18, 44, 38, 48), fill=(255, 255, 255, 255))
        return img

    # ---------- 전역 단축키 (Win32 RegisterHotKey) ----------
    _HOTKEY_MODS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
    _WM_HOTKEY = 0x0312
    _WM_QUIT = 0x0012

    @classmethod
    def _parse_hotkey(cls, s):
        """'alt+v' 형식을 (modifier flags, virtual key code)로 변환"""
        if not IS_WINDOWS:
            return 0, None
        mods, vk = 0, None
        for part in s.lower().split("+"):
            part = part.strip()
            if part in cls._HOTKEY_MODS:
                mods |= cls._HOTKEY_MODS[part]
            elif len(part) == 1:
                vk = ctypes.windll.user32.VkKeyScanW(ord(part)) & 0xFF
            elif part.startswith("f") and part[1:].isdigit():
                vk = 0x70 + int(part[1:]) - 1   # F1 = 0x70
        return mods, vk

    def _hotkeys_from_settings(self):
        return (
            self.db.get_setting("hotkey_show", DEFAULT_HOTKEY_SHOW),
            self.db.get_setting("hotkey_copy", DEFAULT_HOTKEY_COPY),
        )

    def _setup_hotkey(self):
        self._hotkey_thread_id = None
        if not HAS_HOTKEY:
            return
        # sqlite 연결은 스레드 간 공유가 안 되므로 설정은 메인 스레드에서 읽어 넘긴다
        show, copy = self._hotkeys_from_settings()
        threading.Thread(target=self._hotkey_listener, args=(show, copy), daemon=True).start()

    def _restart_hotkeys(self):
        if not HAS_HOTKEY:
            return
        if self._hotkey_thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._hotkey_thread_id, self._WM_QUIT, 0, 0)
            self._hotkey_thread_id = None
            time.sleep(0.1)  # 이전 스레드가 UnregisterHotKey 할 시간
        self._setup_hotkey()

    def _hotkey_listener(self, show_key, copy_key):
        user32 = ctypes.windll.user32
        registered = []
        for hk_id, key in ((1, show_key), (2, copy_key)):
            if not key:
                continue
            mods, vk = self._parse_hotkey(key)
            if vk and user32.RegisterHotKey(None, hk_id, mods, vk):
                registered.append(hk_id)
            # 실패(다른 프로그램이 사용 중 등)해도 나머지 기능은 정상 동작
        if not registered:
            return
        self._hotkey_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self._WM_HOTKEY:
                if msg.wParam == 1:
                    # 아직 상대 앱이 포그라운드일 때(창 뜨기 전) 캐럿 위치를 읽어둔다
                    self._mini_anchor = get_foreground_caret_pos()
                    self._mini_toggle_request.set()
                elif msg.wParam == 2:
                    self._send_copy()
        for hk_id in registered:
            user32.UnregisterHotKey(None, hk_id)

    @staticmethod
    def _send_copy():
        """활성 창에 Ctrl+C 를 보내 선택 텍스트를 복사시킨다 (수집은 폴러가 함).
        단축키의 Alt 가 아직 눌려 있으므로 먼저 논리적으로 해제한다."""
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

    # ---------- 창 표시/숨김/종료 ----------
    def _poll_requests(self):
        if self._quit_request.is_set():
            self._quit()
            return
        if self._full_toggle_request.is_set():
            self._full_toggle_request.clear()
            self._toggle_window()
        if self._mini_toggle_request.is_set():
            self._mini_toggle_request.clear()
            self.mini.toggle(self._mini_anchor)
            self._mini_anchor = None
        self._after_flags = self.after(FLAG_POLL_MS, self._poll_requests)

    def _toggle_window(self):
        if self.state() == "withdrawn":
            self._show_window()
        else:
            self.withdraw()

    def _show_window(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _on_close(self):
        # 트레이가 있으면 종료 대신 트레이로 숨김
        if self._tray_icon is not None:
            self.withdraw()
        else:
            self._quit()

    def _quit(self):
        self.after_cancel(self._after_clip)
        self.after_cancel(self._after_flags)
        if HAS_HOTKEY and self._hotkey_thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._hotkey_thread_id, self._WM_QUIT, 0, 0)
        if self._tray_icon is not None:
            self._tray_icon.stop()
        self.destroy()

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self, padding=(8, 8, 8, 4))
        top.pack(fill="x")

        ttk.Label(top, text="검색").pack(side="left")
        self.search_var = tk.StringVar()
        self._search_after = None
        self.search_var.trace_add("write", lambda *a: self._schedule_refresh())
        entry = ttk.Entry(top, textvariable=self.search_var)
        entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.hint_label = ttk.Label(top)
        self.hint_label.pack(side="left")
        self._update_hint()

        style = ttk.Style(self)
        style.configure("Clip.Treeview", rowheight=40, indent=2)  # 이미지 썸네일 높이 확보
        # 트리 항목의 펼침 인디케이터/들여쓰기 요소 제거 (평면 목록이라 불필요,
        # 남겨두면 썸네일 왼쪽에 빈 공간과 선이 생김)
        style.layout("Clip.Treeview.Item",
                     [("Treeitem.padding", {"sticky": "nswe", "children": [
                         ("Treeitem.image", {"side": "left", "sticky": ""}),
                         ("Treeitem.text", {"side": "left", "sticky": ""})]})])

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=8)

        cols = ("내용", "대분류", "소분류", "키값", "저장시간")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="tree headings",
                                 selectmode="extended", style="Clip.Treeview")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=68, stretch=False, anchor="center")  # 썸네일 컬럼
        widths = (340, 100, 100, 110, 130)
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="w")
        self._thumbs = {}   # PhotoImage 참조 유지 (GC 방지)

        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.bind("<Double-1>", lambda e: self._copy_selected())
        self.tree.bind("<Delete>", lambda e: self._delete_selected())

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="복사", command=self._copy_selected).pack(side="left")
        ttk.Button(bottom, text="카테고리/키 지정", command=self._set_category).pack(side="left", padx=6)
        ttk.Button(bottom, text="삭제", command=self._delete_selected).pack(side="left")
        ttk.Button(bottom, text="옵션", command=self._open_settings).pack(side="left", padx=6)
        self.status = ttk.Label(bottom, text="", anchor="e")
        self.status.pack(side="right")

    def _update_hint(self):
        hint = "(/k 키검색, /c 카테고리검색)"
        if HAS_HOTKEY:
            show, copy = self._hotkeys_from_settings()
            if show:
                hint += f"  열기: {show.upper()}"
            if copy:
                hint += f"  복사수집: {copy.upper()}"
        self.hint_label.config(text=hint)

    def _open_settings(self):
        dlg = SettingsDialog(self, self.db)
        self.wait_window(dlg)
        if dlg.result:
            self._restart_hotkeys()
            self._update_hint()

    # ---------- 클립보드 감시 ----------
    def _poll_clipboard(self):
        try:
            changed = True
            if IS_WINDOWS:
                # 시퀀스 번호가 그대로면 클립보드 내용을 읽지 않는다 (이미지 폴링 비용 절약)
                seq = ctypes.windll.user32.GetClipboardSequenceNumber()
                changed = seq != self._last_seq
                self._last_seq = seq
            if changed:
                self._capture_clipboard()
        finally:
            self._after_clip = self.after(POLL_MS, self._poll_clipboard)

    def _capture_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = None  # 텍스트가 아니거나 비어 있음

        if text:
            if text != self._last_clip:
                self._last_clip = text
                if self.db.add(text) is not None:
                    self._refresh_all()
            return

        if not HAS_PIL:
            return
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            img = None
        if isinstance(img, Image.Image):
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG")
            png = buf.getvalue()
            img_hash = hashlib.md5(png).hexdigest()
            if img_hash != self._last_image_hash:
                self._last_image_hash = img_hash
                if self.db.add_image(png, img.width, img.height) is not None:
                    self._refresh_all()

    def _mark_own_clipboard(self):
        """방금 우리가 클립보드에 넣은 내용은 다시 수집하지 않도록 표시"""
        if IS_WINDOWS:
            self.update_idletasks()
            self._last_seq = ctypes.windll.user32.GetClipboardSequenceNumber()

    # ---------- 클립보드에 이미지 넣기 (Win32 CF_DIB) ----------
    @staticmethod
    def _set_clipboard_image(png_bytes):
        if not (IS_WINDOWS and HAS_PIL):
            return False
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        data = buf.getvalue()[14:]  # BMP 파일 헤더(14바이트) 제거 → DIB

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        # 64비트에서 핸들/포인터가 int(32비트)로 잘리지 않도록 시그니처 지정
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

        CF_DIB, GMEM_MOVEABLE = 8, 0x0002
        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            return bool(user32.SetClipboardData(CF_DIB, handle))
        finally:
            user32.CloseClipboard()

    def copy_clip(self, clip_id):
        """항목을 클립보드로 복사 (텍스트/이미지 공용). 성공 여부 반환"""
        kind, text, image = self.db.get_clip(clip_id)
        if kind is None:
            return False
        if kind == "image" and image is not None:
            ok = self._set_clipboard_image(image)
            if ok:
                self._last_image_hash = hashlib.md5(image).hexdigest()
        else:
            self._last_clip = text
            self.clipboard_clear()
            self.clipboard_append(text)
            ok = True
        self._mark_own_clipboard()
        return ok

    # ---------- 동작 ----------
    def _schedule_refresh(self):
        """타이핑(특히 한글 조합) 중에는 갱신을 미루고, 입력이 멈추면 한 번만 갱신"""
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(SEARCH_DEBOUNCE_MS, self._debounced_refresh)

    def _debounced_refresh(self):
        self._search_after = None
        self._refresh()

    def make_thumb(self, clip_id, kind, size=(56, 34)):
        """이미지 항목의 트리뷰 썸네일 PhotoImage 생성 (텍스트면 None).
        모든 썸네일이 같은 크기가 되도록 여백을 채우고 테두리를 그린다."""
        if kind != "image" or not HAS_PIL:
            return None
        blob = self.db.get_image(clip_id)
        if not blob:
            return None
        try:
            img = Image.open(io.BytesIO(blob))
            img.thumbnail((size[0] - 4, size[1] - 4))
            canvas = Image.new("RGB", size, (246, 246, 246))
            canvas.paste(img, ((size[0] - img.width) // 2, (size[1] - img.height) // 2))
            ImageDraw.Draw(canvas).rectangle(
                [0, 0, size[0] - 1, size[1] - 1], outline=(170, 170, 170))
            return ImageTk.PhotoImage(canvas)
        except Exception:
            return None

    @staticmethod
    def make_preview(text):
        preview = " ".join(text.split())
        if len(preview) > PREVIEW_LEN:
            preview = preview[:PREVIEW_LEN] + "…"
        return preview

    def _refresh(self):
        rows = self.db.search(self.search_var.get())
        self.tree.delete(*self.tree.get_children())
        self._thumbs.clear()
        for cid, kind, text, c1, c2, key, created in rows:
            thumb = self.make_thumb(cid, kind)
            kwargs = {}
            if thumb is not None:
                self._thumbs[cid] = thumb
                kwargs["image"] = thumb
            self.tree.insert("", "end", iid=str(cid),
                             values=(self.make_preview(text), c1, c2, key, created),
                             **kwargs)
        self.status.config(text=f"{len(rows)}개 항목")

    def _refresh_all(self):
        """전체 UI와 (떠 있으면) 미니 UI를 함께 갱신"""
        self._refresh()
        if self.mini.state() != "withdrawn":
            self.mini.refresh()

    def _selected_ids(self):
        return [int(i) for i in self.tree.selection()]

    def _copy_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        if self.copy_clip(ids[0]):
            self.status.config(text="클립보드에 복사됨")

    def _set_category(self):
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("클립보드 관리", "항목을 먼저 선택하세요.", parent=self)
            return
        cat1, cat2, key = self.db.get_category_key(ids[0])
        if len(ids) > 1:
            key = ""  # 키값은 항목별 고유값이므로 다중 선택 시 미리 채우지 않음
        dlg = CategoryDialog(self, self.db, cat1, cat2, key)
        self.wait_window(dlg)
        if dlg.result:
            self.db.set_category_key(ids, *dlg.result)
            self._refresh_all()

    def _delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        if messagebox.askyesno("클립보드 관리", f"{len(ids)}개 항목을 삭제할까요?", parent=self):
            self.db.delete(ids)
            self._refresh_all()


class MiniWindow(tk.Toplevel):
    """단축키로 띄우는 소형 팝업 UI — 검색해서 바로 복사하는 용도"""

    WIDTH, HEIGHT = 480, 340
    MAX_ROWS = 50

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.title("클립보드 검색")
        # overrideredirect 창은 Windows에서 포커스/한글 IME가 제대로 붙지 않는다.
        # 얇은 타이틀바만 있는 툴윈도우로 만들어 일반 창처럼 입력을 받게 한다.
        if IS_WINDOWS:
            self.attributes("-toolwindow", True)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.hide)

        outer = tk.Frame(self)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer, padding=(6, 6, 6, 2))
        top.pack(fill="x")
        self.search_var = tk.StringVar()
        self._search_after = None
        self.search_var.trace_add("write", lambda *a: self._schedule_refresh())
        self.entry = ttk.Entry(top, textvariable=self.search_var)
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="전체 UI", command=self._open_full).pack(side="left", padx=(6, 0))

        self.tree = ttk.Treeview(outer, columns=("내용", "키값"), show="tree headings",
                                 height=6, style="Clip.Treeview", selectmode="browse")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=68, stretch=False, anchor="center")
        self.tree.heading("내용", text="내용")
        self.tree.column("내용", width=290, anchor="w")
        self.tree.heading("키값", text="키값")
        self.tree.column("키값", width=100, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=2)
        self._thumbs = {}

        ttk.Label(outer, text="Enter/더블클릭: 복사하고 닫기    Esc: 닫기",
                  foreground="gray").pack(anchor="w", padx=6, pady=(0, 5))

        self.tree.bind("<Double-1>", lambda e: self._copy())
        self.bind("<Return>", lambda e: self._copy())
        self.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<Down>", self._focus_list)

    def _schedule_refresh(self):
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(SEARCH_DEBOUNCE_MS, self._debounced_refresh)

    def _debounced_refresh(self):
        self._search_after = None
        self.refresh()

    def toggle(self, anchor=None):
        if self.state() == "withdrawn":
            self.show(anchor)
        else:
            self.hide()

    def show(self, anchor=None):
        self.refresh()
        # 텍스트 캐럿 위치가 있으면 그 옆에, 없으면 마우스 커서 근처에,
        # 화면을 벗어나지 않게 배치
        if anchor is not None:
            x, y = anchor[0] + 8, anchor[1] + 8
        else:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
        x = min(x, self.winfo_screenwidth() - self.WIDTH - 10)
        y = min(y, self.winfo_screenheight() - self.HEIGHT - 50)
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{max(x, 0)}+{max(y, 0)}")
        self.deiconify()
        self.lift()
        # 창이 화면에 매핑되기 전에 포커스를 주면 Tk가 캐럿 좌표를 IME에
        # 전달하지 못해 한글 조합창이 창 왼쪽 위 (0,0)에 뜬다.
        # 매핑이 끝난 다음 포커스/캐럿을 잡아 조합창이 입력창 안에 오게 한다.
        self.after(80, self._focus_entry)

    def _focus_entry(self):
        if self.state() == "withdrawn":
            return
        self.entry.focus_force()
        self.entry.select_range(0, "end")
        self.entry.icursor("end")   # 캐럿 위치 갱신 → IME 조합창 위치 보정

    def hide(self):
        self.withdraw()

    def refresh(self):
        rows = self.app.db.search(self.search_var.get())[:self.MAX_ROWS]
        self.tree.delete(*self.tree.get_children())
        self._thumbs.clear()
        for cid, kind, text, c1, c2, key, created in rows:
            thumb = self.app.make_thumb(cid, kind)
            kwargs = {}
            if thumb is not None:
                self._thumbs[cid] = thumb
                kwargs["image"] = thumb
            self.tree.insert("", "end", iid=str(cid),
                             values=(self.app.make_preview(text), key), **kwargs)

    def _focus_list(self, _event=None):
        children = self.tree.get_children()
        if children:
            self.tree.focus_set()
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])

    def _copy(self):
        sel = self.tree.selection()
        if not sel:
            sel = self.tree.get_children()[:1]   # 선택이 없으면 맨 위 항목
        if sel:
            self.app.copy_clip(int(sel[0]))
        self.hide()

    def _open_full(self):
        self.hide()
        self.app._show_window()


if __name__ == "__main__":
    app = ClipboardManager()
    app.mainloop()
