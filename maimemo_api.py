"""墨墨背单词（MaiMemo）开放 API 的异步客户端。

本模块不依赖 AstrBot，可独立测试与复用。

接口总览（Base URL: https://open.maimemo.com/open/api/v1）::

    GET    /vocabulary?spelling=word          单词 spelling -> voc_id
    POST   /vocabulary/query                  批量查询（spellings / ids，最多 1000）
    GET    /notes?voc_id=...                  助记列表
    POST   /notes                             新建助记
    POST   /notes/{id}                        更新助记
    DELETE /notes/{id}                        删除助记
    GET    /interpretations?voc_id=...        自定义释义列表
    POST   /interpretations                   新建释义
    POST   /interpretations/{id}              更新释义
    DELETE /interpretations/{id}              删除释义
    GET    /phrases?voc_id=...                例句列表
    POST   /phrases                           新建例句
    POST   /phrases/{id}                      更新例句
    DELETE /phrases/{id}                      删除例句
    GET    /notepads?limit=&offset=           云词本列表
    GET    /notepads/{id}                     云词本详情
    POST   /notepads                          新建云词本
    POST   /notepads/{id}                     更新云词本
    DELETE /notepads/{id}                     删除云词本
    POST   /study/get_study_progress          今日学习进度
    POST   /study/get_today_items             今日单词列表
    POST   /study/query_study_records         学习记录 / 复习计划
    POST   /study/add_words                   加入学习规划
    POST   /study/advance_study               提前复习

鉴权：``Authorization: Bearer <access_token>``

响应信封：``{"data": {...}, "errors": [], "success": true}``。
本客户端会自动解包 ``data``，同时兼容没有信封的形态。

限流（按 Access Token 对应用户计）：20 次/10 秒，40 次/60 秒，2000 次/5 小时。
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

__all__ = [
    "MaimemoAPIError",
    "MaimemoAuthError",
    "MaimemoRateLimitError",
    "MaimemoClient",
    "MaimemoCache",
    "NotepadStatus",
    "NoteType",
    "InterpretationTag",
    "PhraseTag",
    "StudyResponse",
    "CN_TZ",
]

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

DEFAULT_BASE_URL = "https://open.maimemo.com/open/api/v1"

#: 官方公布的三个限流窗口（次数上限, 窗口秒数）
RATE_WINDOWS = ((20, 10.0), (40, 60.0), (2000, 18000.0))


class MaimemoAPIError(Exception):
    """调用墨墨开放 API 失败。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload

    def __str__(self) -> str:
        if self.status is not None:
            return f"[HTTP {self.status}] {self.message}"
        return self.message


class MaimemoAuthError(MaimemoAPIError):
    """Access Token 缺失、过期或无效。"""


class MaimemoRateLimitError(MaimemoAPIError):
    """触发官方限流。"""


class NotepadStatus:
    """云词本状态。"""

    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    DELETED = "DELETED"

    ALL = (PUBLISHED, UNPUBLISHED, DELETED)


class NoteType:
    """助记类型（``/notes`` 的 note_type 取值）。"""

    ALL = (
        "联想",
        "谐音",
        "派生",
        "词根",
        "词源",
        "固搭",
        "语法",
        "对比",
        "近义",
        "反义",
        "扩展",
        "串记",
        "口诀",
        "合成",
        "吐槽",
        "其他",
        "固定搭配",
        "词根词缀",
        "辨析",
        "近反义词",
        "图例",
    )


class InterpretationTag:
    """释义可用标签，最多同时选 3 个。"""

    ALL = (
        "简明",
        "详细",
        "英英",
        "小学",
        "初中",
        "高中",
        "四级",
        "六级",
        "专升本",
        "专四",
        "专八",
        "考研",
        "考博",
        "雅思",
        "托福",
        "托业",
        "新概念",
        "GRE",
        "GMAT",
        "BEC",
        "MBA",
        "SAT",
        "ACT",
        "法学",
        "医学",
    )
    MAX_COUNT = 3


