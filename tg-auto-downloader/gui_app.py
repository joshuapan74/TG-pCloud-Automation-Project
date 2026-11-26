"""Apple-inspired Tkinter Control Panel, Chat Picker, and Setup Wizard."""

import asyncio
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid
import webbrowser

from PIL import ImageTk
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from file_service import FileService
from platform_utils import (
    APP_DISPLAY_NAME,
    atomic_write_json,
    create_apple_squircle_icon,
    disable_autostart,
    enable_autostart,
    get_bundle_resource_path,
    is_autostart_enabled,
    open_path_in_os,
    read_json_safe,
)
from utils import format_size, get_free_disk_space

# Apple Human Interface Palette
BG_CANVAS = "#F5F5F7"
BG_CARD = "#FFFFFF"
BG_SUBTLE = "#F2F2F7"
BORDER_HAIRLINE = "#E5E5EA"
TEXT_PRIMARY = "#1D1D1F"
TEXT_SECONDARY = "#6E6E73"
APPLE_BLUE = "#007AFF"
APPLE_BLUE_HOVER = "#0062CC"
APPLE_GREEN = "#28CD41"
APPLE_AMBER = "#FF9F0A"
APPLE_RED = "#FF3B30"
BTN_SECONDARY_BG = "#E9E9EE"
BTN_SECONDARY_HOVER = "#DCDCE4"

UI_FONT = "Segoe UI" if sys.platform == "win32" else "SF Pro Text"


def _apply_window_icon(root: tk.Tk):
    """Apply Apple-style squircle icon to window titlebar and taskbar (replacing default Tk icon)."""
    try:
        ico_path = get_bundle_resource_path(os.path.join("assets", "app_icon.ico"))
        if sys.platform == "win32" and os.path.exists(ico_path):
            root.iconbitmap(ico_path)
    except Exception:
        pass
    try:
        icon_img = create_apple_squircle_icon(state="idle", size=64)
        photo = ImageTk.PhotoImage(icon_img)
        root.iconphoto(True, photo)
        root._app_icon_ref = photo
    except Exception:
        pass


def _make_apple_button(parent, text: str, command, primary: bool = False, danger: bool = False, width=None):
    """Create a clean flat Apple-styled button with subtle hover feedback."""
    if primary:
        bg, fg, hover = APPLE_BLUE, "#FFFFFF", APPLE_BLUE_HOVER
    elif danger:
        bg, fg, hover = "#FFE5E5", APPLE_RED, "#FFD0D0"
    else:
        bg, fg, hover = BTN_SECONDARY_BG, TEXT_PRIMARY, BTN_SECONDARY_HOVER

    btn = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        activebackground=hover,
        activeforeground=fg,
        relief="flat",
        bd=0,
        cursor="hand2",
        font=(UI_FONT, 10, "bold" if primary else "normal"),
        padx=14,
        pady=6,
    )
    if width:
        btn.config(width=width)

    def on_enter(_e):
        if btn["state"] != "disabled":
            btn.config(bg=hover)

    def on_leave(_e):
        if btn["state"] != "disabled":
            btn.config(bg=bg)

    btn.bind("<Enter>", on_enter)
    btn.bind("<Leave>", on_leave)
    return btn


def _make_card(parent, title: str = ""):
    """Create an elevated white card container with hairline border."""
    outer = tk.Frame(
        parent,
        bg=BG_CARD,
        highlightbackground=BORDER_HAIRLINE,
        highlightthickness=1,
        bd=0,
    )
    inner = tk.Frame(outer, bg=BG_CARD, padx=14, pady=9)
    inner.pack(fill="both", expand=True)
    if title:
        tk.Label(
            inner,
            text=title,
            bg=BG_CARD,
            fg=TEXT_PRIMARY,
            font=(UI_FONT, 10, "bold"),
        ).pack(anchor="w", pady=(0, 5))
    return outer, inner


