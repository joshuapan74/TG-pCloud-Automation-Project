"""Cross-platform Telegram MTProto client service supporting custom chats, deletion detection, and parallel downloads."""

import asyncio
import ctypes
import glob
import logging
import os
import re
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import aiofiles
from telethon import TelegramClient, events
from telethon.errors import FileReferenceExpiredError, MediaEmptyError, MessageIdInvalidError
from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
)

CHUNK_SIZE = 512 * 1024                  # 512 KB (MTProto max request_size)
MULTI_PART_THRESHOLD = 50 * 1024 * 1024  # >= 50 MB uses 4-worker parallel download
NUM_WORKERS = 4
MAX_RETRY_PER_PART = 3
WATCHDOG_TIMEOUT = 60

MESSAGE_CHECK_ERROR = object()


def enable_fast_telethon_crypto() -> str:
    """
    Bind Python/OS bundled OpenSSL libcrypto AES-IGE functions into telethon.crypto.libssl
    when cryptg and system libssl are unavailable, avoiding slow pure-Python pyaes loops.
    """
    try:
        from telethon.crypto import aes, libssl
    except Exception:
        return "unavailable"

    if getattr(aes, "cryptg", None) is not None:
        return "cryptg"
    if getattr(libssl, "decrypt_ige", None) is not None and getattr(libssl, "encrypt_ige", None) is not None:
        return "libssl"

    search_dirs: List[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        search_dirs.append(meipass)
    try:
        import _ssl
        search_dirs.append(os.path.dirname(_ssl.__file__))
    except Exception:
        pass
    search_dirs.extend([
        os.path.join(sys.base_prefix, "DLLs"),
        os.path.join(sys.base_prefix, "lib"),
        os.path.dirname(sys.executable),
    ])
    if sys.platform == "darwin":
        search_dirs.extend([
            "/opt/homebrew/opt/openssl@3/lib",
            "/usr/local/opt/openssl@3/lib",
            "/opt/homebrew/lib",
            "/usr/local/lib",
        ])

    patterns = ["libcrypto-3*.dll", "libcrypto*.dll"] if sys.platform == "win32" else (
        ["libcrypto*.dylib"] if sys.platform == "darwin" else ["libcrypto.so*"]
    )

    seen = set()
    candidates: List[str] = []
    for d in search_dirs:
        if not d or d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        for pat in patterns:
            candidates.extend(sorted(glob.glob(os.path.join(d, pat))))

    class _AESKey(ctypes.Structure):
        _fields_ = [("rd_key", ctypes.c_uint32 * 60), ("rounds", ctypes.c_uint)]

    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
            if not (
                hasattr(lib, "AES_set_encrypt_key")
                and hasattr(lib, "AES_set_decrypt_key")
                and hasattr(lib, "AES_ige_encrypt")
            ):
                continue

            lib.AES_set_encrypt_key.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_AESKey)]
            lib.AES_set_encrypt_key.restype = ctypes.c_int
            lib.AES_set_decrypt_key.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_AESKey)]
            lib.AES_set_decrypt_key.restype = ctypes.c_int
            lib.AES_ige_encrypt.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_size_t,
                ctypes.POINTER(_AESKey),
                ctypes.c_char_p,
                ctypes.c_int,
            ]
            lib.AES_ige_encrypt.restype = None

            def _make_encrypt(clibrary):
                def _encrypt_ige(plain_text: bytes, key: bytes, iv: bytes) -> bytes:
                    pad = len(plain_text) % 16
                    if pad:
                        plain_text += os.urandom(16 - pad)
                    ak = _AESKey()
                    clibrary.AES_set_encrypt_key(key, 8 * len(key), ctypes.byref(ak))
                    iv_buf = ctypes.create_string_buffer(iv, len(iv))
                    out_buf = ctypes.create_string_buffer(len(plain_text))
                    clibrary.AES_ige_encrypt(plain_text, out_buf, len(plain_text), ctypes.byref(ak), iv_buf, 1)
                    return out_buf.raw
                return _encrypt_ige

            def _make_decrypt(clibrary):
                def _decrypt_ige(cipher_text: bytes, key: bytes, iv: bytes) -> bytes:
                    ak = _AESKey()
                    clibrary.AES_set_decrypt_key(key, 8 * len(key), ctypes.byref(ak))
                    iv_buf = ctypes.create_string_buffer(iv, len(iv))
                    out_buf = ctypes.create_string_buffer(len(cipher_text))
                    clibrary.AES_ige_encrypt(cipher_text, out_buf, len(cipher_text), ctypes.byref(ak), iv_buf, 0)
                    return out_buf.raw
                return _decrypt_ige

            enc_fn = _make_encrypt(lib)
            dec_fn = _make_decrypt(lib)

            # Quick roundtrip sanity check before binding globally
            test_key = b"0123456789abcdef0123456789abcdef"
            test_iv = b"fedcba9876543210fedcba9876543210"
            test_msg = b"TGAutoDownloader"
            if dec_fn(enc_fn(test_msg, test_key, test_iv), test_key, test_iv) != test_msg:
                continue

            libssl.encrypt_ige = enc_fn
            libssl.decrypt_ige = dec_fn
            return f"libcrypto:{os.path.basename(path)}"
        except Exception:
            continue

    return "pyaes"