class PhraseTag:
    """例句可用标签，最多同时选 3 个。"""

    ALL = InterpretationTag.ALL + ("词典", "短语")
    MAX_COUNT = 3


class StudyResponse:
    """学习反馈枚举。"""

    FAMILIAR = "FAMILIAR"
    VAGUE = "VAGUE"
    FORGET = "FORGET"
    WELL_FAMILIAR = "WELL_FAMILIAR"
    CANCEL_WELL_FAMILIAR = "CANCEL_WELL_FAMILIAR"

    LABELS = {
        FAMILIAR: "认识",
        VAGUE: "模糊",
        FORGET: "忘记",
        WELL_FAMILIAR: "熟知",
        CANCEL_WELL_FAMILIAR: "取消熟知",
    }


@dataclass
class _Window:
    limit: int
    span: float
    hits: deque = field(default_factory=deque)


class MaimemoCache:
    """极简 JSON 文件缓存。

    只用于缓存 ``spelling -> voc_id`` 这类**不含版权内容**的标识数据，
    以减少 API 调用、规避官方限流。释义 / 助记 / 例句等内容数据不会被持久化。
    """

    #: 默认缓存有效期：7 天（与墨墨服务协议允许的临时缓存上限一致）
    DEFAULT_TTL = 7 * 24 * 3600

    def __init__(self, path: str | None = None, ttl: float = DEFAULT_TTL) -> None:
        self.path = path
        self.ttl = ttl
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fp:
                raw = json.load(fp)
            if isinstance(raw, dict):
                self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("读取墨墨缓存失败，将忽略：%s", exc)

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            import os

            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:
            logger.warning("写入墨墨缓存失败：%s", exc)

    def get(self, key: str) -> Any:
        item = self._data.get(key)
        if not item:
            return None
        if self.ttl > 0 and time.time() - float(item.get("t", 0)) > self.ttl:
            self._data.pop(key, None)
            self._dirty = True
            return None
        return item.get("v")

    def set(self, key: str, value: Any) -> None:
        self._data[key] = {"v": value, "t": time.time()}
        self._dirty = True


