"""会话级 Access Token 存储。

每个聊天会话（私聊 / 群聊）可以绑定自己的墨墨 Access Token。设计要点：

* 数据落在 AstrBot 的插件数据目录（``data/plugin_data/<插件名>/``），
  不写在插件自身目录，避免插件更新时被覆盖。
* 落盘文件只保存 Token 本身，不保存任何墨墨的词汇内容。
* 支持回退到插件配置里的全局 Token。
"""

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


class TokenStore:
    """``session -> access_token`` 的简单持久化映射。"""

    FILENAME = "tokens.json"

    def __init__(self, data_dir: str, global_token: str = "") -> None:
        self.data_dir = data_dir
        self.global_token = (global_token or "").strip()
        self.path = os.path.join(data_dir, self.FILENAME)
        self._sessions: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError as exc:
            logger.warning("创建插件数据目录失败：%s", exc)
            return
        try:
            with open(self.path, encoding="utf-8") as fp:
                raw = json.load(fp)
            sessions = raw.get("sessions") if isinstance(raw, dict) else None
            if isinstance(sessions, dict):
                self._sessions = {
                    str(k): v for k, v in sessions.items() if isinstance(v, dict)
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("读取 Token 存储失败，将忽略：%s", exc)

    def _save(self) -> None:
        payload = {"version": 1, "sessions": self._sessions}
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass  # Windows 上可能不支持，忽略
        except OSError as exc:
            logger.error("保存 Token 失败：%s", exc)

    # ------------------------------------------------------------------ #
    def set_global_token(self, token: str) -> None:
        """更新配置中的全局 Token（由插件在配置重载时调用）。"""
        self.global_token = (token or "").strip()

    def get_token(self, session_id: str) -> str:
        """取某会话应使用的 Token，优先会话级、其次全局。"""
        entry = self._sessions.get(session_id or "")
        if entry:
            token = str(entry.get("token") or "").strip()
            if token:
                return token
        return self.global_token

    def has_session_token(self, session_id: str) -> bool:
        entry = self._sessions.get(session_id or "")
        return bool(entry and str(entry.get("token") or "").strip())

    def set_token(self, session_id: str, token: str, *, source: str = "chat") -> None:
        """为一个会话绑定 Token。"""
        session_id = session_id or ""
        if not session_id:
            raise ValueError("session_id 不能为空")
        self._sessions[session_id] = {
            "token": (token or "").strip(),
            "source": source,
            "updated_at": int(time.time()),
        }
        self._save()

    def remove_token(self, session_id: str) -> bool:
        """解绑会话 Token，返回是否确实删除了一条记录。"""
        existed = self._sessions.pop(session_id or "", None) is not None
        if existed:
            self._save()
        return existed

    def clear(self) -> int:
        """清空所有会话 Token，返回清理条数。"""
        count = len(self._sessions)
        self._sessions = {}
        if count:
            self._save()
        return count

    def masked(self, session_id: str) -> str:
        """返回脱敏后的 Token 摘要，用于在聊天中回显确认。"""
        token = self.get_token(session_id)
        if not token:
            return ""
        return f"{token[:6]}…{token[-4:]}" if len(token) > 12 else "***"