CRYPTO_BACKEND = enable_fast_telethon_crypto()


def setup_logger(log_path: str) -> logging.Logger:
    """Configure logger writing to per-user app data log file and stdout."""
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    logger = logging.getLogger("TGAutoDownloader")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        if sys.stdout is not None:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)
    return logger


def _extract_photo_size(photo) -> int:
    sizes = getattr(photo, "sizes", None) or []
    for candidate in reversed(sizes):
        direct = getattr(candidate, "size", None)
        if isinstance(direct, int) and direct > 0:
            return direct
        prog = getattr(candidate, "sizes", None)
        if isinstance(prog, list) and prog:
            return int(prog[-1])
    return 0


def parse_chat_reference(raw: str) -> Union[str, int]:
    """Parse user-pasted chat ID, @username, or t.me link into a Telethon-friendly target."""
    text = (raw or "").strip()
    if not text or text.lower() in ("me", "saved", "我的收藏"):
        return "me"

    m_private = re.search(r"t\.me/c/(\d+)(?:/\d+)?", text, re.IGNORECASE)
    if m_private:
        return int(f"-100{m_private.group(1)}")

    m_public = re.search(r"t\.me/([A-Za-z0-9_]{3,})(?:/\d+)?", text, re.IGNORECASE)
    if m_public:
        slug = m_public.group(1)
        if slug.startswith("+"):
            raise ValueError("不支援私有邀請連結，請改貼該群組任一訊息連結 (t.me/c/...) 或數字 Chat ID。")
        return f"@{slug}"

    if text.lstrip("-").isdigit():
        return int(text)

    if text.startswith("@"):
        return text
    return f"@{text}"


def extract_media_info(message, max_size_bytes: int = 4 * 1024 * 1024 * 1024) -> Optional[Tuple[str, int]]:
    """Extract (filename, size) if message carries a supported Document or Photo within limits."""
    if not message or not getattr(message, "media", None):
        return None

    if isinstance(message.media, MessageMediaDocument):
        document = message.media.document
        size = document.size
        filename = f"file_{message.id}"
        for attr in document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
                break
    elif isinstance(message.media, MessageMediaPhoto):
        size = _extract_photo_size(message.media.photo)
        timestamp = message.date.strftime("%Y%m%d_%H%M%S")
        filename = f"photo_{timestamp}_{message.id}.jpg"
    else:
        return None

    if size > max_size_bytes:
        return None
    return filename, size


