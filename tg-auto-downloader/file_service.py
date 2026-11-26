"""Per-user configuration, temp isolation, orphan cleanup, and file verification."""

import os
import shutil
import time
from typing import Any, Dict, Optional, Set, Tuple, Union

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from platform_utils import atomic_write_json, get_app_data_dir, read_json_safe


class FileService:
    """Manages persistent settings, isolated temp downloads, and destination verification."""

    def __init__(
        self,
        default_download_dir: str = "~/Downloads/TelegramAuto/_temp",
        default_shipping_dir: str = "~/Downloads/TelegramAuto",
    ):
        self.app_data_dir = get_app_data_dir()
        self.config_path = os.path.join(self.app_data_dir, "config.json")
        self.status_path = os.path.join(self.app_data_dir, "status.json")
        self.command_path = os.path.join(self.app_data_dir, "command.json")
        self.queue_state_path = os.path.join(self.app_data_dir, "pending_queue.json")
        self.app_state_path = os.path.join(self.app_data_dir, "app_state.json")
        self.dialogs_path = os.path.join(self.app_data_dir, "dialogs.json")
        self.resolved_chat_path = os.path.join(self.app_data_dir, "resolved_chat.json")
        self.session_base_path = os.path.join(self.app_data_dir, "auto_downloader_session")
        self.log_path = os.path.join(self.app_data_dir, "downloader.log")

        self.default_download_dir = os.path.abspath(os.path.expanduser(default_download_dir))
        self.default_shipping_dir = os.path.abspath(os.path.expanduser(default_shipping_dir))

        self.tg_api_id: str = ""
        self.tg_api_hash: str = ""
        self.download_dir: str = self.default_download_dir
        self.shipping_dir: str = self.default_shipping_dir
        self.auto_unzip: bool = True
        self.delete_after_transfer: bool = True
        self.catch_up_missed: bool = False
        self.watch_chat_id: str = "me"
        self.watch_chat_title: str = "我的收藏 (Saved Messages)"

        self.load_config()
        try:
            self.ensure_dir_exists(self.download_dir)
        except Exception:
            pass

    def ensure_dir_exists(self, path: str):
        if path and not os.path.exists(path):
            os.makedirs(path, exist_ok=True)

    def load_config(self):
        """Load user settings from per-user config.json with safe defaults."""
        load_dotenv()
        data = read_json_safe(self.config_path, {})

        env_api_id = (os.getenv("TG_API_ID") or "").strip()
        env_api_hash = (os.getenv("TG_API_HASH") or "").strip()
        if env_api_id == "your_api_id_here":
            env_api_id = ""
        if env_api_hash == "your_api_hash_here":
            env_api_hash = ""

        self.tg_api_id = str(data.get("tg_api_id", "") or env_api_id).strip()
        self.tg_api_hash = str(data.get("tg_api_hash", "") or env_api_hash).strip()

        raw_dl = str(data.get("download_dir", "")).strip()
        self.download_dir = os.path.abspath(os.path.expanduser(raw_dl)) if raw_dl else self.default_download_dir

        raw_ship = str(data.get("shipping_dir", "")).strip()
        self.shipping_dir = os.path.abspath(os.path.expanduser(raw_ship)) if raw_ship else self.default_shipping_dir

        self.auto_unzip = bool(data.get("auto_unzip", True))
        self.delete_after_transfer = bool(data.get("delete_after_transfer", True))
        self.catch_up_missed = bool(data.get("catch_up_missed", False))
        self.watch_chat_id = str(data.get("watch_chat_id", "me") or "me").strip()
        self.watch_chat_title = str(
            data.get("watch_chat_title", "我的收藏 (Saved Messages)") or "我的收藏 (Saved Messages)"
        ).strip()

    def save_config(self):
        """Persist current configuration atomically to config.json."""
        payload: Dict[str, Any] = {
            "tg_api_id": self.tg_api_id,
            "tg_api_hash": self.tg_api_hash,
            "download_dir": self.download_dir,
            "shipping_dir": self.shipping_dir,
            "auto_unzip": self.auto_unzip,
            "delete_after_transfer": self.delete_after_transfer,
            "catch_up_missed": self.catch_up_missed,
            "watch_chat_id": self.watch_chat_id,
            "watch_chat_title": self.watch_chat_title,
        }
        atomic_write_json(self.config_path, payload)

    def get_watch_chat_target(self) -> Union[str, int]:
        """Return 'me' or integer chat_id for Telethon."""
        if not self.watch_chat_id or self.watch_chat_id == "me":
            return "me"
        try:
            return int(self.watch_chat_id)
        except ValueError:
            return self.watch_chat_id

    def get_last_seen_msg_id(self, chat_key: str) -> int:
        state = read_json_safe(self.app_state_path, {})
        mapping = state.get("last_seen_msg_ids", {})
        if isinstance(mapping, dict):
            try:
                return int(mapping.get(str(chat_key), 0) or 0)
            except (ValueError, TypeError):
                return 0
        return 0

    def update_last_seen_msg_id(self, chat_key: str, msg_id: int):
        state = read_json_safe(self.app_state_path, {})
        mapping = state.get("last_seen_msg_ids")
        if not isinstance(mapping, dict):
            mapping = {}
        prev = int(mapping.get(str(chat_key), 0) or 0)
        if msg_id > prev:
            mapping[str(chat_key)] = int(msg_id)
            state["last_seen_msg_ids"] = mapping
            atomic_write_json(self.app_state_path, state)

    def get_credentials(self) -> Tuple[Optional[int], Optional[str]]:
        if not self.tg_api_id or not self.tg_api_hash:
            return None, None
        try:
            return int(self.tg_api_id), self.tg_api_hash
        except ValueError:
            return None, None

    def set_credentials(self, api_id: str, api_hash: str) -> Tuple[bool, str]:
        clean_id = api_id.strip()
        clean_hash = api_hash.strip()
        if not clean_id or not clean_hash:
            return False, "API ID 與 API Hash 不可為空。"
        try:
            parsed_id = int(clean_id)
            if parsed_id <= 0:
                raise ValueError
        except ValueError:
            return False, "API ID 必須為正整數。"

        self.tg_api_id = str(parsed_id)
        self.tg_api_hash = clean_hash
        self.save_config()
        return True, "API 憑證已儲存。"

    def get_session_base_path(self) -> str:
        return self.session_base_path

    def has_session_file(self) -> bool:
        return os.path.exists(self.session_base_path + ".session")

    def delete_session_files(self):
        for suffix in (".session", ".session-journal"):
            target = self.session_base_path + suffix
            if os.path.exists(target):
                try:
                    os.remove(target)
                except Exception:
                    pass

    def get_download_dir(self) -> str:
        return self.download_dir

    def _test_writable(self, target_dir: str) -> Tuple[bool, str]:
        expanded = os.path.abspath(os.path.expanduser(target_dir))
        try:
            self.ensure_dir_exists(expanded)
            probe = os.path.join(expanded, ".write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return True, expanded
        except Exception as e:
            return False, str(e)

    def is_dir_accessible(self, target_dir: str) -> bool:
        if not target_dir:
            return False
        ok, _ = self._test_writable(target_dir)
        return ok

    def set_download_dir(self, new_path: str) -> bool:
        ok, result = self._test_writable(new_path)
        if not ok:
            print(f"[-] 無法設定下載暫存目錄 {new_path}: {result}")
            return False
        self.download_dir = result
        self.save_config()
        return True

    def get_shipping_dir(self) -> str:
        return self.shipping_dir

    def set_shipping_dir(self, new_path: str) -> bool:
        ok, result = self._test_writable(new_path)
        if not ok:
            print(f"[-] 無法設定上傳目標資料夾 {new_path}: {result}")
            return False
        self.shipping_dir = result
        self.save_config()
        return True

    def clean_unfinished_files(self):
        if not os.path.exists(self.download_dir):
            return
        for entry in os.listdir(self.download_dir):
            if entry.endswith(".temp") or entry.endswith(".part"):
                self.safe_delete(os.path.join(self.download_dir, entry))

    def clean_orphan_temp_files(self, keep_filenames: Set[str]) -> int:
        """
        Clean up files/folders in download_dir that do not belong to any retained/quarantined task.
        Only operates inside download_dir (temp folder) so finished files in shipping_dir stay untouched.
        """
        if not os.path.exists(self.download_dir):
            return 0
        # Safety guard: if user manually set download_dir == shipping_dir, only clean .parts / .temp / .part
        same_as_shipping = os.path.abspath(self.download_dir) == os.path.abspath(self.shipping_dir)
        removed = 0
        for entry in os.listdir(self.download_dir):
            full_path = os.path.join(self.download_dir, entry)
            base_name = entry[:-6] if entry.endswith(".parts") else entry
            if base_name in keep_filenames:
                continue
            if same_as_shipping and not (entry.endswith(".parts") or entry.endswith(".part") or entry.endswith(".temp")):
                continue
            self.safe_delete(full_path)
            removed += 1
        return removed

    def verify_moved_file(self, target_path: str, expected_size: int = -1) -> bool:
        if not os.path.exists(target_path):
            return False
        if os.path.isdir(target_path):
            time.sleep(2)
            return os.path.exists(target_path)

        size_first = os.path.getsize(target_path)
        if expected_size >= 0 and size_first != expected_size:
            return False

        time.sleep(2)
        if not os.path.exists(target_path):
            return False

        return os.path.getsize(target_path) == size_first

    def safe_delete(self, path: str):
        if not path or not os.path.exists(path):
            return
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as e:
            print(f"[-] 清理本地快取失敗 {path}: {e}")
