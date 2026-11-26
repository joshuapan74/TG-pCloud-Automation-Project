"""Download queue tracking, persistent pending queue, and recent transfer history."""

import asyncio
from typing import Any, Dict, List, Optional, Set

from platform_utils import atomic_write_json, read_json_safe


class QueueService:
    """Maintains active download queue, persistent pending state, and recent completed history."""

    def __init__(self, state_path: Optional[str] = None, max_history: int = 20):
        self.state_path = state_path
        self.items: Dict[int, Dict[str, Any]] = {}
        self.order: List[int] = []
        self.history: List[Dict[str, Any]] = []
        self.max_history = max_history
        self.download_lock = asyncio.Lock()

    def _serialize_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "msg_id": item["msg_id"],
            "name": item["name"],
            "size": item["size"],
            "chat_id": item.get("chat_id", "me"),
            "status": item["status"],
            "progress": round(float(item.get("progress", 0.0)), 1),
            "speed": item.get("speed", "--"),
            "eta": item.get("eta", "--"),
            "is_completed": bool(item.get("is_completed", False)),
        }

    def persist_state(self):
        """Persist active/pending/quarantined tasks to disk (no message_obj)."""
        if not self.state_path:
            return
        pending = [
            self._serialize_item(self.items[mid])
            for mid in self.order
            if mid in self.items and not self.items[mid].get("is_completed")
        ]
        try:
            atomic_write_json(self.state_path, {"pending": pending})
        except Exception:
            pass

    def load_persisted_pending(self) -> List[Dict[str, Any]]:
        """Load unfinished tasks saved from previous session."""
        if not self.state_path:
            return []
        data = read_json_safe(self.state_path, {})
        items = data.get("pending", [])
        return items if isinstance(items, list) else []

    def add_item(
        self,
        msg_id: int,
        name: str,
        size: int,
        chat_id: str = "me",
        status: str = "等待中",
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Add a new file download task to the queue."""
        if msg_id not in self.items:
            self.items[msg_id] = {
                "msg_id": msg_id,
                "name": name,
                "size": size,
                "chat_id": str(chat_id),
                "status": status,
                "progress": 0.0,
                "speed": "--",
                "eta": "計算中..." if status == "等待中" else "--",
                "received_bytes": 0,
                "is_completed": False,
            }
            self.order.append(msg_id)
            if persist:
                self.persist_state()
        return self.items[msg_id]

    def get_item(self, msg_id: int) -> Optional[Dict[str, Any]]:
        return self.items.get(msg_id)

    def update_item(self, msg_id: int, persist: Optional[bool] = None, **kwargs):
        if msg_id in self.items:
            status_changed = "status" in kwargs and kwargs["status"] != self.items[msg_id].get("status")
            self.items[msg_id].update(kwargs)
            if persist is True or (persist is None and status_changed):
                self.persist_state()

    def mark_completed(self, msg_id: int, status_label: str = "已完成並清理"):
        """Mark item completed and archive a lightweight summary in history."""
        if msg_id in self.items:
            self.items[msg_id].update({
                "status": status_label,
                "progress": 100.0,
                "eta": "0s",
                "is_completed": True,
            })
            self.history.insert(0, self._serialize_item(self.items[msg_id]))
            self.history = self.history[: self.max_history]
            self.persist_state()

    def remove_item(self, msg_id: int, force_delete: bool = False):
        if msg_id in self.order:
            self.order.remove(msg_id)
        if msg_id in self.items and (force_delete or self.items[msg_id].get("is_completed")):
            del self.items[msg_id]
        self.persist_state()

    def clear_all(self, keep_downloading: bool = True) -> int:
        """Clear all history and non-downloading queue items; returns cleared count."""
        cleared = len(self.history)
        self.history.clear()
        for mid in list(self.order):
            item = self.items.get(mid)
            if keep_downloading and item and item.get("status") == "下載中":
                continue
            self.order.remove(mid)
            if mid in self.items:
                del self.items[mid]
            cleared += 1
        self.persist_state()
        return cleared

    def get_pending_count(self) -> int:
        return sum(1 for item in self.items.values() if item["status"] == "等待中")

    def get_retained_filenames(self) -> Set[str]:
        """Return filenames that still belong to active or quarantined queue items."""
        return {item["name"] for item in self.items.values() if not item.get("is_completed")}

    def get_all_items(self) -> List[Dict[str, Any]]:
        return [self.items[msg_id] for msg_id in self.order if msg_id in self.items]

    def get_serializable_items(self) -> List[Dict[str, Any]]:
        """Return active items plus recent completed history without non-JSON objects."""
        active = [self._serialize_item(item) for item in self.get_all_items()]
        return active + list(self.history)