class TelegramService:
    """Handles Telegram chat listening, deletion events, dialog listing, and parallel downloading."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: str,
        log_path: str,
        watch_chat: Union[str, int] = "me",
        on_alert: Optional[Callable[[str], None]] = None,
        max_size_bytes: int = 4 * 1024 * 1024 * 1024,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.log = setup_logger(log_path)
        self.watch_chat: Union[str, int] = watch_chat if watch_chat else "me"
        self.on_alert = on_alert
        self.max_size_bytes = max_size_bytes

        self.on_monitor_cmd = None
        self.on_new_file_callback = None
        self.on_deleted_callback = None
        self.liveness_task = None
        self.account_label = ""
        self.last_error = ""
        self._handler_ref = None
        self._delete_handler_ref = None

        self.log.info(f"=== TG Auto Downloader 啟動 (api_id={api_id}, watch_chat={self.watch_chat}, crypto={CRYPTO_BACKEND}) ===")
        self.client = self._build_client()

    def _build_client(self) -> TelegramClient:
        return TelegramClient(
            self.session_path,
            self.api_id,
            self.api_hash,
            connection_retries=None,
            retry_delay=5,
            request_retries=10,
        )

    def _notify_alert(self, message: str):
        self.last_error = message
        if self.on_alert:
            try:
                self.on_alert(message)
            except Exception:
                pass

    async def start(self) -> bool:
        """Connect using existing authorized session without blocking on terminal input."""
        self.log.info("正在連線至 Telegram...")
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                self.log.warning("Telegram 尚未登入授權，請透過設定介面登入。")
                await self.client.disconnect()
                self.account_label = ""
                return False

            me = await self.client.get_me()
            username = f"@{me.username}" if me.username else (me.first_name or "User")
            self.account_label = f"{username} (ID: {me.id})"
            self.last_error = ""
            self.log.info(f"✓ Telegram 連線成功！登入帳號: {self.account_label}")

            try:
                await self.client.get_dialogs(limit=80)
            except Exception:
                pass

            self.liveness_task = asyncio.create_task(self._liveness_check_loop())
            return True
        except Exception as e:
            self.log.error(f"✗ Telegram 連線失敗: {e}")
            self._notify_alert(f"Telegram 連線失敗: {e}")
            return False

    async def stop(self):
        self.log.info("正在中斷 Telegram 連線...")
        if self.liveness_task:
            self.liveness_task.cancel()
            try:
                await self.liveness_task
            except asyncio.CancelledError:
                pass
            self.liveness_task = None

        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.account_label = ""

    async def _reconnect_client(self):
        self.log.warning("偵測到連線異常，正在重新建立 Telegram 連線...")
        try:
            await self.client.disconnect()
        except Exception as e:
            self.log.error(f"斷開舊連線時發生錯誤: {e}")

        self.client = self._build_client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Session 已失效，請重新登入。")

        me = await self.client.get_me()
        username = f"@{me.username}" if me.username else (me.first_name or "User")
        self.account_label = f"{username} (ID: {me.id})"
        self.log.info(f"✓ Reconnect 成功！帳號: {self.account_label}")

        try:
            await self.client.get_dialogs(limit=80)
        except Exception:
            pass

        if self.on_new_file_callback:
            self._bind_listener()
        if self.on_deleted_callback:
            self._bind_delete_listener()

    async def _liveness_check_loop(self):
        self.log.info("背景連線活性檢查已啟動。")
        while True:
            try:
                await asyncio.sleep(180)
                if not self.client.is_connected():
                    self.log.warning("[活性檢查] 連線中斷，觸發重連。")
                    await self._reconnect_client()
                    continue
                await asyncio.wait_for(self.client.get_me(), timeout=15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"[活性檢查] 連線異常: {e}")
                try:
                    await self._reconnect_client()
                except Exception as re_err:
                    self.log.error(f"[活性檢查] 重連失敗: {re_err}")

    def set_watch_chat(self, watch_chat: Union[str, int]):
        self.watch_chat = watch_chat if watch_chat else "me"
        self.log.info(f"切換監聽對話框為: {self.watch_chat}")
        if self.on_new_file_callback:
            self._bind_listener()

    def register_listener(self, on_new_file):
        self.on_new_file_callback = on_new_file
        self._bind_listener()

    def register_delete_listener(self, on_deleted):
        self.on_deleted_callback = on_deleted
        self._bind_delete_listener()

    def register_monitor_listener(self, on_monitor_cmd):
        self.on_monitor_cmd = on_monitor_cmd
        self.log.info("已註冊遠端監控指令監聽器 (/回報)")

    def _bind_listener(self):
        if self._handler_ref is not None:
            try:
                self.client.remove_event_handler(self._handler_ref)
            except Exception:
                pass
            self._handler_ref = None

        chat_filter = "me" if self.watch_chat == "me" else [self.watch_chat]
        self.log.info(f"已綁定對話框監聽器 (chats={chat_filter})")

        @self.client.on(events.NewMessage(chats=chat_filter))
        async def handler(event):
            message = event.message

            if message.message and message.message.strip().startswith("/回報"):
                self.log.info(f"偵測到遠端監控指令 /回報 (ID={message.id})")
                if self.on_monitor_cmd:
                    await self.on_monitor_cmd(message)
                return

            info = extract_media_info(message, self.max_size_bytes)
            if not info:
                return
            filename, size = info
            self.log.info(f"偵測到新檔案: {filename} ({size / (1024 * 1024):.2f} MB, ID={message.id})")
            if self.on_new_file_callback:
                await self.on_new_file_callback(message.id, filename, size, message)

        self._handler_ref = handler

    def _bind_delete_listener(self):
        if self._delete_handler_ref is not None:
            try:
                self.client.remove_event_handler(self._delete_handler_ref)
            except Exception:
                pass
            self._delete_handler_ref = None

        @self.client.on(events.MessageDeleted())
        async def delete_handler(event):
            deleted_ids = list(getattr(event, "deleted_ids", []) or [])
            if deleted_ids and self.on_deleted_callback:
                await self.on_deleted_callback(deleted_ids)

        self._delete_handler_ref = delete_handler

    async def fetch_dialogs(self, limit: int = 80) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = [
            {
                "id": "me",
                "title": "我的收藏 (Saved Messages)",
                "kind": "我的收藏",
            }
        ]
        me = await self.client.get_me()
        my_id = getattr(me, "id", None)

        async for dialog in self.client.iter_dialogs(limit=limit):
            if my_id is not None and dialog.id == my_id:
                continue
            if dialog.is_channel:
                kind = "群組" if dialog.is_group else "頻道"
            elif dialog.is_group:
                kind = "群組"
            else:
                kind = "私人對話"
            title = (dialog.name or str(dialog.id)).strip()
            results.append({
                "id": str(dialog.id),
                "title": title,
                "kind": kind,
            })
        return results

    async def resolve_chat_reference(self, raw_input: str) -> Dict[str, str]:
        parsed = parse_chat_reference(raw_input)
        if parsed == "me":
            return {"id": "me", "title": "我的收藏 (Saved Messages)"}

        entity = await self.client.get_entity(parsed)
        from telethon.utils import get_peer_id
        peer_id = get_peer_id(entity)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(peer_id)
        username = getattr(entity, "username", None)
        if username:
            title = f"{title} (@{username})"
        return {"id": str(peer_id), "title": title}

    async def get_message_by_id(self, msg_id: int, chat_target: Optional[Union[str, int]] = None):
        target = chat_target if chat_target is not None else self.watch_chat
        try:
            return await self.client.get_messages(target, ids=msg_id)
        except Exception as e:
            self.log.warning(f"無法取回 Telegram 訊息 (chat={target}, ID={msg_id}): {e}")
            return None

    async def check_message_exists(self, msg_id: int, chat_target: Optional[Union[str, int]] = None) -> Union[bool, object]:
        """Return True if message still exists with media, False if definitively deleted, or MESSAGE_CHECK_ERROR."""
        target = chat_target if chat_target is not None else self.watch_chat
        try:
            msg = await asyncio.wait_for(self.client.get_messages(target, ids=msg_id), timeout=10)
            if msg is None or not getattr(msg, "media", None):
                return False
            return True
        except (MessageIdInvalidError, MediaEmptyError):
            return False
        except Exception:
            return MESSAGE_CHECK_ERROR

    async def get_latest_message_id(self, chat_target: Optional[Union[str, int]] = None) -> int:
        target = chat_target if chat_target is not None else self.watch_chat
        try:
            msgs = await self.client.get_messages(target, limit=1)
            if msgs:
                return int(msgs[0].id)
        except Exception as e:
            self.log.warning(f"無法取得最新訊息 ID (chat={target}): {e}")
        return 0

    async def fetch_missed_media_messages(self, min_id: int, limit: int = 50) -> List[Any]:
        collected = []
        try:
            async for msg in self.client.iter_messages(
                self.watch_chat,
                min_id=min_id,
                reverse=True,
                limit=limit,
            ):
                if extract_media_info(msg, self.max_size_bytes):
                    collected.append(msg)
        except Exception as e:
            self.log.warning(f"掃描離線期間新訊息失敗: {e}")
        return collected

    async def download_file(self, message, dest_path: str, progress_callback) -> bool:
        """Select single-stream or 4-worker parallel download; propagates CancelledError cleanly."""
        try:
            media = message.media
            if isinstance(media, MessageMediaDocument):
                total_size = media.document.size
            elif isinstance(media, MessageMediaPhoto):
                total_size = _extract_photo_size(media.photo)
            else:
                total_size = 0

            use_multipart = (
                total_size >= MULTI_PART_THRESHOLD
                and isinstance(media, MessageMediaDocument)
            )
            engine = "4 路並行引擎" if use_multipart else "單一串流"
            self.log.info(f"開始下載 [{engine}]: {os.path.basename(dest_path)} ({total_size / (1024 * 1024):.1f} MB)")

            if use_multipart:
                return await self._multipart_download(message, dest_path, total_size, progress_callback)
            return await self._single_stream_download(message, dest_path, total_size, progress_callback)
        except asyncio.CancelledError:
            raise
        except (MessageIdInvalidError, MediaEmptyError, FileReferenceExpiredError) as e:
            self.log.warning(f"下載來源訊息失效或已被刪除 (ID {message.id}): {e}")
            return False
        except Exception as e:
            self.log.error(f"下載失敗 (ID {message.id}): {e}", exc_info=True)
            return False

    async def _single_stream_download(self, message, dest_path: str, total_size: int, progress_callback) -> bool:
        existing_size = 0
        if os.path.exists(dest_path):
            existing_size = os.path.getsize(dest_path)
            if existing_size >= total_size > 0:
                progress_callback(total_size, total_size, "0s")
                return True

        start_time = time.time()
        received_tot = existing_size
        last_prog_t = time.time()

        async def do_download():
            nonlocal received_tot, last_prog_t
            mode = "ab" if existing_size > 0 else "wb"
            async with aiofiles.open(dest_path, mode) as f:
                async for chunk in self.client.iter_download(
                    message.media,
                    offset=existing_size,
                    request_size=CHUNK_SIZE,
                ):
                    await f.write(chunk)
                    received_tot += len(chunk)
                    last_prog_t = time.time()

                    elapsed = time.time() - start_time
                    speed = (received_tot - existing_size) / max(elapsed, 0.001)
                    remaining = max(total_size - received_tot, 0)
                    eta = f"{int(remaining / speed)}s" if speed > 0 else "計算中..."
                    progress_callback(received_tot, total_size, eta)

        ok = await self._run_with_watchdog(do_download, lambda: last_prog_t, message.id)
        if ok and total_size > 0:
            for attempt in range(3):
                final_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                if final_size == total_size:
                    return True
                self.log.warning(f"整檔驗證第 {attempt + 1}/3 次未通過 ({final_size}/{total_size})，2s 後複檢...")
                await asyncio.sleep(2)
            return False
        return ok

    async def _multipart_download(self, message, dest_path: str, total_size: int, progress_callback) -> bool:
        part_size = (total_size + NUM_WORKERS - 1) // NUM_WORKERS
        parts_dir = dest_path + ".parts"
        os.makedirs(parts_dir, exist_ok=True)

        progress_lock = asyncio.Lock()
        received_per_part = [0] * NUM_WORKERS
        last_prog_t = time.time()
        start_time = time.time()
        last_reported = 0

        for i in range(NUM_WORKERS):
            part_path = os.path.join(parts_dir, f"part{i:02d}")
            if os.path.exists(part_path):
                received_per_part[i] = os.path.getsize(part_path)
        total_already = sum(received_per_part)

        async def download_part(part_idx: int):
            nonlocal last_prog_t, last_reported
            offset_start = part_idx * part_size
            part_limit = min(part_size, total_size - offset_start)
            part_path = os.path.join(parts_dir, f"part{part_idx:02d}")

            already_in_part = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if already_in_part >= part_limit:
                async with progress_lock:
                    received_per_part[part_idx] = part_limit
                return

            current_offset = offset_start + already_in_part
            remaining_limit = part_limit - already_in_part

            for attempt in range(MAX_RETRY_PER_PART):
                try:
                    async with aiofiles.open(part_path, "ab") as f:
                        async for chunk in self.client.iter_download(
                            message.media,
                            offset=current_offset,
                            request_size=CHUNK_SIZE,
                        ):
                            if len(chunk) > remaining_limit:
                                chunk = chunk[:remaining_limit]
                            await f.write(chunk)
                            chunk_len = len(chunk)
                            current_offset += chunk_len
                            remaining_limit -= chunk_len

                            async with progress_lock:
                                received_per_part[part_idx] += chunk_len
                                total_recv = sum(received_per_part)
                                last_prog_t = time.time()

                            if total_recv - last_reported >= 256 * 1024:
                                elapsed = time.time() - start_time
                                speed = (total_recv - total_already) / max(elapsed, 0.001)
                                remain = total_size - total_recv
                                eta = f"{int(remain / speed)}s" if speed > 0 else "計算中..."
                                progress_callback(total_recv, total_size, eta)
                                last_reported = total_recv

                            if remaining_limit <= 0:
                                break
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    wait = 2 ** attempt
                    self.log.warning(f"分段 {part_idx} 失敗 ({attempt + 1}/{MAX_RETRY_PER_PART})，{wait}s 後重試: {e}")
                    if attempt < MAX_RETRY_PER_PART - 1:
                        await asyncio.sleep(wait)
                    else:
                        raise RuntimeError(f"分段 {part_idx} 超過最大重試次數。") from e

        async def do_multipart():
            await asyncio.gather(*[download_part(i) for i in range(NUM_WORKERS)])
            self.log.info(f"所有分段完成，開始合併 → {os.path.basename(dest_path)}")
            async with aiofiles.open(dest_path, "wb") as out:
                for i in range(NUM_WORKERS):
                    part_path = os.path.join(parts_dir, f"part{i:02d}")
                    if os.path.exists(part_path):
                        async with aiofiles.open(part_path, "rb") as pf:
                            while True:
                                buf = await pf.read(4 * 1024 * 1024)
                                if not buf:
                                    break
                                await out.write(buf)

            await asyncio.sleep(3)
            for attempt in range(3):
                final_size = os.path.getsize(dest_path)
                if final_size == total_size:
                    shutil.rmtree(parts_dir, ignore_errors=True)
                    return
                self.log.warning(f"整檔驗證第 {attempt + 1}/3 次未通過 ({final_size}/{total_size})...")
                await asyncio.sleep(2)
            raise RuntimeError("整檔大小驗證失敗")

        success = await self._run_with_watchdog(do_multipart, lambda: last_prog_t, message.id)
        if not success and os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception:
                pass
        return success

    async def _run_with_watchdog(self, coro_func, get_last_progress_time, msg_id: int) -> bool:
        task = asyncio.create_task(coro_func())
        watchdog_triggered = False

        async def watchdog():
            nonlocal watchdog_triggered
            while not task.done():
                await asyncio.sleep(5)
                if time.time() - get_last_progress_time() > WATCHDOG_TIMEOUT:
                    watchdog_triggered = True
                    task.cancel()
                    self.log.warning(f"看門狗觸發：{WATCHDOG_TIMEOUT}s 無數據，已取消任務 (ID {msg_id})")

        wdog = asyncio.create_task(watchdog())
        try:
            await task
            return True
        except asyncio.CancelledError:
            if watchdog_triggered:
                self.log.warning(f"下載任務已被看門狗取消 (ID {msg_id})")
                return False
            # External cancel/pause: cancel inner task and await its cleanup before propagating
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise
        except RuntimeError as e:
            self.log.error(f"下載任務錯誤 (ID {msg_id}): {e}")
            return False
        except Exception as e:
            self.log.error(f"下載失敗 (ID {msg_id}): {e}", exc_info=True)
            return False
        finally:
            wdog.cancel()
            try:
                await wdog
            except asyncio.CancelledError:
                pass

    async def delete_message(self, message_id: int, chat_target: Optional[Union[str, int]] = None) -> bool:
        target = chat_target if chat_target is not None else self.watch_chat
        try:
            await self.client.delete_messages(target, [message_id])
            self.log.info(f"雲端訊息已沖銷: chat={target}, ID={message_id}")
            return True
        except Exception as e:
            self.log.warning(f"無法刪除 Telegram 訊息（可能無權限）(chat={target}, ID={message_id}): {e}")
            return False