class _AsyncLoginBridge:
    """Runs a dedicated asyncio loop in a background thread for login and standalone dialog fetch."""

    def __init__(self, session_path: str):
        self.session_path = session_path
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.client = None
        self.phone = ""
        self.phone_code_hash = ""

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _disconnect_if_open(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def send_code(self, api_id: int, api_hash: str, phone: str):
        await self._disconnect_if_open()
        self.client = TelegramClient(self.session_path, api_id, api_hash)
        await self.client.connect()
        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            await self._disconnect_if_open()
            return ("already_authorized", f"@{me.username or me.first_name} (ID: {me.id})")

        sent = await self.client.send_code_request(phone)
        self.phone = phone
        self.phone_code_hash = sent.phone_code_hash
        return ("code_sent", "驗證碼已發送至您的 Telegram 裝置或簡訊。")

    async def verify_code(self, code: str):
        if self.client is None:
            raise RuntimeError("請先點擊「發送驗證碼」。")
        try:
            await self.client.sign_in(
                phone=self.phone,
                code=code,
                phone_code_hash=self.phone_code_hash,
            )
            me = await self.client.get_me()
            await self._disconnect_if_open()
            return ("signed_in", f"@{me.username or me.first_name} (ID: {me.id})")
        except SessionPasswordNeededError:
            return ("need_2fa", "此帳號已啟用兩步驟驗證，請輸入 2FA 密碼。")

    async def verify_2fa(self, password: str):
        if self.client is None:
            raise RuntimeError("連線階段已過期，請重新發送驗證碼。")
        await self.client.sign_in(password=password)
        me = await self.client.get_me()
        await self._disconnect_if_open()
        return ("signed_in", f"@{me.username or me.first_name} (ID: {me.id})")

    async def fetch_dialogs_direct(self, api_id: int, api_hash: str):
        client = TelegramClient(self.session_path, api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("尚未登入 Telegram 帳號。")
            results = [{"id": "me", "title": "我的收藏 (Saved Messages)", "kind": "我的收藏"}]
            me = await client.get_me()
            my_id = getattr(me, "id", None)
            async for d in client.iter_dialogs(limit=80):
                if my_id is not None and d.id == my_id:
                    continue
                if d.is_channel:
                    kind = "群組" if d.is_group else "頻道"
                elif d.is_group:
                    kind = "群組"
                else:
                    kind = "私人對話"
                results.append({"id": str(d.id), "title": (d.name or str(d.id)).strip(), "kind": kind})
            return results
        finally:
            await client.disconnect()

    async def resolve_chat_direct(self, api_id: int, api_hash: str, raw_input: str):
        from tg_service import parse_chat_reference
        from telethon.utils import get_peer_id

        parsed = parse_chat_reference(raw_input)
        if parsed == "me":
            return {"id": "me", "title": "我的收藏 (Saved Messages)"}

        client = TelegramClient(self.session_path, api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("尚未登入 Telegram 帳號。")
            entity = await client.get_entity(parsed)
            peer_id = get_peer_id(entity)
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(peer_id)
            username = getattr(entity, "username", None)
            if username:
                title = f"{title} (@{username})"
            return {"id": str(peer_id), "title": title}
        finally:
            await client.disconnect()

    def shutdown(self):
        if self.loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._disconnect_if_open(), self.loop)
            try:
                fut.result(timeout=3)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)


class ControlPanelApp:
    """Apple-styled Control Panel providing Telegram login, chat/folder selection, and queue status."""

    def __init__(self, initial_tab: str = "account"):
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.file_service = FileService()
        self.login_bridge = _AsyncLoginBridge(self.file_service.get_session_base_path())

        self.root = tk.Tk()
        self.root.title(APP_DISPLAY_NAME)
        self.root.configure(bg=BG_CANVAS)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        _apply_window_icon(self.root)

        self._daemon_alive = False
        self._daemon_version = ""
        self._build_styles()
        self._build_ui(initial_tab)
        self._fit_fixed_window(initial_tab)
        self._ensure_visible()
        self._poll_status()

    def _fit_fixed_window(self, initial_tab: str):
        """Measure all tabs and lock the window to an exact compact non-resizable size."""
        max_w, max_h = 760, 480
        for key in self._tab_frames:
            self._select_tab(key)
            self.root.update_idletasks()
            max_w = max(max_w, self.root.winfo_reqwidth())
            max_h = max(max_h, self.root.winfo_reqheight())
        self._select_tab(initial_tab if initial_tab in self._tab_frames else "account")
        self.root.update_idletasks()
        self.root.geometry(f"{max_w}x{max_h}")
        self.root.resizable(False, False)

    def _ensure_visible(self):
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        self.root.focus_force()

    def _build_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Apple.Treeview",
            background=BG_CARD,
            fieldbackground=BG_CARD,
            foreground=TEXT_PRIMARY,
            rowheight=28,
            borderwidth=0,
            font=(UI_FONT, 10),
        )
        style.configure(
            "Apple.Treeview.Heading",
            background=BG_SUBTLE,
            foreground=TEXT_SECONDARY,
            relief="flat",
            font=(UI_FONT, 9, "bold"),
        )
        style.map("Apple.Treeview", background=[("selected", "#E5F1FF")], foreground=[("selected", APPLE_BLUE)])

    def _send_daemon_command(self, action: str, payload: str = ""):
        cmd = {"id": str(uuid.uuid4()), "action": action, "payload": payload, "ts": time.time()}
        atomic_write_json(self.file_service.command_path, cmd)

    def _wait_for_daemon_client_stop(self, timeout: float = 4.0):
        self._send_daemon_command("stop_client")
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = read_json_safe(self.file_service.status_path, {})
            ts = float(status.get("timestamp", 0) or 0)
            if time.time() - ts > 3.5 or not status.get("client_active", False):
                return
            time.sleep(0.2)

    def _build_ui(self, initial_tab: str):
        # Top Header Bar
        header = tk.Frame(self.root, bg=BG_CANVAS, padx=18, pady=10)
        header.pack(fill="x")

        badge_img = create_apple_squircle_icon(state="idle", size=40)
        self._header_badge_photo = ImageTk.PhotoImage(badge_img)
        tk.Label(header, image=self._header_badge_photo, bg=BG_CANVAS).pack(side="left", padx=(0, 12))

        title_col = tk.Frame(header, bg=BG_CANVAS)
        title_col.pack(side="left")
        tk.Label(
            title_col,
            text=APP_DISPLAY_NAME,
            bg=BG_CANVAS,
            fg=TEXT_PRIMARY,
            font=(UI_FONT, 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_col,
            text="Telegram 雲端檔案自動下載與同步資料夾助手",
            bg=BG_CANVAS,
            fg=TEXT_SECONDARY,
            font=(UI_FONT, 9),
        ).pack(anchor="w")

        self.status_pill = tk.Label(
            header,
            text="● 檢查狀態中...",
            bg=BG_SUBTLE,
            fg=TEXT_SECONDARY,
            font=(UI_FONT, 9, "bold"),
            padx=12,
            pady=5,
        )
        self.status_pill.pack(side="right")

        # Apple-style Segmented Tab Control
        seg_wrap = tk.Frame(self.root, bg=BG_CANVAS, padx=18)
        seg_wrap.pack(fill="x", pady=(0, 8))
        seg_bar = tk.Frame(seg_wrap, bg="#E5E5EA", padx=3, pady=3)
        seg_bar.pack(fill="x")

        self._tab_buttons = {}
        self._tab_frames = {}
        tabs = [
            ("account", "1. Telegram 帳號登入"),
            ("settings", "2. 對話框與同步設定"),
            ("status", "3. 下載佇列與狀態"),
        ]
        for idx, (key, label) in enumerate(tabs):
            btn = tk.Button(
                seg_bar,
                text=label,
                command=lambda k=key: self._select_tab(k),
                relief="flat",
                bd=0,
                cursor="hand2",
                font=(UI_FONT, 10, "bold"),
                pady=6,
            )
            btn.pack(side="left", fill="x", expand=True, padx=2)
            self._tab_buttons[key] = btn

        self.content_area = tk.Frame(self.root, bg=BG_CANVAS, padx=18)
        self.content_area.pack(fill="both", expand=True, pady=(0, 12))

        self.tab_account = tk.Frame(self.content_area, bg=BG_CANVAS)
        self.tab_settings = tk.Frame(self.content_area, bg=BG_CANVAS)
        self.tab_status = tk.Frame(self.content_area, bg=BG_CANVAS)
        self._tab_frames = {
            "account": self.tab_account,
            "settings": self.tab_settings,
            "status": self.tab_status,
        }

        self._build_account_tab()
        self._build_settings_tab()
        self._build_status_tab()
        self._select_tab(initial_tab if initial_tab in self._tab_frames else "account")

    def _select_tab(self, tab_key: str):
        for key, frame in self._tab_frames.items():
            if key == tab_key:
                frame.pack(fill="both", expand=True)
                self._tab_buttons[key].config(bg=BG_CARD, fg=TEXT_PRIMARY)
            else:
                frame.pack_forget()
                self._tab_buttons[key].config(bg="#E5E5EA", fg=TEXT_SECONDARY)

    # ─── Tab 1: Account & Login ──────────────────────────────────────────────

    def _build_account_tab(self):
        c_state, inner_state = _make_card(self.tab_account, "目前連線與帳號狀態")
        c_state.pack(fill="x", pady=(0, 12))

        self.account_state_var = tk.StringVar(value="檢查中...")
        tk.Label(
            inner_state,
            textvariable=self.account_state_var,
            bg=BG_CARD,
            fg=TEXT_PRIMARY,
            font=(UI_FONT, 10),
        ).pack(side="left")
        _make_apple_button(inner_state, "登出目前帳號", self._on_logout, danger=True).pack(side="right")

        c_cred, inner_cred = _make_card(self.tab_account, "步驟 A：Telegram API 憑證（僅存於本機個人設定目錄）")
        c_cred.pack(fill="x", pady=(0, 12))

        r0 = tk.Frame(inner_cred, bg=BG_CARD)
        r0.pack(fill="x", pady=4)
        tk.Label(r0, text="API ID", width=14, anchor="w", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 10)).pack(side="left")
        self.api_id_var = tk.StringVar(value=self.file_service.tg_api_id)
        tk.Entry(
            r0, textvariable=self.api_id_var, width=30, bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        ).pack(side="left", padx=(0, 10), ipady=4)
        _make_apple_button(
            r0, "前往 my.telegram.org 取得", lambda: webbrowser.open("https://my.telegram.org")
        ).pack(side="left")

        r1 = tk.Frame(inner_cred, bg=BG_CARD)
        r1.pack(fill="x", pady=4)
        tk.Label(r1, text="API Hash", width=14, anchor="w", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 10)).pack(side="left")
        self.api_hash_var = tk.StringVar(value=self.file_service.tg_api_hash)
        self.api_hash_entry = tk.Entry(
            r1, textvariable=self.api_hash_var, width=30, show="•", bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        )
        self.api_hash_entry.pack(side="left", padx=(0, 10), ipady=4)
        self.show_hash_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            r1, text="顯示 Hash", variable=self.show_hash_var, bg=BG_CARD, fg=TEXT_PRIMARY,
            activebackground=BG_CARD, selectcolor=BG_CARD, font=(UI_FONT, 9),
            command=lambda: self.api_hash_entry.config(show="" if self.show_hash_var.get() else "•"),
        ).pack(side="left")

        c_login, inner_login = _make_card(self.tab_account, "步驟 B：手機號碼與驗證碼登入（支援 2FA）")
        c_login.pack(fill="x", pady=(0, 10))

        rp = tk.Frame(inner_login, bg=BG_CARD)
        rp.pack(fill="x", pady=5)
        tk.Label(rp, text="手機號碼（含國碼）", width=16, anchor="w", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 10)).pack(side="left")
        self.phone_var = tk.StringVar()
        tk.Entry(
            rp, textvariable=self.phone_var, width=24, bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        ).pack(side="left", padx=(0, 10), ipady=4)
        self.btn_send_code = _make_apple_button(rp, "發送驗證碼", self._on_send_code, primary=True)
        self.btn_send_code.pack(side="left")
        tk.Label(rp, text="例：+886912345678", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 9)).pack(side="left", padx=10)

        rc = tk.Frame(inner_login, bg=BG_CARD)
        rc.pack(fill="x", pady=5)
        tk.Label(rc, text="Telegram 驗證碼", width=16, anchor="w", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 10)).pack(side="left")
        self.code_var = tk.StringVar()
        self.entry_code = tk.Entry(
            rc, textvariable=self.code_var, width=24, state="disabled", bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        )
        self.entry_code.pack(side="left", padx=(0, 10), ipady=4)
        self.btn_verify_code = _make_apple_button(rc, "驗證登入", self._on_verify_code, primary=True)
        self.btn_verify_code.config(state="disabled")
        self.btn_verify_code.pack(side="left")

        r2 = tk.Frame(inner_login, bg=BG_CARD)
        r2.pack(fill="x", pady=5)
        tk.Label(r2, text="2FA 兩步驟密碼", width=16, anchor="w", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 10)).pack(side="left")
        self.pwd_var = tk.StringVar()
        self.entry_pwd = tk.Entry(
            r2, textvariable=self.pwd_var, width=24, show="•", state="disabled", bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        )
        self.entry_pwd.pack(side="left", padx=(0, 10), ipady=4)
        self.btn_verify_pwd = _make_apple_button(r2, "送出 2FA 密碼", self._on_verify_2fa, primary=True)
        self.btn_verify_pwd.config(state="disabled")
        self.btn_verify_pwd.pack(side="left")

        self.login_feedback_var = tk.StringVar(
            value="提示：登入成功後會在本機系統目錄產生專屬 Session，之後不需重複登入。"
        )
        tk.Label(
            self.tab_account, textvariable=self.login_feedback_var, bg=BG_CANVAS, fg=TEXT_SECONDARY,
            font=(UI_FONT, 9), wraplength=740, justify="left"
        ).pack(anchor="w", pady=4)

    def _on_send_code(self):
        ok, msg = self.file_service.set_credentials(self.api_id_var.get(), self.api_hash_var.get())
        if not ok:
            messagebox.showerror("憑證格式錯誤", msg, parent=self.root)
            return
        phone = self.phone_var.get().strip()
        if not phone:
            messagebox.showerror("缺少手機號碼", "請輸入包含國碼的手機號碼（例如 +886912345678）。", parent=self.root)
            return

        api_id, api_hash = self.file_service.get_credentials()
        self.btn_send_code.config(state="disabled")
        self.login_feedback_var.set("正在暫停背景連線並向 Telegram 請求驗證碼...")

        def worker():
            try:
                self._wait_for_daemon_client_stop()
                fut = self.login_bridge.submit(self.login_bridge.send_code(api_id, api_hash, phone))
                status, info = fut.result(timeout=30)
                self.root.after(0, lambda: self._handle_send_code_result(status, info))
            except PhoneNumberInvalidError:
                self.root.after(0, lambda: self._handle_login_error("手機號碼格式無效，請確認國碼（例如 +886...）。"))
            except FloodWaitError as e:
                self.root.after(0, lambda: self._handle_login_error(f"請求過於頻繁，請於 {e.seconds} 秒後再試。"))
            except Exception as e:
                self.root.after(0, lambda: self._handle_login_error(f"發送驗證碼失敗: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_send_code_result(self, status: str, info: str):
        self.btn_send_code.config(state="normal")
        if status == "already_authorized":
            self.login_feedback_var.set(f"已驗證帳號：{info}")
            self._send_daemon_command("start_client")
            messagebox.showinfo("已登入", f"目前 Session 已登入為：{info}", parent=self.root)
            return
        self.entry_code.config(state="normal")
        self.btn_verify_code.config(state="normal")
        self.login_feedback_var.set(info)
        self.entry_code.focus_set()

    def _on_verify_code(self):
        code = self.code_var.get().strip()
        if not code:
            messagebox.showerror("缺少驗證碼", "請輸入 Telegram 傳送給您的驗證碼。", parent=self.root)
            return
        self.btn_verify_code.config(state="disabled")
        self.login_feedback_var.set("正在驗證登入中...")

        def worker():
            try:
                fut = self.login_bridge.submit(self.login_bridge.verify_code(code))
                status, info = fut.result(timeout=30)
                self.root.after(0, lambda: self._handle_verify_result(status, info))
            except PhoneCodeInvalidError:
                self.root.after(0, lambda: self._handle_code_retry("驗證碼不正確，請重新確認。"))
            except PhoneCodeExpiredError:
                self.root.after(0, lambda: self._handle_login_error("驗證碼已過期，請重新點擊「發送驗證碼」。"))
            except Exception as e:
                self.root.after(0, lambda: self._handle_code_retry(f"驗證失敗: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_verify_result(self, status: str, info: str):
        if status == "need_2fa":
            self.login_feedback_var.set(info)
            self.entry_pwd.config(state="normal")
            self.btn_verify_pwd.config(state="normal")
            self.entry_pwd.focus_set()
            return
        if status == "signed_in":
            self._on_login_completed(info)

    def _on_verify_2fa(self):
        pwd = self.pwd_var.get()
        if not pwd:
            messagebox.showerror("缺少密碼", "請輸入您的 Telegram 兩步驟驗證密碼。", parent=self.root)
            return
        self.btn_verify_pwd.config(state="disabled")
        self.login_feedback_var.set("正在驗證 2FA 密碼...")

        def worker():
            try:
                fut = self.login_bridge.submit(self.login_bridge.verify_2fa(pwd))
                status, info = fut.result(timeout=30)
                if status == "signed_in":
                    self.root.after(0, lambda: self._on_login_completed(info))
            except PasswordHashInvalidError:
                self.root.after(0, lambda: self._handle_2fa_retry("兩步驟驗證密碼錯誤，請再試一次。"))
            except Exception as e:
                self.root.after(0, lambda: self._handle_2fa_retry(f"2FA 驗證失敗: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_login_completed(self, account_info: str):
        self.code_var.set("")
        self.pwd_var.set("")
        self.entry_code.config(state="disabled")
        self.btn_verify_code.config(state="disabled")
        self.entry_pwd.config(state="disabled")
        self.btn_verify_pwd.config(state="disabled")
        self.btn_send_code.config(state="normal")
        self.login_feedback_var.set(f"登入成功：{account_info}。已通知背景服務連線！")
        self._send_daemon_command("start_client")
        messagebox.showinfo("登入成功", f"歡迎 {account_info}！\n可前往「2. 對話框與同步設定」指定抓取對話框與資料夾。", parent=self.root)

    def _handle_code_retry(self, msg: str):
        self.btn_verify_code.config(state="normal")
        self.login_feedback_var.set(msg)

    def _handle_2fa_retry(self, msg: str):
        self.btn_verify_pwd.config(state="normal")
        self.login_feedback_var.set(msg)

    def _handle_login_error(self, msg: str):
        self.btn_send_code.config(state="normal")
        self.entry_code.config(state="disabled")
        self.btn_verify_code.config(state="disabled")
        self.entry_pwd.config(state="disabled")
        self.btn_verify_pwd.config(state="disabled")
        self.login_feedback_var.set(msg)

    def _on_logout(self):
        if not messagebox.askyesno("確認登出", "確定要登出並清除本機 Telegram Session 嗎？", parent=self.root):
            return
        self.login_feedback_var.set("正在停止連線並清除 Session...")

        def worker():
            self._wait_for_daemon_client_stop()
            self.file_service.delete_session_files()
            self.root.after(0, lambda: self.login_feedback_var.set("已登出並清除本機 Session。"))

        threading.Thread(target=worker, daemon=True).start()

    # ─── Tab 2: Target Dialog, Sync Folder & Preferences ─────────────────────

    def _build_settings_tab(self):
        # Card 1: Target Telegram Chat / Dialog
        c_chat, inner_chat = _make_card(self.tab_settings, "監聽抓取的 Telegram 對話框")
        c_chat.pack(fill="x", pady=(0, 8))

        self.watch_chat_id = self.file_service.watch_chat_id
        self.watch_chat_title_var = tk.StringVar(value=self.file_service.watch_chat_title)

        row_chat = tk.Frame(inner_chat, bg=BG_CARD)
        row_chat.pack(fill="x", pady=(0, 6))
        tk.Label(row_chat, text="目前目標：", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 10)).pack(side="left")
        tk.Label(
            row_chat, textvariable=self.watch_chat_title_var, bg=BG_SUBTLE, fg=APPLE_BLUE,
            font=(UI_FONT, 10, "bold"), padx=10, pady=4
        ).pack(side="left", padx=(4, 10))
        _make_apple_button(row_chat, "從清單選擇...", self._open_chat_picker, primary=True).pack(side="right")
        _make_apple_button(
            row_chat, "重設為我的收藏", self._reset_watch_chat
        ).pack(side="right", padx=(0, 8))

        row_paste = tk.Frame(inner_chat, bg=BG_CARD)
        row_paste.pack(fill="x", pady=(0, 4))
        tk.Label(
            row_paste, text="貼上位置：", bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 10)
        ).pack(side="left")
        self.chat_paste_var = tk.StringVar()
        tk.Entry(
            row_paste, textvariable=self.chat_paste_var, bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        ).pack(side="left", fill="x", expand=True, padx=(4, 8), ipady=4)
        self.btn_resolve_chat = _make_apple_button(row_paste, "解析套用", self._resolve_pasted_chat)
        self.btn_resolve_chat.pack(side="right")

        tk.Label(
            inner_chat,
            text="支援貼上 @用戶名、https://t.me/...、私人訊息連結 https://t.me/c/.../123 或數字 Chat ID",
            bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 8),
        ).pack(anchor="w", pady=(0, 6))

        self.catch_up_var = tk.BooleanVar(value=self.file_service.catch_up_missed)
        tk.Checkbutton(
            inner_chat,
            text="補抓程式關閉期間的新檔案（啟動連線後自動掃描上一次關機後傳入該對話框的新媒體）",
            variable=self.catch_up_var,
            bg=BG_CARD, fg=TEXT_PRIMARY, activebackground=BG_CARD, selectcolor=BG_CARD, font=(UI_FONT, 9),
        ).pack(anchor="w")

        # Card 2: Folders
        c_dir, inner_dir = _make_card(self.tab_settings, "資料夾路徑設定")
        c_dir.pack(fill="x", pady=(0, 8))

        tk.Label(
            inner_dir,
            text="上傳同步目標資料夾（預設 ~/Downloads/TelegramAuto）：",
            bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(0, 2))
        row_ship = tk.Frame(inner_dir, bg=BG_CARD)
        row_ship.pack(fill="x", pady=(0, 6))
        self.shipping_dir_var = tk.StringVar(value=self.file_service.get_shipping_dir())
        tk.Entry(
            row_ship, textvariable=self.shipping_dir_var, bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        ).pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=3)
        _make_apple_button(row_ship, "選擇資料夾...", self._browse_shipping_dir).pack(side="right")

        tk.Label(
            inner_dir,
            text="下載暫存工作區（未完成與斷點分段檔放置處）：",
            bg=BG_CARD, fg=TEXT_SECONDARY, font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(0, 2))
        row_dl = tk.Frame(inner_dir, bg=BG_CARD)
        row_dl.pack(fill="x")
        self.download_dir_var = tk.StringVar(value=self.file_service.get_download_dir())
        tk.Entry(
            row_dl, textvariable=self.download_dir_var, bg=BG_SUBTLE, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        ).pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=3)
        _make_apple_button(row_dl, "選擇資料夾...", self._browse_download_dir).pack(side="right")

        # Card 3: Automation Preferences
        c_pref, inner_pref = _make_card(self.tab_settings, "自動化行為偏好")
        c_pref.pack(fill="x", pady=(0, 8))

        self.auto_unzip_var = tk.BooleanVar(value=self.file_service.auto_unzip)
        tk.Checkbutton(
            inner_pref,
            text="自動解壓縮標準 ZIP 檔案（遇到加密 ZIP 會自動隔離警告，不刪除雲端訊息）",
            variable=self.auto_unzip_var,
            bg=BG_CARD, fg=TEXT_PRIMARY, activebackground=BG_CARD, selectcolor=BG_CARD, font=(UI_FONT, 9),
        ).pack(anchor="w", pady=2)

        self.delete_after_var = tk.BooleanVar(value=self.file_service.delete_after_transfer)
        tk.Checkbutton(
            inner_pref,
            text="確認移轉成功後，自動刪除 Telegram 對話框中的原檔訊息（需有刪除權限）",
            variable=self.delete_after_var,
            bg=BG_CARD, fg=TEXT_PRIMARY, activebackground=BG_CARD, selectcolor=BG_CARD, font=(UI_FONT, 9),
        ).pack(anchor="w", pady=2)

        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        tk.Checkbutton(
            inner_pref,
            text="開機自動啟動背景常駐程式（Windows 登錄檔 / macOS LaunchAgent）",
            variable=self.autostart_var,
            bg=BG_CARD, fg=TEXT_PRIMARY, activebackground=BG_CARD, selectcolor=BG_CARD, font=(UI_FONT, 9),
        ).pack(anchor="w", pady=2)

        footer = tk.Frame(self.tab_settings, bg=BG_CANVAS)
        footer.pack(fill="x")
        self.settings_msg_var = tk.StringVar(value=f"設定檔位置：{self.file_service.config_path}")
        tk.Label(footer, textvariable=self.settings_msg_var, bg=BG_CANVAS, fg=TEXT_SECONDARY, font=(UI_FONT, 9)).pack(side="left")
        _make_apple_button(footer, "儲存設定並套用", self._on_save_settings, primary=True).pack(side="right")

    def _reset_watch_chat(self):
        self.watch_chat_id = "me"
        self.watch_chat_title_var.set("我的收藏 (Saved Messages)")
        self.chat_paste_var.set("")

    def _resolve_pasted_chat(self):
        raw = self.chat_paste_var.get().strip()
        if not raw:
            messagebox.showinfo("提示", "請先貼上 Telegram 連結、@用戶名或 Chat ID。", parent=self.root)
            return

        api_id, api_hash = self.file_service.get_credentials()
        if not api_id or not api_hash or not self.file_service.has_session_file():
            messagebox.showwarning("請先登入", "請先完成 Telegram 登入，才能驗證與解析對話框。", parent=self.root)
            return

        if self._daemon_alive and self._daemon_version != "3.2":
            messagebox.showwarning("請重啟背景服務", "偵測到舊版背景服務仍在運作，請先從系統匣「結束程式」後重新開啟。", parent=self.root)
            return

        self.btn_resolve_chat.config(state="disabled")
        self.settings_msg_var.set("正在解析 Telegram 對話框位置...")

        def worker():
            try:
                if self._daemon_alive:
                    old_ts = float(read_json_safe(self.file_service.resolved_chat_path, {}).get("timestamp", 0) or 0)
                    self._send_daemon_command("resolve_chat", payload=raw)
                    deadline = time.time() + 10.0
                    res = None
                    while time.time() < deadline:
                        data = read_json_safe(self.file_service.resolved_chat_path, {})
                        if float(data.get("timestamp", 0) or 0) > old_ts:
                            res = data
                            break
                        time.sleep(0.2)
                    if not res:
                        raise RuntimeError("背景服務逾時未回應，請重試。")
                    if not res.get("ok"):
                        raise RuntimeError(res.get("error") or "解析失敗")
                else:
                    fut = self.login_bridge.submit(self.login_bridge.resolve_chat_direct(api_id, api_hash, raw))
                    res = fut.result(timeout=15)

                cid = str(res["id"])
                title = str(res["title"])

                def done_ui():
                    self.btn_resolve_chat.config(state="normal")
                    self.watch_chat_id = cid
                    self.watch_chat_title_var.set(f"{title} (ID: {cid})" if cid != "me" else title)
                    self.settings_msg_var.set(f"✓ 已解析：{title}，請記得點「儲存設定並套用」。")

                self.root.after(0, done_ui)
            except Exception as e:
                def err_ui():
                    self.btn_resolve_chat.config(state="normal")
                    self.settings_msg_var.set(f"解析失敗：{e}")
                    messagebox.showerror("解析對話框失敗", str(e), parent=self.root)

                self.root.after(0, err_ui)

        threading.Thread(target=worker, daemon=True).start()

    def _open_chat_picker(self):
        api_id, api_hash = self.file_service.get_credentials()
        if not api_id or not api_hash or not self.file_service.has_session_file():
            messagebox.showwarning("請先登入", "請先在「1. Telegram 帳號登入」完成登入後，再讀取對話框清單。", parent=self.root)
            return

        if self._daemon_alive and self._daemon_version != "3.2":
            messagebox.showwarning("請重啟背景服務", "偵測到舊版背景服務仍在運作，請先從系統匣「結束程式」後重新開啟。", parent=self.root)
            return

        modal = tk.Toplevel(self.root)
        modal.title("選擇要抓取的 Telegram 對話框")
        modal.configure(bg=BG_CANVAS)
        modal.geometry("540x450")
        modal.resizable(False, False)
        modal.transient(self.root)
        modal.grab_set()
        _apply_window_icon(modal)

        top = tk.Frame(modal, bg=BG_CANVAS, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="搜尋對話框", bg=BG_CANVAS, fg=TEXT_SECONDARY, font=(UI_FONT, 9)).pack(anchor="w")

        search_var = tk.StringVar()
        search_entry = tk.Entry(
            top, textvariable=search_var, bg=BG_CARD, fg=TEXT_PRIMARY,
            relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
        )
        search_entry.pack(fill="x", pady=(4, 0), ipady=5)

        body = tk.Frame(modal, bg=BG_CANVAS, padx=16)
        body.pack(fill="both", expand=True)

        tree = ttk.Treeview(body, columns=("kind", "title", "id"), show="headings", style="Apple.Treeview")
        tree.heading("kind", text="類型")
        tree.heading("title", text="對話名稱")
        tree.heading("id", text="Chat ID")
        tree.column("kind", width=85, anchor="center")
        tree.column("title", width=290, anchor="w")
        tree.column("id", width=115, anchor="center")
        tree.pack(fill="both", expand=True)

        status_lbl_var = tk.StringVar(value="正在載入 Telegram 對話清單...")
        tk.Label(modal, textvariable=status_lbl_var, bg=BG_CANVAS, fg=TEXT_SECONDARY, font=(UI_FONT, 9)).pack(anchor="w", padx=16, pady=(6, 0))

        all_dialogs = []

        def render_list(*_args):
            q = search_var.get().strip().lower()
            for r in tree.get_children():
                tree.delete(r)
            for d in all_dialogs:
                if q and q not in d["title"].lower() and q not in d["id"].lower() and q not in d["kind"].lower():
                    continue
                tree.insert("", "end", values=(d["kind"], d["title"], d["id"]))

        search_var.trace_add("write", render_list)

        def apply_selected(_event=None):
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "請先點選一個對話框。", parent=modal)
                return
            vals = tree.item(sel[0], "values")
            kind, title, cid = vals[0], vals[1], vals[2]
            self.watch_chat_id = cid
            display = title if cid == "me" else f"{title} [{kind}]"
            self.watch_chat_title_var.set(display)
            modal.destroy()

        tree.bind("<Double-1>", apply_selected)

        btn_bar = tk.Frame(modal, bg=BG_CANVAS, padx=16, pady=12)
        btn_bar.pack(fill="x")
        _make_apple_button(btn_bar, "取消", modal.destroy).pack(side="left")
        _make_apple_button(btn_bar, "套用此對話框", apply_selected, primary=True).pack(side="right")

        def load_dialogs():
            nonlocal all_dialogs
            try:
                if self._daemon_alive:
                    old_ts = float(read_json_safe(self.file_service.dialogs_path, {}).get("timestamp", 0) or 0)
                    self._send_daemon_command("refresh_dialogs")
                    deadline = time.time() + 12.0
                    data = {}
                    while time.time() < deadline:
                        cand = read_json_safe(self.file_service.dialogs_path, {})
                        if float(cand.get("timestamp", 0) or 0) > old_ts:
                            data = cand
                            break
                        time.sleep(0.25)
                    if not data:
                        data = read_json_safe(self.file_service.dialogs_path, {})
                    if data.get("error"):
                        raise RuntimeError(data["error"])
                    all_dialogs = data.get("dialogs", [])
                else:
                    fut = self.login_bridge.submit(self.login_bridge.fetch_dialogs_direct(api_id, api_hash))
                    all_dialogs = fut.result(timeout=20)
                msg = (
                    f"共載入 {len(all_dialogs)} 個對話框（雙擊即可選取）"
                    if all_dialogs
                    else "未取得對話框，建議直接關閉並在設定頁貼上對話連結或 @用戶名。"
                )
                modal.after(0, lambda: (render_list(), status_lbl_var.set(msg)))
            except Exception as e:
                modal.after(0, lambda: status_lbl_var.set(f"載入失敗：{e}"))

        threading.Thread(target=load_dialogs, daemon=True).start()

    def _browse_shipping_dir(self):
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="選擇上傳同步目標資料夾",
            initialdir=self.shipping_dir_var.get() or os.path.expanduser("~"),
        )
        if chosen:
            self.shipping_dir_var.set(os.path.abspath(chosen))

    def _browse_download_dir(self):
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="選擇本機下載暫存工作區",
            initialdir=self.download_dir_var.get() or os.path.expanduser("~"),
        )
        if chosen:
            self.download_dir_var.set(os.path.abspath(chosen))

    def _on_save_settings(self):
        ship = self.shipping_dir_var.get().strip() or self.file_service.default_shipping_dir
        dl = self.download_dir_var.get().strip() or self.file_service.default_download_dir

        if not self.file_service.set_download_dir(dl):
            messagebox.showerror("路徑無效", "下載暫存資料夾無法寫入或建立，請重新選擇。", parent=self.root)
            return
        if not self.file_service.set_shipping_dir(ship):
            messagebox.showerror("路徑無效", "上傳同步目標資料夾無法寫入，請確認權限或掛載狀態。", parent=self.root)
            return

        self.shipping_dir_var.set(self.file_service.get_shipping_dir())
        self.download_dir_var.set(self.file_service.get_download_dir())

        self.file_service.auto_unzip = self.auto_unzip_var.get()
        self.file_service.delete_after_transfer = self.delete_after_var.get()
        self.file_service.catch_up_missed = self.catch_up_var.get()
        self.file_service.watch_chat_id = self.watch_chat_id
        self.file_service.watch_chat_title = self.watch_chat_title_var.get()
        self.file_service.save_config()

        want_autostart = self.autostart_var.get()
        if want_autostart != is_autostart_enabled():
            ok, msg = enable_autostart() if want_autostart else disable_autostart()
            if not ok:
                messagebox.showwarning("開機啟動設定提示", msg, parent=self.root)
                self.autostart_var.set(is_autostart_enabled())

        self._send_daemon_command("reload_config")
        self.settings_msg_var.set("✓ 設定已儲存並套用。")
        messagebox.showinfo("儲存成功", "設定已更新並通知背景服務套用。", parent=self.root)

    # ─── Tab 3: Queue & System Status ────────────────────────────────────────

    def _build_status_tab(self):
        c_sys, inner_sys = _make_card(self.tab_status, "系統健康看板")
        c_sys.pack(fill="x", pady=(0, 12))

        self.daemon_info_var = tk.StringVar(value="等待背景服務回報...")
        tk.Label(
            inner_sys, textvariable=self.daemon_info_var, bg=BG_CARD, fg=TEXT_PRIMARY,
            font=(UI_FONT, 10), justify="left", wraplength=740
        ).pack(anchor="w")

        self.warning_var = tk.StringVar(value="")
        tk.Label(
            inner_sys, textvariable=self.warning_var, bg=BG_CARD, fg=APPLE_RED, font=(UI_FONT, 9, "bold")
        ).pack(anchor="w", pady=(4, 0))

        c_q, inner_q = _make_card(self.tab_status, "下載任務與近期歷史（重啟自動保留續傳）")
        c_q.pack(fill="both", expand=True, pady=(0, 10))

        cols = ("name", "size", "progress", "speed", "eta", "status")
        self.tree = ttk.Treeview(inner_q, columns=cols, show="headings", height=7, style="Apple.Treeview")
        headers = {
            "name": ("檔案名稱", 220),
            "size": ("大小", 80),
            "progress": ("進度", 65),
            "speed": ("速度", 85),
            "eta": ("剩餘", 65),
            "status": ("狀態", 145),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w" if key in ("name", "status") else "center")
        self.tree.pack(fill="both", expand=True)

        actions = tk.Frame(self.tab_status, bg=BG_CANVAS)
        actions.pack(fill="x")
        _make_apple_button(actions, "一鍵清除任務與歷史", self._on_clear_all_queue, danger=True).pack(side="left", padx=(0, 6))
        _make_apple_button(actions, "清異常項目", self._on_clear_abnormal).pack(side="left", padx=(0, 6))
        _make_apple_button(
            actions, "目標資料夾", lambda: self._open_dir(self.file_service.get_shipping_dir())
        ).pack(side="left", padx=(0, 6))
        _make_apple_button(
            actions, "暫存工作區", lambda: self._open_dir(self.file_service.get_download_dir())
        ).pack(side="left", padx=(0, 6))
        _make_apple_button(
            actions, "日誌", lambda: open_path_in_os(self.file_service.log_path)
        ).pack(side="right")

    def _open_dir(self, path: str):
        if not path or not open_path_in_os(path):
            messagebox.showinfo("提示", "資料夾尚未建立或目前無法開啟。", parent=self.root)

    def _on_clear_all_queue(self):
        self._send_daemon_command("clear_all_queue")
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)

    def _on_clear_abnormal(self):
        self._send_daemon_command("clear_abnormal")
        messagebox.showinfo("已送出", "已通知背景服務清除異常項目與殘留快取。", parent=self.root)

    def _poll_status(self):
        status = read_json_safe(self.file_service.status_path, {})
        now = time.time()
        ts = float(status.get("timestamp", 0) or 0)
        alive = (now - ts) <= 4.0
        self._daemon_alive = alive
        self._daemon_version = str(status.get("version", "") or "")

        self.file_service.load_config()
        has_creds = all(self.file_service.get_credentials())
        has_session = self.file_service.has_session_file()

        if alive and status.get("connected"):
            acct = status.get("account", "已登入")
            self.status_pill.config(text=f"● 運作中 ({acct})", bg="#E3F9E9", fg="#157F3D")
            self.account_state_var.set(f"已連線：{acct}")
        elif alive and not has_creds:
            self.status_pill.config(text="● 等待設定 API 憑證", bg=BG_SUBTLE, fg=TEXT_SECONDARY)
            self.account_state_var.set("尚未設定 Telegram API ID / Hash")
        elif alive and not has_session:
            self.status_pill.config(text="● 等待 Telegram 登入", bg="#FFF4E0", fg="#B25E09")
            self.account_state_var.set("尚未登入（請於下方完成驗證碼登入）")
        elif alive:
            self.status_pill.config(text="● 連線中 / 未連線", bg="#FFF4E0", fg="#B25E09")
            self.account_state_var.set(status.get("last_error") or "尚未連線")
        else:
            self.status_pill.config(text="● 背景服務未啟動", bg="#FFE5E5", fg=APPLE_RED)
            self.account_state_var.set("已登入本機 Session" if has_session else "尚未登入")

        free_gb = get_free_disk_space(self.file_service.get_download_dir())
        mem_mb = status.get("memory_mb", 0.0)
        pending = status.get("pending_count", 0)
        pid = status.get("pid", "--")
        self.daemon_info_var.set(
            f"背景服務：{'運作中 (PID ' + str(pid) + ')' if alive else '未啟動'}   •   "
            f"記憶體：{mem_mb:.1f} MB   •   可用空間：{free_gb:.2f} GB   •   等待任務：{pending}\n"
            f"監聽對話框：{self.file_service.watch_chat_title}   |   目標資料夾：{self.file_service.get_shipping_dir()}"
        )

        if status.get("has_encrypted_warning"):
            self.warning_var.set("⚠️ 警告：偵測到加密 ZIP 檔，已隔離保留於暫存工作區且未刪除雲端訊息。")
        else:
            self.warning_var.set("")

        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        for item in status.get("items", []):
            self.tree.insert(
                "",
                "end",
                values=(
                    item.get("name", ""),
                    format_size(int(item.get("size", 0))),
                    f"{item.get('progress', 0.0):.1f}%",
                    item.get("speed", "--"),
                    item.get("eta", "--"),
                    item.get("status", ""),
                ),
            )

        self.root.after(1000, self._poll_status)

    def _on_close(self):
        self.login_bridge.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run_gui(initial_tab: str = "account"):
    app = ControlPanelApp(initial_tab=initial_tab)
    app.run()


