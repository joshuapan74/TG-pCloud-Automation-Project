"""Cross-platform (Windows & macOS) entry point: System Tray Daemon + GUI launcher."""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import pystray

from file_service import FileService
from platform_utils import (
    APP_DISPLAY_NAME,
    atomic_write_json,
    create_tray_icon_image,
    get_launch_command_args,
    get_memory_usage_mb,
    read_json_safe,
)
from queue_service import QueueService
from tg_service import TelegramService, extract_media_info
from utils import format_size, get_free_disk_space, make_progress_bar


class DaemonController:
    """Coordinates TelegramService, QueueWorker, persistent queue recovery, IPC, and Tray Icon."""

    def __init__(self):
        self.file_service = FileService()
        self.queue_service = QueueService(state_path=self.file_service.queue_state_path)
        self.tg_service = None
        self.client_active = False
        self.has_encrypted_warning = False
        self.last_command_id = ""
        self.stop_event = None
        self.loop = None
        self.tray_icon = None
        self._last_icon_state = None
        self._gui_process = None

    def _spawn_ui_subprocess(self, args: list):
        cmd = get_launch_command_args(args)
        kwargs = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 1
            kwargs["startupinfo"] = si
        return subprocess.Popen(cmd, **kwargs)

    def launch_gui(self, tab: str = "account"):
        if self._gui_process is not None and self._gui_process.poll() is None:
            return
        try:
            self._gui_process = self._spawn_ui_subprocess(["--gui", "--tab", tab])
        except Exception as e:
            print(f"[-] 無法開啟設定介面: {e}")

    async def prompt_required_folder_async(self) -> str:
        try:
            proc = self._spawn_ui_subprocess(["--pick-required-folder"])
            while proc.poll() is None:
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[-] 無法開啟強制資料夾選擇視窗: {e}")
        self.file_service.load_config()
        return self.file_service.get_shipping_dir()

    def notify(self, title: str, message: str):
        if self.tray_icon:
            try:
                self.tray_icon.notify(message, title)
            except Exception:
                pass

    def _current_icon_state(self) -> str:
        if self.has_encrypted_warning:
            return "warning"
        for item in self.queue_service.get_all_items():
            if item.get("status") == "下載中":
                return "downloading"
        api_id, api_hash = self.file_service.get_credentials()
        if not api_id or not api_hash or not self.file_service.has_session_file():
            return "unconfigured"
        return "idle"

    def _write_status_snapshot(self):
        connected = bool(
            self.tg_service
            and self.tg_service.client
            and self.tg_service.client.is_connected()
            and self.tg_service.account_label
        )
        payload = {
            "version": "3.2",
            "timestamp": time.time(),
            "pid": os.getpid(),
            "memory_mb": round(get_memory_usage_mb(), 1),
            "client_active": self.client_active,
            "connected": connected,
            "account": self.tg_service.account_label if self.tg_service else "",
            "last_error": self.tg_service.last_error if self.tg_service else "",
            "has_encrypted_warning": self.has_encrypted_warning,
            "pending_count": self.queue_service.get_pending_count(),
            "items": self.queue_service.get_serializable_items(),
        }
        try:
            atomic_write_json(self.file_service.status_path, payload)
        except Exception:
            pass

        new_state = self._current_icon_state()
        if self.tray_icon and new_state != self._last_icon_state:
            self._last_icon_state = new_state
            try:
                self.tray_icon.icon = create_tray_icon_image(new_state)
            except Exception:
                pass

    async def _refresh_dialogs_cache(self):
        if not self.tg_service or not self.client_active:
            atomic_write_json(
                self.file_service.dialogs_path,
                {"timestamp": time.time(), "dialogs": [], "error": "Telegram 尚未連線"},
            )
            return
        try:
            dialogs = await self.tg_service.fetch_dialogs(limit=80)
            atomic_write_json(
                self.file_service.dialogs_path,
                {"timestamp": time.time(), "dialogs": dialogs, "error": ""},
            )
        except Exception as e:
            print(f"[-] 更新對話清單失敗: {e}")
            atomic_write_json(
                self.file_service.dialogs_path,
                {"timestamp": time.time(), "dialogs": [], "error": str(e)},
            )

    async def _resolve_chat_cache(self, raw_input: str):
        if not self.tg_service or not self.client_active:
            atomic_write_json(
                self.file_service.resolved_chat_path,
                {"timestamp": time.time(), "ok": False, "error": "Telegram 尚未連線"},
            )
            return
        try:
            res = await self.tg_service.resolve_chat_reference(raw_input)
            atomic_write_json(
                self.file_service.resolved_chat_path,
                {"timestamp": time.time(), "ok": True, "id": res["id"], "title": res["title"]},
            )
        except Exception as e:
            atomic_write_json(
                self.file_service.resolved_chat_path,
                {"timestamp": time.time(), "ok": False, "error": str(e)},
            )

    async def _restore_pending_tasks(self):
        """Restore unfinished tasks from previous run and clean orphan temp files."""
        saved = self.queue_service.load_persisted_pending()
        for entry in saved:
            msg_id = entry.get("msg_id")
            name = entry.get("name", "")
            size = int(entry.get("size", 0) or 0)
            chat_id_str = str(entry.get("chat_id", "me") or "me")
            prev_status = entry.get("status", "等待中")
            if not msg_id or not name:
                continue

            if prev_status == "⚠️ 加密隔離":
                local_zip = os.path.join(self.file_service.get_download_dir(), name)
                if os.path.exists(local_zip):
                    self.queue_service.add_item(
                        msg_id, name, size, chat_id=chat_id_str, status="⚠️ 加密隔離", persist=False
                    )
                    self.has_encrypted_warning = True
                continue

            chat_target = "me" if chat_id_str == "me" else (int(chat_id_str) if chat_id_str.lstrip("-").isdigit() else chat_id_str)
            msg_obj = await self.tg_service.get_message_by_id(msg_id, chat_target=chat_target)
            if msg_obj and getattr(msg_obj, "media", None):
                self.queue_service.add_item(
                    msg_id, name, size, chat_id=chat_id_str, status="等待中", persist=False
                )
                self.queue_service.update_item(msg_id, persist=False, message_obj=msg_obj)
            else:
                print(f"[*] 雲端訊息已不存在，略過恢復任務: {name} (ID: {msg_id})")

        self.queue_service.persist_state()
        removed = self.file_service.clean_orphan_temp_files(self.queue_service.get_retained_filenames())
        if removed:
            print(f"[*] 啟動清理完成：移除 {removed} 個孤兒暫存項目。")

    async def _sync_missed_messages(self):
        """Optionally catch up media messages received while app was closed."""
        chat_key = self.file_service.watch_chat_id
        last_id = self.file_service.get_last_seen_msg_id(chat_key)

        if last_id == 0:
            latest_id = await self.tg_service.get_latest_message_id()
            if latest_id > 0:
                self.file_service.update_last_seen_msg_id(chat_key, latest_id)
            return

        if not self.file_service.catch_up_missed:
            return

        missed = await self.tg_service.fetch_missed_media_messages(min_id=last_id, limit=50)
        for msg in missed:
            info = extract_media_info(msg, self.tg_service.max_size_bytes)
            if not info:
                continue
            filename, size = info
            if not self.queue_service.get_item(msg.id):
                await self.handle_new_file(msg.id, filename, size, msg)
            self.file_service.update_last_seen_msg_id(chat_key, msg.id)

    async def start_tg_client(self):
        await self.stop_tg_client()
        self.file_service.load_config()
        api_id, api_hash = self.file_service.get_credentials()
        if not api_id or not api_hash or not self.file_service.has_session_file():
            self.client_active = False
            self.file_service.clean_orphan_temp_files(self.queue_service.get_retained_filenames())
            return

        service = TelegramService(
            api_id=api_id,
            api_hash=api_hash,
            session_path=self.file_service.get_session_base_path(),
            log_path=self.file_service.log_path,
            watch_chat=self.file_service.get_watch_chat_target(),
            on_alert=lambda msg: self.notify(APP_DISPLAY_NAME, msg),
        )
        ok = await service.start()
        if ok:
            service.register_listener(self.handle_new_file)
            service.register_monitor_listener(self.handle_monitor_command)
            self.tg_service = service
            self.client_active = True
            await self._refresh_dialogs_cache()
            await self._restore_pending_tasks()
            await self._sync_missed_messages()
        else:
            await service.stop()
            self.tg_service = service
            self.client_active = False

    async def stop_tg_client(self):
        if self.tg_service is not None:
            await self.tg_service.stop()
            self.tg_service = None
        self.client_active = False

    def clear_abnormal_reports(self):
        self.has_encrypted_warning = False
        abnormal_statuses = {"⚠️ 加密隔離", "解壓/移轉失敗", "移轉失敗", "移轉驗證失敗", "下載失敗"}
        cleaned = 0
        for item in list(self.queue_service.get_all_items()):
            if item.get("status") in abnormal_statuses:
                msg_id = item["msg_id"]
                temp_path = os.path.join(self.file_service.get_download_dir(), item["name"])
                self.file_service.safe_delete(temp_path)
                self.file_service.safe_delete(temp_path + ".parts")
                self.queue_service.remove_item(msg_id, force_delete=True)
                cleaned += 1
        self.file_service.clean_unfinished_files()
        self.notify(APP_DISPLAY_NAME, f"已重設異常狀態並清理 {cleaned} 個暫存項目。")

    def clear_all_queue_and_history(self):
        self.has_encrypted_warning = False
        cleared = self.queue_service.clear_all(keep_downloading=True)
        self.file_service.clean_orphan_temp_files(self.queue_service.get_retained_filenames())
        self.notify(APP_DISPLAY_NAME, f"已一鍵清除 {cleared} 筆佇列與歷史紀錄。")

    async def handle_new_file(self, msg_id, filename, size, message_obj):
        chat_key = self.file_service.watch_chat_id
        print(f"[+] 偵測到新檔案 ({self.file_service.watch_chat_title}): {filename} ({size} Bytes, ID: {msg_id})")
        self.file_service.update_last_seen_msg_id(chat_key, msg_id)
        self.queue_service.add_item(msg_id, filename, size, chat_id=chat_key)
        self.queue_service.update_item(msg_id, persist=False, message_obj=message_obj)

    async def handle_monitor_command(self, message):
        active_items = self.queue_service.get_all_items()
        current = next((i for i in active_items if i.get("status") == "下載中"), None)
        free_gb = get_free_disk_space(self.file_service.get_download_dir())

        if current:
            bar = make_progress_bar(current.get("progress", 0.0), width=10)
            report = (
                "📊 **Telegram 下載器遠端監控**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📂 **目前下載**: `{current.get('name', '未知檔案')}`\n"
                f"📏 **檔案大小**: {format_size(current.get('size', 0))}\n"
                f"⚡ **下載進度**: {bar}\n"
                f"🚀 **傳輸速度**: {current.get('speed', '計算中...')}\n"
                f"⏳ **預估時間 (ETA)**: {current.get('eta', '計算中...')}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💾 **系統剩餘空間**: {free_gb:.2f} GB\n"
                f"📥 **佇列等待數量**: {self.queue_service.get_pending_count()} 個任務"
            )
        else:
            report = (
                "📊 **Telegram 下載器遠端監控**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🟢 **系統狀態**: 閒置 (Idle) — 目前無下載項目\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💾 **系統剩餘空間**: {free_gb:.2f} GB\n"
                f"📥 **佇列等待數量**: {self.queue_service.get_pending_count()} 個任務"
            )

        try:
            await message.respond(report)
        except Exception as e:
            print(f"[-] 回傳遠端監控字卡失敗: {e}")

    async def _process_commands(self):
        cmd_path = self.file_service.command_path
        if not os.path.exists(cmd_path):
            return
        cmd = read_json_safe(cmd_path, {})
        cmd_id = cmd.get("id", "")
        if not cmd_id or cmd_id == self.last_command_id:
            return
        self.last_command_id = cmd_id
        try:
            os.remove(cmd_path)
        except Exception:
            pass

        action = cmd.get("action")
        if action == "stop_client":
            await self.stop_tg_client()
        elif action == "start_client":
            await self.start_tg_client()
        elif action == "refresh_dialogs":
            await self._refresh_dialogs_cache()
        elif action == "resolve_chat":
            await self._resolve_chat_cache(str(cmd.get("payload", "")))
        elif action == "reload_config":
            old_chat = self.file_service.watch_chat_id
            old_catch_up = self.file_service.catch_up_missed
            self.file_service.load_config()
            if not self.client_active:
                await self.start_tg_client()
            elif self.tg_service:
                if self.file_service.watch_chat_id != old_chat:
                    self.tg_service.set_watch_chat(self.file_service.get_watch_chat_target())
                    await self._sync_missed_messages()
                elif not old_catch_up and self.file_service.catch_up_missed:
                    await self._sync_missed_messages()
        elif action == "clear_abnormal":
            self.clear_abnormal_reports()
        elif action == "clear_all_queue":
            self.clear_all_queue_and_history()

    async def queue_worker(self):
        while not self.stop_event.is_set():
            pending = [i for i in self.queue_service.get_all_items() if i["status"] == "等待中"]
            if not pending or not self.tg_service or not self.client_active:
                await asyncio.sleep(1)
                continue

            target = pending[0]
            msg_id = target["msg_id"]
            if not target.get("message_obj"):
                self.queue_service.update_item(msg_id, status="下載失敗", eta="--")
                continue

            async with self.queue_service.download_lock:
                self.queue_service.update_item(msg_id, status="下載中")
                self.file_service.ensure_dir_exists(self.file_service.get_download_dir())
                dest_path = os.path.join(self.file_service.get_download_dir(), target["name"])

                last_received = None
                last_time = time.time()

                def on_progress(received, total, eta):
                    nonlocal last_received, last_time
                    pct = (received / total) * 100 if total > 0 else 0.0
                    now = time.time()
                    if last_received is None:
                        last_received = received
                        last_time = now
                        speed_str = "計算中..."
                    else:
                        dt = now - last_time
                        if dt >= 0.5:
                            diff = max(0, received - last_received)
                            speed_str = f"{format_size(int(diff / dt))}/s"
                            last_received = received
                            last_time = now
                        else:
                            item = self.queue_service.get_item(msg_id)
                            speed_str = item.get("speed", "計算中...") if item else "計算中..."
                    self.queue_service.update_item(
                        msg_id, persist=False, progress=pct, eta=eta, received_bytes=received, speed=speed_str
                    )

                success = await self.tg_service.download_file(target["message_obj"], dest_path, on_progress)
                if not success:
                    self.queue_service.update_item(msg_id, status="下載失敗", eta="--")
                    continue

                shipping_dir = self.file_service.get_shipping_dir()
                while not self.file_service.is_dir_accessible(shipping_dir):
                    print(f"[-] 找不到目標磁碟機或資料夾 {shipping_dir}，彈出視窗要求使用者重新指定...")
                    self.queue_service.update_item(msg_id, status="等待指定目標資料夾", eta="--")
                    self.notify(APP_DISPLAY_NAME, f"找不到磁碟機或資料夾：{shipping_dir}，請重新指定。")
                    shipping_dir = await self.prompt_required_folder_async()

                is_zip = target["name"].lower().endswith(".zip") and self.file_service.auto_unzip
                final_dest = ""

                if is_zip:
                    is_encrypted = False
                    try:
                        with zipfile.ZipFile(dest_path) as zf:
                            for info in zf.infolist():
                                if info.flag_bits & 0x1:
                                    is_encrypted = True
                                    break
                    except Exception:
                        is_encrypted = True

                    if is_encrypted:
                        self.has_encrypted_warning = True
                        self.queue_service.update_item(msg_id, status="⚠️ 加密隔離", eta="--")
                        self.notify(APP_DISPLAY_NAME, f"發現加密 ZIP 檔，已隔離保留：{target['name']}")
                        continue

                    folder_name = os.path.splitext(target["name"])[0]
                    extract_dir = os.path.join(self.file_service.get_download_dir(), folder_name)
                    try:
                        if os.path.exists(extract_dir):
                            self.file_service.safe_delete(extract_dir)
                        with zipfile.ZipFile(dest_path) as zf:
                            zf.extractall(extract_dir)
                        dest_folder = os.path.join(shipping_dir, folder_name)
                        if os.path.abspath(extract_dir) != os.path.abspath(dest_folder):
                            if os.path.exists(dest_folder):
                                self.file_service.safe_delete(dest_folder)
                            shutil.move(extract_dir, dest_folder)
                        final_dest = dest_folder
                    except Exception as e:
                        print(f"[-] ZIP 解壓縮或搬移失敗 {target['name']}: {e}")
                        self.queue_service.update_item(msg_id, status="解壓/移轉失敗", eta="--")
                        continue
                else:
                    try:
                        dest_file = os.path.join(shipping_dir, target["name"])
                        if os.path.abspath(dest_path) != os.path.abspath(dest_file):
                            if os.path.exists(dest_file):
                                self.file_service.safe_delete(dest_file)
                            shutil.move(dest_path, dest_file)
                        final_dest = dest_file
                    except Exception as e:
                        print(f"[-] 搬移失敗 {target['name']}: {e}")
                        self.queue_service.update_item(msg_id, status="移轉失敗", eta="--")
                        continue

                expected_size = -1 if is_zip else target["size"]
                if self.file_service.verify_moved_file(final_dest, expected_size):
                    chat_id_str = str(target.get("chat_id", "me") or "me")
                    chat_target = "me" if chat_id_str == "me" else (int(chat_id_str) if chat_id_str.lstrip("-").isdigit() else chat_id_str)
                    if self.file_service.delete_after_transfer and self.tg_service:
                        deleted = await self.tg_service.delete_message(msg_id, chat_target=chat_target)
                        status_label = "已完成並清理" if deleted else "已完成移轉(無刪除權限)"
                    else:
                        status_label = "已完成移轉"

                    if is_zip and os.path.exists(dest_path):
                        self.file_service.safe_delete(dest_path)

                    self.queue_service.mark_completed(msg_id, status_label=status_label)
                    self.queue_service.remove_item(msg_id)
                else:
                    self.queue_service.update_item(msg_id, status="移轉驗證失敗", eta="--")

    async def status_and_command_loop(self):
        while not self.stop_event.is_set():
            await self._process_commands()
            self._write_status_snapshot()
            await asyncio.sleep(0.5)

    async def async_main(self):
        self.stop_event = asyncio.Event()
        await self.start_tg_client()
        self._write_status_snapshot()
        await asyncio.gather(
            self.queue_worker(),
            self.status_and_command_loop(),
        )

    def start_background_loop(self):
        self.loop = asyncio.new_event_loop()

        def runner():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.async_main())

        t = threading.Thread(target=runner, daemon=True)
        t.start()

    def stop_all(self):
        if self.loop and self.stop_event:
            async def _shutdown():
                self.stop_event.set()
                await self.stop_tg_client()

            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        if self.tray_icon:
            self.tray_icon.stop()

    def _tray_status_label(self, _item) -> str:
        if self.tg_service and self.client_active and self.tg_service.account_label:
            return f"狀態: 已連線 {self.tg_service.account_label}"
        api_id, api_hash = self.file_service.get_credentials()
        if not api_id or not api_hash or not self.file_service.has_session_file():
            return "狀態: 尚未設定帳號（請點「控制面板」）"
        return "狀態: 未連線"

    def _tray_queue_label(self, _item) -> str:
        active = self.queue_service.get_all_items()
        downloading = next((i for i in active if i["status"] == "下載中"), None)
        if downloading:
            return f"下載中: {downloading['name']} ({downloading['progress']:.1f}%)"
        return f"佇列等待中: {self.queue_service.get_pending_count()} 個檔案"

    def run_tray(self):
        self.start_background_loop()

        api_id, api_hash = self.file_service.get_credentials()
        needs_setup = not api_id or not api_hash or not self.file_service.has_session_file()
        if needs_setup:
            self.launch_gui("account")
        elif not self.file_service.is_dir_accessible(self.file_service.get_shipping_dir()):
            self._spawn_ui_subprocess(["--pick-required-folder"])

        menu = pystray.Menu(
            pystray.MenuItem(self._tray_status_label, None, enabled=False),
            pystray.MenuItem(self._tray_queue_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("開啟控制面板 / 帳號與對話框設定...", lambda icon, item: self.launch_gui("account")),
            pystray.MenuItem("對話框與同步資料夾設定...", lambda icon, item: self.launch_gui("settings")),
            pystray.MenuItem("查看下載佇列與狀態...", lambda icon, item: self.launch_gui("status")),
            pystray.MenuItem("一鍵清除佇列與歷史", lambda icon, item: self.clear_all_queue_and_history()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("結束程式", lambda icon, item: self.stop_all()),
        )

        initial_state = self._current_icon_state()
        self._last_icon_state = initial_state
        self.tray_icon = pystray.Icon(
            "TGAutoDownloader",
            create_tray_icon_image(initial_state),
            APP_DISPLAY_NAME,
            menu,
        )
        self.tray_icon.run()


def _is_another_daemon_running(file_service: FileService) -> bool:
    status = read_json_safe(file_service.status_path, {})
    ts = float(status.get("timestamp", 0) or 0)
    pid = status.get("pid")
    if time.time() - ts <= 3.0 and isinstance(pid, int) and pid != os.getpid():
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description=APP_DISPLAY_NAME)
    parser.add_argument("--gui", action="store_true", help="Open control panel GUI window")
    parser.add_argument("--pick-required-folder", action="store_true", help="Prompt user to choose an accessible destination folder")
    parser.add_argument("--tab", default="account", choices=["account", "settings", "status"])
    args = parser.parse_args()

    if args.pick_required_folder:
        from gui_app import run_required_folder_picker
        run_required_folder_picker()
        return

    if args.gui:
        from gui_app import run_gui
        run_gui(initial_tab=args.tab)
        return

    file_service = FileService()
    if _is_another_daemon_running(file_service):
        from gui_app import run_gui
        run_gui(initial_tab="status")
        return

    controller = DaemonController()
    controller.run_tray()


if __name__ == "__main__":
    main()