class MaimemoClient:
    """墨墨开放 API 异步客户端。

    注意：客户端实例内部持有**可变**的 Access Token、连接池与限流窗口。
    插件的用法是「单个实例 + 调用前按会话切换 Token」，这在单事件循环下是安全的：
    ``set_token`` 到真正发出请求之间没有 ``await``。
    """

    def __init__(
        self,
        access_token: str = "",
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        max_retries: int = 2,
        cache: MaimemoCache | None = None,
        user_agent: str = "astrbot-plugin-maimemo/1.0",
    ) -> None:
        self.access_token = (access_token or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.cache = cache if cache is not None else MaimemoCache(None)
        self.user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None
        self._windows = [_Window(limit=n, span=s) for n, s in RATE_WINDOWS]

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        """是否已配置 Access Token。"""
        return bool(self.access_token)

    def set_token(self, token: str) -> None:
        self.access_token = (token or "").strip()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": self.user_agent},
            )
        return self._session

    async def close(self) -> None:
        """关闭底层连接池并落盘缓存，插件卸载时调用。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self.cache.save()

    async def __aenter__(self) -> "MaimemoClient":
        await self._get_session()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # 限流
    # ------------------------------------------------------------------ #
    def _charge_rate_limit(self) -> None:
        """登记一次请求；若会超出任一窗口则直接拒绝，避免撞官方 429。"""
        now = time.monotonic()
        for window in self._windows:
            while window.hits and now - window.hits[0] >= window.span:
                window.hits.popleft()
            if len(window.hits) >= window.limit:
                raise MaimemoRateLimitError(
                    f"请求过于频繁，已触发墨墨限流"
                    f"（{window.limit} 次 / {int(window.span)} 秒），请稍后再试。"
                )
        for window in self._windows:
            window.hits.append(now)

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_error(payload: Any, status: int) -> str:
        """从错误响应中尽量提取人类可读的信息。"""
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                parts: list[str] = []
                for item in errors:
                    if isinstance(item, dict):
                        parts.append(
                            str(
                                item.get("msg")
                                or item.get("message")
                                or item.get("detail")
                                or item.get("code")
                                or item
                            )
                        )
                    else:
                        parts.append(str(item))
                return "；".join(parts)
            for key in ("message", "msg", "detail", "error", "error_description"):
                value = payload.get(key)
                if value:
                    return str(value)
        if isinstance(payload, str) and payload.strip():
            return payload.strip()[:300]
        return {
            401: "Access Token 无效或已过期",
            403: "无权访问该资源",
            404: "资源不存在",
        }.get(status, f"HTTP {status}")

    @staticmethod
    def _error_code(payload: Any) -> str:
        """取出官方错误码（形如 ``common_invalid_params``），便于排查。"""
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                code = errors[0].get("code")
                if code:
                    return str(code)
        return ""

    @classmethod
    def _describe_error(cls, payload: Any, status: int) -> str:
        """给错误加上 HTTP 状态与官方错误码，方便定位是参数问题还是鉴权问题。"""
        message = cls._extract_error(payload, status)
        code = cls._error_code(payload)
        label = f"HTTP {status}"
        if code:
            label += f" / {code}"
        return f"{label}：{message}"

    def _unwrap(self, payload: Any, status: int) -> Any:
        """解包墨墨的响应信封，返回真正的业务数据。

        官方成功响应形如 ``{"data": {...}, "errors": [], "success": true}``；
        同时兼容没有信封、直接返回业务数据的形态。
        """
        if not isinstance(payload, dict):
            return payload

        errors = payload.get("errors")
        success = payload.get("success")
        has_envelope = "data" in payload or "success" in payload or errors is not None

        if errors:
            message = self._describe_error(payload, status)
            if status in (401, 403):
                raise MaimemoAuthError(message, status=status, payload=payload)
            if status == 429:
                raise MaimemoRateLimitError(message, status=status, payload=payload)
            raise MaimemoAPIError(message, status=status, payload=payload)

        # 失败也可能是 HTTP 200 + success:false
        if has_envelope and success is False:
            raise MaimemoAPIError(
                self._describe_error(payload, status) or "墨墨接口返回失败。",
                status=status,
                payload=payload,
            )

        if "data" in payload:
            return payload["data"]
        return payload

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        use_cache: bool = False,
    ) -> Any:
        """发送一次 API 请求并返回解包后的业务数据。

        Args:
            method: HTTP 方法，``GET`` / ``POST`` / ``DELETE``。
            path: 相对 ``base_url`` 的路径，如 ``/vocabulary``。
            params: 查询参数，``None`` 值会被自动剔除。
            json_body: 请求体，会被序列化为 JSON。
            use_cache: 是否使用本地文件缓存（仅建议对只读且内容稳定的接口开启）。

        Raises:
            MaimemoAuthError: Token 缺失或鉴权失败。
            MaimemoRateLimitError: 触发限流。
            MaimemoAPIError: 其它错误（网络异常、5xx、业务错误）。
        """
        if not self.access_token:
            raise MaimemoAuthError(
                "尚未配置墨墨 Access Token，请先获取并填入插件配置。"
            )

        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        cache_key = ""
        if use_cache:
            cache_key = "{}|{}|{}".format(
                method.upper(),
                path,
                json.dumps(clean_params or {}, sort_keys=True, ensure_ascii=False),
            )
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._charge_rate_limit()
            try:
                session = await self._get_session()
                async with session.request(
                    method.upper(),
                    url,
                    params=clean_params,
                    json=json_body,
                    headers=headers,
                ) as resp:
                    status = resp.status
                    text = await resp.text()
                    payload: Any = None
                    if text:
                        try:
                            payload = json.loads(text)
                        except ValueError:
                            payload = text

                    if status in (401, 403):
                        raise MaimemoAuthError(
                            self._describe_error(payload, status),
                            status=status,
                            payload=payload,
                        )
                    if status == 429:
                        raise MaimemoRateLimitError(
                            self._describe_error(payload, status) or "触发限流",
                            status=status,
                            payload=payload,
                        )
                    if status >= 400:
                        raise MaimemoAPIError(
                            self._describe_error(payload, status),
                            status=status,
                            payload=payload,
                        )

                    data = self._unwrap(payload, status)
                    if use_cache and cache_key:
                        self.cache.set(cache_key, data)
                        self.cache.save()
                    return data
            except (MaimemoAuthError, MaimemoRateLimitError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(0.6 * (2**attempt))
            except MaimemoAPIError as exc:
                if exc.status and 500 <= exc.status < 600 and attempt < self.max_retries:
                    last_exc = exc
                    await asyncio.sleep(0.6 * (2**attempt))
                    continue
                raise

        raise MaimemoAPIError(f"请求墨墨接口失败：{last_exc}") from last_exc

    async def _get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def _post(self, path: str, body: Any = None) -> Any:
        return await self.request(
            "POST", path, json_body=body if body is not None else {}
        )

    # ------------------------------------------------------------------ #
    # 词汇 Vocabulary
    # ------------------------------------------------------------------ #
    async def get_voc(self, spelling: str) -> dict[str, Any]:
        """按拼写查询单个单词，返回 ``{"id": ..., "spelling": ...}``。"""
        spelling = (spelling or "").strip()
        if not spelling:
            raise MaimemoAPIError("单词拼写不能为空。")
        payload = await self.request(
            "GET", "/vocabulary", params={"spelling": spelling}, use_cache=True
        )
        voc = payload.get("voc") if isinstance(payload, dict) else None
        if not isinstance(voc, dict) or not voc.get("id"):
            raise MaimemoAPIError(f"未找到单词「{spelling}」，请检查拼写。")
        return voc

    async def query_vocabularies(
        self, spellings: list[str] | None = None, ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """批量查询单词。``spellings`` 与 ``ids`` 互斥，各自最多 1000 个。"""
        if spellings and ids:
            raise MaimemoAPIError("spellings 与 ids 不能同时传入。")
        body: dict[str, Any] = {}
        if spellings:
            if len(spellings) > 1000:
                raise MaimemoAPIError("spellings 一次最多 1000 个。")
            body["spellings"] = [s.strip() for s in spellings if s and s.strip()]
        elif ids:
            if len(ids) > 1000:
                raise MaimemoAPIError("ids 一次最多 1000 个。")
            body["ids"] = list(ids)
        else:
            raise MaimemoAPIError("spellings 与 ids 至少需要一个。")

        cache_key = "batchvoc|" + json.dumps(body, sort_keys=True, ensure_ascii=False)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        payload = await self._post("/vocabulary/query", body)
        voc = payload.get("voc") if isinstance(payload, dict) else None
        result = voc if isinstance(voc, list) else []
        self.cache.set(cache_key, result)
        self.cache.save()
        return result

    async def resolve_voc_id(self, spelling: str) -> str:
        """把单词拼写解析为 ``voc_id``，优先命中本地缓存。"""
        spelling = (spelling or "").strip()
        if not spelling:
            raise MaimemoAPIError("单词拼写不能为空。")
        key = f"vocid::{spelling.lower()}"
        cached = self.cache.get(key)
        if cached:
            return str(cached)
        voc = await self.get_voc(spelling)
        self.cache.set(key, voc["id"])
        self.cache.save()
        return str(voc["id"])

    async def resolve_voc_ids(self, spellings: list[str]) -> dict[str, str]:
        """批量把拼写解析为 ``voc_id``，返回 ``{spelling: voc_id}``。"""
        wanted = [s.strip() for s in spellings if s and s.strip()]
        result: dict[str, str] = {}
        missing: list[str] = []
        for word in wanted:
            cached = self.cache.get(f"vocid::{word.lower()}")
            if cached:
                result[word] = str(cached)
            else:
                missing.append(word)

        if missing:
            found = await self.query_vocabularies(spellings=missing)
            for item in found:
                spelling = str(item.get("spelling", ""))
                voc_id = item.get("id")
                if spelling and voc_id:
                    result[spelling] = str(voc_id)
                    self.cache.set(f"vocid::{spelling.lower()}", voc_id)
            self.cache.save()
        return result

    # ------------------------------------------------------------------ #
    # 助记 Notes
    # ------------------------------------------------------------------ #
    async def list_notes(self, voc_id: str) -> list[dict[str, Any]]:
        payload = await self._get("/notes", voc_id=voc_id)
        notes = payload.get("notes") if isinstance(payload, dict) else None
        return notes if isinstance(notes, list) else []

    async def create_note(
        self, voc_id: str, note: str, note_type: str = "其他"
    ) -> dict[str, Any]:
        if note_type not in NoteType.ALL:
            raise MaimemoAPIError(
                f"不支持的助记类型「{note_type}」，可选：{'、'.join(NoteType.ALL)}"
            )
        payload = await self._post(
            "/notes",
            {"note": {"voc_id": voc_id, "note_type": note_type, "note": note}},
        )
        return payload.get("note", {}) if isinstance(payload, dict) else {}

    async def update_note(
        self, note_id: str, note: str, note_type: str = "其他"
    ) -> dict[str, Any]:
        payload = await self._post(
            f"/notes/{note_id}", {"note": {"note_type": note_type, "note": note}}
        )
        return payload.get("note", {}) if isinstance(payload, dict) else {}

    async def delete_note(self, note_id: str) -> Any:
        return await self.request("DELETE", f"/notes/{note_id}")

    # ------------------------------------------------------------------ #
    # 释义 Interpretations
    # ------------------------------------------------------------------ #
    async def list_interpretations(self, voc_id: str) -> list[dict[str, Any]]:
        payload = await self._get("/interpretations", voc_id=voc_id)
        items = payload.get("interpretations") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def create_interpretation(
        self,
        voc_id: str,
        interpretation: str,
        tags: list[str] | None = None,
        status: str = NotepadStatus.PUBLISHED,
    ) -> dict[str, Any]:
        tags = [t for t in (tags or []) if t]
        if len(tags) > InterpretationTag.MAX_COUNT:
            raise MaimemoAPIError(
                f"释义标签最多选择 {InterpretationTag.MAX_COUNT} 个。"
            )
        payload = await self._post(
            "/interpretations",
            {
                "interpretation": {
                    "voc_id": voc_id,
                    "interpretation": interpretation,
                    "tags": tags,
                    "status": status,
                }
            },
        )
        return payload.get("interpretation", {}) if isinstance(payload, dict) else {}

    async def update_interpretation(
        self,
        interpretation_id: str,
        interpretation: str,
        tags: list[str] | None = None,
        status: str = NotepadStatus.PUBLISHED,
    ) -> dict[str, Any]:
        payload = await self._post(
            f"/interpretations/{interpretation_id}",
            {
                "interpretation": {
                    "interpretation": interpretation,
                    "tags": [t for t in (tags or []) if t],
                    "status": status,
                }
            },
        )
        return payload.get("interpretation", {}) if isinstance(payload, dict) else {}

    async def delete_interpretation(self, interpretation_id: str) -> Any:
        return await self.request("DELETE", f"/interpretations/{interpretation_id}")

    # ------------------------------------------------------------------ #
    # 例句 Phrases
    # ------------------------------------------------------------------ #
    async def list_phrases(self, voc_id: str) -> list[dict[str, Any]]:
        payload = await self._get("/phrases", voc_id=voc_id)
        items = payload.get("phrases") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def create_phrase(
        self,
        voc_id: str,
        phrase: str,
        interpretation: str = "",
        tags: list[str] | None = None,
        origin: str = "自编",
    ) -> dict[str, Any]:
        tags = [t for t in (tags or []) if t]
        if len(tags) > PhraseTag.MAX_COUNT:
            raise MaimemoAPIError(f"例句标签最多选择 {PhraseTag.MAX_COUNT} 个。")
        payload = await self._post(
            "/phrases",
            {
                "phrase": {
                    "voc_id": voc_id,
                    "phrase": phrase,
                    "interpretation": interpretation,
                    "tags": tags,
                    "origin": origin,
                }
            },
        )
        return payload.get("phrase", {}) if isinstance(payload, dict) else {}

    async def update_phrase(
        self,
        phrase_id: str,
        phrase: str,
        interpretation: str = "",
        tags: list[str] | None = None,
        origin: str = "自编",
    ) -> dict[str, Any]:
        payload = await self._post(
            f"/phrases/{phrase_id}",
            {
                "phrase": {
                    "phrase": phrase,
                    "interpretation": interpretation,
                    "tags": [t for t in (tags or []) if t],
                    "origin": origin,
                }
            },
        )
        return payload.get("phrase", {}) if isinstance(payload, dict) else {}

    async def delete_phrase(self, phrase_id: str) -> Any:
        return await self.request("DELETE", f"/phrases/{phrase_id}")

    # ------------------------------------------------------------------ #
    # 云词本 Notepads
    # ------------------------------------------------------------------ #
    #: 官方 CLI 对 /notepads 使用的默认页大小
    NOTEPAD_PAGE_SIZE = 10

    async def list_notepads(
        self, limit: int = NOTEPAD_PAGE_SIZE, offset: int = 0,
        ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """分页列出云词本。

        官方要求 ``limit`` 与 ``offset`` 必填。官方 CLI 固定用 limit=10，
        说明服务端对 ``limit`` 可能有未公开的上限；因此这里在遇到参数类错误时
        会自动降级为官方默认页大小重试一次，避免用户只看到「Invalid parameters」。
        """
        def build(page_size: int) -> dict[str, Any]:
            params: dict[str, Any] = {
                "limit": int(page_size),
                "offset": int(offset),
            }
            if ids:
                # 官方 CLI 使用 ids[0]=..&ids[1]=.. 的数组序列化形式
                for index, value in enumerate(ids):
                    params[f"ids[{index}]"] = value
            return params

        try:
            payload = await self._get("/notepads", **build(limit))
        except MaimemoAPIError as exc:
            # 只对「参数不合法」这类 4xx 做降级，鉴权/限流错误直接抛出
            is_param_error = (
                exc.status is not None
                and 400 <= exc.status < 500
                and exc.status not in (401, 403, 429)
            )
            if not is_param_error or int(limit) == self.NOTEPAD_PAGE_SIZE:
                raise
            logger.warning(
                "limit=%s 被服务端拒绝（%s），降级为官方默认值 %s 重试",
                limit, exc, self.NOTEPAD_PAGE_SIZE,
            )
            payload = await self._get("/notepads", **build(self.NOTEPAD_PAGE_SIZE))

        items = payload.get("notepads") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def get_notepad(self, notepad_id: str) -> dict[str, Any]:
        payload = await self._get(f"/notepads/{notepad_id}")
        return payload.get("notepad", {}) if isinstance(payload, dict) else {}

    async def create_notepad(
        self,
        title: str,
        content: str,
        brief: str = "",
        tags: list[str] | None = None,
        status: str = NotepadStatus.PUBLISHED,
    ) -> dict[str, Any]:
        """新建云词本。

        Args:
            content: 词条内容，每行一个单词，``#`` 开头的行为章节标题。
        """
        payload = await self._post(
            "/notepads",
            {
                "notepad": {
                    "title": title,
                    "brief": brief,
                    "content": content,
                    "tags": [t for t in (tags or []) if t],
                    "status": status,
                }
            },
        )
        return payload.get("notepad", {}) if isinstance(payload, dict) else {}

    async def update_notepad(
        self,
        notepad_id: str,
        title: str,
        content: str,
        brief: str = "",
        tags: list[str] | None = None,
        status: str = NotepadStatus.PUBLISHED,
    ) -> dict[str, Any]:
        """更新云词本。官方要求所有字段都必须提供。"""
        payload = await self._post(
            f"/notepads/{notepad_id}",
            {
                "notepad": {
                    "title": title,
                    "brief": brief,
                    "content": content,
                    "tags": [t for t in (tags or []) if t],
                    "status": status,
                }
            },
        )
        return payload.get("notepad", {}) if isinstance(payload, dict) else {}

    async def delete_notepad(self, notepad_id: str) -> dict[str, Any]:
        payload = await self.request("DELETE", f"/notepads/{notepad_id}")
        return payload.get("notepad", {}) if isinstance(payload, dict) else {}

    async def add_words_to_notepad(
        self,
        notepad_id: str,
        words: list[str],
        *,
        chapter: str = "",
    ) -> dict[str, Any]:
        """把单词追加到已有云词本末尾（先读取再合并回写）。

        Args:
            words: 要追加的单词列表。
            chapter: 若提供，则在追加内容前插入 ``# 章节名``。
        """
        notepad = await self.get_notepad(notepad_id)
        if not notepad:
            raise MaimemoAPIError(f"云词本 {notepad_id} 不存在。")
        existing = str(notepad.get("content") or "").rstrip("\n")
        lines: list[str] = []
        if chapter:
            lines.append(f"# {chapter.strip()}")
        lines.extend(w.strip() for w in words if w and w.strip())

        merged = "\n".join([existing, *lines]) if existing else "\n".join(lines)
        return await self.update_notepad(
            notepad_id,
            title=str(notepad.get("title") or ""),
            content=merged,
            brief=str(notepad.get("brief") or ""),
            tags=list(notepad.get("tags") or []),
            status=str(notepad.get("status") or NotepadStatus.PUBLISHED),
        )

    # ------------------------------------------------------------------ #
    # 学习数据 Study（公测接口，需在 App 内开启自动同步）
    # ------------------------------------------------------------------ #
    async def get_study_progress(self) -> dict[str, Any]:
        """今日学习进度：``finished`` / ``total`` / ``study_time``(毫秒)。"""
        payload = await self._post("/study/get_study_progress")
        progress = payload.get("progress") if isinstance(payload, dict) else None
        return progress if isinstance(progress, dict) else {}

    async def get_today_items(
        self,
        *,
        is_finished: bool | None = None,
        is_new: bool | None = None,
        voc_ids: list[str] | None = None,
        spellings: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """今日单词列表。"""
        body: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
        if is_finished is not None:
            body["is_finished"] = bool(is_finished)
        if is_new is not None:
            body["is_new"] = bool(is_new)
        if voc_ids:
            body["voc_ids"] = list(voc_ids)
        elif spellings:
            body["spellings"] = [s.strip() for s in spellings if s and s.strip()]
        payload = await self._post("/study/get_today_items", body)
        items = payload.get("today_items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def query_study_records(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        voc_ids: list[str] | None = None,
        spellings: list[str] | None = None,
        as_count: bool = False,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """查询学习记录 / 复习计划。

        Returns:
            ``(records, count)``；``as_count=True`` 时 ``records`` 为空、``count`` 为总数。
        """
        body: dict[str, Any] = {
            "as_count": bool(as_count),
            "limit": max(1, min(int(limit), 1000)),
        }
        if start or end:
            window: dict[str, str] = {}
            if start:
                window["start"] = start
            if end:
                window["end"] = end
            body["next_study_date"] = window
        if voc_ids:
            body["voc_ids"] = list(voc_ids)
        elif spellings:
            body["spellings"] = [s.strip() for s in spellings if s and s.strip()]

        payload = await self._post("/study/query_study_records", body)
        if not isinstance(payload, dict):
            return [], 0
        records = payload.get("records")
        return (
            records if isinstance(records, list) else [],
            int(payload.get("count") or 0),
        )

    async def count_due_before(self, day: datetime | None = None) -> int:
        """统计某天（默认今天，北京时间 23:59:59）之前需要复习的单词总数。"""
        moment = day or datetime.now(CN_TZ)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=CN_TZ)
        end = moment.astimezone(CN_TZ).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        _, count = await self.query_study_records(end=end.isoformat(), as_count=True)
        return count

    async def count_plan_total(self) -> int:
        """统计学习规划中的单词总数。"""
        _, count = await self.query_study_records(as_count=True)
        return count

    async def add_words(
        self, words: list[str], *, advance: bool = False
    ) -> dict[str, Any]:
        """把单词加入学习规划。

        Args:
            words: 单词拼写列表（会自动解析为 ``voc_id``）。
            advance: 是否同时设置成立即复习（无等级限制）。

        Returns:
            ``{"requested": n, "added_count": m, "missing": [...]}``
        """
        cleaned = [w.strip() for w in words if w and w.strip()]
        if not cleaned:
            raise MaimemoAPIError("请至少提供一个单词。")
        if len(cleaned) > 1000:
            raise MaimemoAPIError("一次最多添加 1000 个单词。")

        mapping = await self.resolve_voc_ids(cleaned)
        missing = [w for w in cleaned if w not in mapping]
        if not mapping:
            raise MaimemoAPIError(
                "这些单词在墨墨词库中都未找到：" + "、".join(missing[:20])
            )
        payload = await self._post(
            "/study/add_words",
            {
                "words": [{"id": vid} for vid in mapping.values()],
                "advance": bool(advance),
            },
        )
        added = payload.get("added_count") if isinstance(payload, dict) else 0
        return {
            "requested": len(cleaned),
            "added_count": int(added or 0),
            "missing": missing,
        }

    async def advance_study(self, words: list[str]) -> dict[str, Any]:
        """把已在规划中的单词提前到立即复习（需要账号等级 ≥ 10）。"""
        cleaned = [w.strip() for w in words if w and w.strip()]
        if not cleaned:
            raise MaimemoAPIError("请至少提供一个单词。")
        mapping = await self.resolve_voc_ids(cleaned)
        missing = [w for w in cleaned if w not in mapping]
        if not mapping:
            raise MaimemoAPIError(
                "这些单词在墨墨词库中都未找到：" + "、".join(missing[:20])
            )
        payload = await self._post(
            "/study/advance_study", {"voc_ids": list(mapping.values())}
        )
        advanced = payload.get("advanced_count") if isinstance(payload, dict) else 0
        return {
            "requested": len(cleaned),
            "advanced_count": int(advanced or 0),
            "missing": missing,
        }

    # ------------------------------------------------------------------ #
    # 组合方法：完整单词资料
    # ------------------------------------------------------------------ #
    async def get_word_bundle(self, spelling: str) -> dict[str, Any]:
        """汇总一个单词的标识、自定义释义、助记与例句。

        三个子接口并发调用；任一失败会被记录日志并降级为空列表，不影响整体返回。
        """
        voc = await self.get_voc(spelling)
        voc_id = str(voc["id"])

        async def safe(coro: Any, label: str) -> Any:
            try:
                return await coro
            except MaimemoAPIError as exc:
                logger.warning("获取 %s 的%s失败：%s", spelling, label, exc)
                return []

        interpretations, notes, phrases = await asyncio.gather(
            safe(self.list_interpretations(voc_id), "释义"),
            safe(self.list_notes(voc_id), "助记"),
            safe(self.list_phrases(voc_id), "例句"),
        )
        return {
            "voc": voc,
            "interpretations": interpretations,
            "notes": notes,
            "phrases": phrases,
        }