def run_required_folder_picker():
    """Apple-styled modal dialog forcing user to choose an accessible destination folder."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    file_service = FileService()
    missing_path = file_service.get_shipping_dir()
    default_dir = file_service.default_shipping_dir

    root = tk.Tk()
    root.title(f"{APP_DISPLAY_NAME} — 需要指定目標資料夾")
    root.configure(bg=BG_CANVAS)
    root.geometry("600x280")
    root.resizable(False, False)
    _apply_window_icon(root)

    outer = tk.Frame(root, bg=BG_CANVAS, padx=18, pady=16)
    outer.pack(fill="both", expand=True)
    card, inner = _make_card(outer)
    card.pack(fill="both", expand=True)

    tk.Label(
        inner,
        text="找不到原本設定的磁碟機或目標資料夾",
        bg=BG_CARD, fg=APPLE_RED, font=(UI_FONT, 12, "bold"),
    ).pack(anchor="w")
    tk.Label(
        inner, text=missing_path or "(未設定)", bg=BG_CARD, fg=TEXT_SECONDARY,
        font=(UI_FONT, 9), wraplength=530,
    ).pack(anchor="w", pady=(2, 12))

    tk.Label(
        inner,
        text="請指定可用的同步目標資料夾（預設為 ~/Downloads/TelegramAuto）：",
        bg=BG_CARD, fg=TEXT_PRIMARY, font=(UI_FONT, 10),
    ).pack(anchor="w", pady=(0, 6))

    path_var = tk.StringVar(value=default_dir)
    row = tk.Frame(inner, bg=BG_CARD)
    row.pack(fill="x", pady=(0, 16))
    tk.Entry(
        row, textvariable=path_var, bg=BG_SUBTLE, fg=TEXT_PRIMARY,
        relief="flat", font=(UI_FONT, 10), highlightthickness=1, highlightbackground=BORDER_HAIRLINE
    ).pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=5)

    def browse():
        chosen = filedialog.askdirectory(parent=root, title="選擇目標資料夾", initialdir=default_dir)
        if chosen:
            path_var.set(os.path.abspath(chosen))

    _make_apple_button(row, "選擇資料夾...", browse).pack(side="right")

    btn_row = tk.Frame(inner, bg=BG_CARD)
    btn_row.pack(fill="x")

    def confirm(target_path: str):
        clean = target_path.strip() or default_dir
        if file_service.set_shipping_dir(clean):
            root.destroy()
        else:
            messagebox.showerror(
                "無法使用此資料夾",
                f"目標資料夾無法寫入或磁碟機不存在：\n{clean}\n\n請選擇其他資料夾或使用預設路徑。",
                parent=root,
            )

    _make_apple_button(btn_row, "改用預設 (~/Downloads/TelegramAuto)", lambda: confirm(default_dir)).pack(side="left")
    _make_apple_button(btn_row, "確認套用", lambda: confirm(path_var.get()), primary=True).pack(side="right")

    def on_attempt_close():
        messagebox.showwarning(
            "必須指定資料夾",
            "找不到原本的磁碟機，請先選擇可用的目標資料夾或點選「改用預設」。",
            parent=root,
        )

    root.protocol("WM_DELETE_WINDOW", on_attempt_close)
    root.update_idletasks()
    root.deiconify()
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    root.focus_force()
    root.mainloop()
