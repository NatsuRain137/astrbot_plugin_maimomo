"""插件业务层：把聊天侧的需求翻译成对墨墨 API 的调用。

这一层不依赖 AstrBot，只依赖 :mod:`maimemo_api`、:mod:`token_store` 与 :mod:`render`，
方便单独测试；``main.py`` 中的指令与 LLM 工具都只是它的薄封装。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .maimemo_api import (
    CN_TZ,
    InterpretationTag,
    MaimemoAPIError,
    MaimemoAuthError,
    MaimemoCache,
    MaimemoClient,
    MaimemoRateLimitError,
    NoteType,
)
from .render import (
    ATTRIBUTION,
    format_notepad_detail,
    format_notepads,
    format_study_items,
    format_study_progress,
    format_study_records,
    format_vocabulary,
    format_word_bundle,
)
from .token_store import TokenStore

logger = logging.getLogger(__name__)


class MaimemoServiceError(Exception):
    """面向用户的、已经润色过的错误。"""


@dataclass
class PluginSettings:
    """从插件配置读出来的运行时选项。"""

    access_token: str = ""
    base_url: str = "https://open.maimemo.com/open/api/v1"
    timeout: int = 20
    max_retries: int = 2
    enable_llm_tools: bool = True
    allow_llm_write: bool = False
    notepad_creation: str = "everyone"
    list_limit: int = 20
    word_limit: int = 5
    show_attribution: bool = True

    @classmethod
    def from_config(cls, config: Any) -> "PluginSettings":
        """从 AstrBotConfig（一个 dict 子类）安全地构造设置。"""

        def pick(key: str, default: Any) -> Any:
            try:
                value = config.get(key, default)
            except Exception:  # noqa: BLE001 - 配置对象异常时退回默认值
                return default
            return default if value is None else value

        output = pick("output", {})
        if not isinstance(output, dict):
            output = {}

        def pick_output(key: str, default: Any) -> Any:
            value = output.get(key, default)
            return default if value is None else value

        def as_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        return cls(
            access_token=str(pick("access_token", "") or "").strip(),
            base_url=str(
                pick("base_url", "https://open.maimemo.com/open/api/v1")
                or "https://open.maimemo.com/open/api/v1"
            ).strip(),
            timeout=max(5, as_int(pick("timeout", 20), 20)),
            max_retries=max(0, min(5, as_int(pick("max_retries", 2), 2))),
            enable_llm_tools=bool(pick("enable_llm_tools", True)),
            allow_llm_write=bool(pick("allow_llm_write", False)),
            notepad_creation=str(pick("notepad_creation", "everyone") or "everyone"),
            list_limit=max(5, min(100, as_int(pick_output("list_limit", 20), 20))),
            word_limit=max(1, min(20, as_int(pick_output("word_limit", 5), 5))),
            show_attribution=bool(pick_output("show_attribution", True)),
        )


class MaimemoService:
    """插件各指令与工具共用的门面对象。"""

    def __init__(self, data_dir: str, settings: PluginSettings) -> None:
        self.settings = settings
        self.tokens = TokenStore(data_dir, settings.access_token)
        cache = MaimemoCache(f"{data_dir}/voc_cache.json")
        self.client = MaimemoClient(
            access_token=settings.access_token,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
            cache=cache,
        )
        #: 最近一次列出的云词本，用于让用户按序号引用
        self._last_notepads: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    def apply_settings(self, settings: PluginSettings) -> None:
        """配置热重载时更新设置。"""
        self.settings = settings
        self.client.base_url = settings.base_url.rstrip("/")
        self.client.timeout = settings.timeout
        self.client.max_retries = settings.max_retries
        self.tokens.set_global_token(settings.access_token)

    async def close(self) -> None:
        await self.client.close()

    def _client_for(self, session_id: str) -> MaimemoClient:
        """把共享客户端的 Token 切换成该会话的 Token。

        复用同一个 ``MaimemoClient``（共享连接池、限流器与缓存），
        只临时覆盖 Token；``set_token`` 与真正的请求之间没有 ``await``。
        """
        token = self.tokens.get_token(session_id)
        self.client.set_token(token)
        return self.client

    def require_client(self, session_id: str) -> MaimemoClient:
        """取到绑定了 Token 的客户端，没有 Token 时抛 :class:`MaimemoAuthError`。"""
        client = self._client_for(session_id)
        if not client.configured:
            raise MaimemoAuthError(
                "尚未配置墨墨 Access Token。\n"
                "获取方式：墨墨背单词 App → 我的 → 开放 API，"
                "然后在这里发送 `/memo token <你的Token>` 绑定。"
            )
        return client

    def _wrap_error(self, exc: Exception) -> MaimemoServiceError:
        """把底层异常转换成适合直接回复给用户的文案。"""
        if isinstance(exc, MaimemoAuthError):
            return MaimemoServiceError(
                "墨墨鉴权失败，Access Token 可能无效或已过期。\n"
                "请重新获取 Token 后用 `/memo token <Token>` 更新"
                "（网页获取的 Token 有效期为 7 天）。"
            )
        if isinstance(exc, MaimemoRateLimitError):
            return MaimemoServiceError(f"请求过于频繁：{exc.message}")
        if isinstance(exc, MaimemoAPIError):
            return MaimemoServiceError(f"墨墨接口返回错误：{exc.message}")
        return MaimemoServiceError(f"调用墨墨接口时出现问题：{exc}")

    # ------------------------------------------------------------------ #
    # 学习数据
    # ------------------------------------------------------------------ #
    async def study_status(self, session_id: str, *, with_plan: bool = True) -> str:
        """今日学习进度（可选附带规划总数与待复习数）。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        try:
            progress = await client.get_study_progress()
            plan_total = due_today = None
            if with_plan:
                try:
                    plan_total = await client.count_plan_total()
                    due_today = await client.count_due_before()
                except MaimemoAPIError as exc:
                    logger.warning("获取学习规划统计失败：%s", exc)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        return format_study_progress(
            progress, plan_total=plan_total, due_today=due_today
        )

    async def today_words(
        self,
        session_id: str,
        *,
        is_finished: bool | None = None,
        limit: int | None = None,
    ) -> str:
        """今日单词列表。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        try:
            items = await client.get_today_items(
                is_finished=is_finished, limit=limit or 1000
            )
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        title = "今日单词"
        if is_finished is True:
            title = "今日已学单词"
        elif is_finished is False:
            title = "今日未学单词"
        return format_study_items(items, title=title, limit=self.settings.list_limit)

    async def today_forgotten(self, session_id: str) -> str:
        """今日标记为「忘记」的单词。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        try:
            items = await client.get_today_items(is_finished=True, limit=1000)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc

        forgotten = [
            item for item in items if str(item.get("first_response") or "") == "FORGET"
        ]
        if not forgotten:
            return (
                "🎉 今天没有被标记为「忘记」的单词。\n"
                f"（今日已完成 {len(items)} 个，数据来自墨墨公测接口）"
            )
        body = format_study_items(
            forgotten, title="今日忘记的单词", limit=self.settings.list_limit
        )
        return body + "\n\n建议把这些词加进云词本重点复习：\n" + self._word_list_hint(
            [str(i.get("voc_spelling") or "") for i in forgotten]
        )

    async def review_plan(
        self, session_id: str, *, days: int = 1, only_count: bool = False
    ) -> str:
        """未来 N 天内需要复习的单词。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        days = max(1, min(days, 365))
        end = datetime.now(CN_TZ) + timedelta(days=days - 1)
        end = end.replace(hour=23, minute=59, second=59, microsecond=0)

        try:
            if only_count:
                count = await client.count_due_before(end)
                return f"📅 到 {end:%Y-%m-%d} 为止需要复习的单词：{count} 个"
            records, count = await client.query_study_records(
                end=end.isoformat(), limit=1000
            )
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        return format_study_records(
            records,
            count,
            title=f"{days} 天内待复习单词",
            limit=self.settings.list_limit,
        )

    async def sticky_words(self, session_id: str, *, limit: int = 1000) -> str:
        """反复忘记的「顽固词」。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        try:
            records, _ = await client.query_study_records(limit=limit)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc

        sticky = [
            record
            for record in records
            if "STICKING" in _as_str_list(record.get("tags"))
        ]
        if not sticky:
            return "🎉 当前没有标记为「顽固词」的记录。"
        return format_study_records(
            sticky,
            len(sticky),
            title="顽固词（反复忘记）",
            limit=self.settings.list_limit,
        )

    # ------------------------------------------------------------------ #
    # 查词
    # ------------------------------------------------------------------ #
    async def search_words(self, session_id: str, words: list[str]) -> str:
        """批量把拼写解析为 voc_id。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        cleaned = _split_words(words)
        if not cleaned:
            raise MaimemoServiceError("请提供至少一个单词。")
        try:
            found = await client.query_vocabularies(spellings=cleaned[:1000])
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc

        found_spellings = {str(i.get("spelling", "")).lower() for i in found}
        missing = [w for w in cleaned if w.lower() not in found_spellings]
        text = format_vocabulary(found)
        if missing:
            text += "\n\n未找到：" + "、".join(missing[:20])
        return text

    async def word_detail(self, session_id: str, spelling: str) -> str:
        """单词的自定义释义 / 助记 / 例句。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        spelling = (spelling or "").strip()
        if not spelling:
            raise MaimemoServiceError("请提供要查询的单词。")
        try:
            bundle = await client.get_word_bundle(spelling)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        text = format_word_bundle(bundle, limit=self.settings.word_limit)
        if not self.settings.show_attribution:
            text = text.replace("\n" + ATTRIBUTION, "")
        return text

    # ------------------------------------------------------------------ #
    # 写入学习规划
    # ------------------------------------------------------------------ #
    async def add_words(
        self, session_id: str, words: list[str], *, advance: bool = False
    ) -> str:
        """把单词加入学习规划。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        cleaned = _split_words(words)
        if not cleaned:
            raise MaimemoServiceError("请提供要加入学习规划的单词。")
        if len(cleaned) > 1000:
            raise MaimemoServiceError("一次最多添加 1000 个单词。")
        try:
            result = await client.add_words(cleaned, advance=advance)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc

        lines = [
            f"✅ 已提交 {result['requested']} 个单词，成功加入 {result['added_count']} 个。"
        ]
        if advance:
            lines.append("已同时设置为立即复习。")
        if result["missing"]:
            lines.append("词库中未找到：" + "、".join(result["missing"][:20]))
        if result["added_count"] < result["requested"]:
            lines.append("（部分单词可能已达学习上限或已在规划中）")
        lines.append("请打开墨墨 App 同步查看。")
        return "\n".join(lines)

    async def advance_words(self, session_id: str, words: list[str]) -> str:
        """把已在规划中的单词提前到立即复习。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        cleaned = _split_words(words)
        if not cleaned:
            raise MaimemoServiceError("请提供要提前复习的单词。")
        try:
            result = await client.advance_study(cleaned)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc

        lines = [
            f"⏩ 已提交 {result['requested']} 个单词，"
            f"成功提前 {result['advanced_count']} 个。",
            "该功能需要墨墨账号等级 ≥ 10；若数量为 0，"
            "可能是等级不足或单词不在学习规划中。",
        ]
        if result["missing"]:
            lines.append("词库中未找到：" + "、".join(result["missing"][:20]))
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 云词本
    # ------------------------------------------------------------------ #
    async def list_notepads(self, session_id: str) -> str:
        """列出云词本，并记住顺序以便用序号引用。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        try:
            notepads = await client.list_notepads(limit=100, offset=0)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        self._last_notepads[session_id] = notepads
        text = format_notepads(notepads, limit=self.settings.list_limit)
        if notepads:
            text += "\n\n提示：可用 `/memo np get <序号>` 查看详情。"
        return text

    async def get_notepad(self, session_id: str, ref: str) -> str:
        """按 id 或序号查看云词本详情。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        notepad_id = self._resolve_notepad_ref(session_id, ref)
        try:
            notepad = await client.get_notepad(notepad_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        return format_notepad_detail(notepad, limit=self.settings.list_limit)

    def _resolve_notepad_ref(self, session_id: str, ref: str) -> str:
        """把用户给的序号或 id 解析成云词本 id。"""
        ref = (ref or "").strip()
        if not ref:
            raise MaimemoServiceError(
                "请提供云词本 id 或序号（可先执行 `/memo np list`）。"
            )
        if ref.isdigit():
            cached = self._last_notepads.get(session_id) or []
            index = int(ref) - 1
            if 0 <= index < len(cached):
                return str(cached[index].get("id"))
            raise MaimemoServiceError(
                f"序号 {ref} 超出范围，请先执行 `/memo np list` 刷新列表。"
            )
        return ref

    async def create_notepad(
        self,
        session_id: str,
        title: str,
        content: str = "",
        brief: str = "",
        words: list[str] | None = None,
    ) -> str:
        """新建云词本；``words`` 与 ``content`` 二者取其一。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        title = (title or "").strip()
        if not title:
            raise MaimemoServiceError("请提供云词本标题。")

        body_content = (content or "").strip()
        if words:
            extra = "\n".join(_split_words(words))
            body_content = f"{body_content}\n{extra}".strip() if body_content else extra
        if not body_content:
            raise MaimemoServiceError(
                "请提供词条内容：用 `/memo np add <标题> <单词1 单词2 ...>`，"
                "或 `章节名|词条` 形式提供带章节的内容。"
            )

        try:
            notepad = await client.create_notepad(
                title, body_content, brief=brief or title
            )
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc

        self._last_notepads.pop(session_id, None)
        word_count = len(_split_words(words or [])) or _count_content_words(body_content)
        return (
            f"✅ 已创建云词本「{title}」\n"
            f"id：{notepad.get('id', '?')}\n"
            f"词条：{word_count} 个\n"
            "打开墨墨 App 的云词本即可看到（可能需要下拉同步）。"
        )

    async def append_words(
        self, session_id: str, ref: str, words: list[str], *, chapter: str = ""
    ) -> str:
        """把单词追加到已有云词本。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        cleaned = _split_words(words)
        if not cleaned:
            raise MaimemoServiceError("请提供要添加的单词。")
        notepad_id = self._resolve_notepad_ref(session_id, ref)
        try:
            notepad = await client.add_words_to_notepad(
                notepad_id, cleaned, chapter=chapter
            )
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        title = notepad.get("title") or notepad_id
        self._last_notepads.pop(session_id, None)
        shown = "、".join(cleaned[:30]) + ("…" if len(cleaned) > 30 else "")
        return f"✅ 已向云词本「{title}」追加 {len(cleaned)} 个单词：\n{shown}"

    async def delete_notepad(self, session_id: str, ref: str) -> str:
        """按 id 或序号删除云词本。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        notepad_id = self._resolve_notepad_ref(session_id, ref)
        try:
            notepad = await client.delete_notepad(notepad_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        self._last_notepads.pop(session_id, None)
        return f"🗑️ 已删除云词本「{notepad.get('title') or notepad_id}」。"

    # ------------------------------------------------------------------ #
    # 助记 / 释义 / 例句
    # ------------------------------------------------------------------ #
    async def add_note(
        self, session_id: str, word: str, note: str, note_type: str = "其他"
    ) -> str:
        """为一个单词新增助记。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        word = (word or "").strip()
        note = (note or "").strip()
        if not word or not note:
            raise MaimemoServiceError("用法：`/memo note <单词> | <助记内容>`。")
        if note_type not in NoteType.ALL:
            raise MaimemoServiceError(
                "不支持的助记类型，可选：" + "、".join(NoteType.ALL)
            )
        try:
            voc_id = await client.resolve_voc_id(word)
            created = await client.create_note(voc_id, note, note_type)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        return (
            f"✅ 已为 {word} 添加助记（{note_type}）\n"
            f"内容：{note}\n"
            f"id：{created.get('id', '?')}\n\n{ATTRIBUTION}"
        )

    async def add_interpretation(
        self, session_id: str, word: str, interpretation: str, tags: list[str]
    ) -> str:
        """为一个单词新增自定义释义。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        word = (word or "").strip()
        interpretation = (interpretation or "").strip()
        if not word or not interpretation:
            raise MaimemoServiceError(
                "用法：`/memo interp <单词> | <释义内容> [| 标签1,标签2]`。"
            )
        cleaned_tags = _split_tags(tags)
        invalid = [t for t in cleaned_tags if t not in InterpretationTag.ALL]
        if invalid:
            raise MaimemoServiceError(
                "不支持的标签：" + "、".join(invalid)
                + "。可选：" + "、".join(InterpretationTag.ALL)
            )
        if len(cleaned_tags) > InterpretationTag.MAX_COUNT:
            raise MaimemoServiceError(
                f"释义标签最多 {InterpretationTag.MAX_COUNT} 个。"
            )
        try:
            voc_id = await client.resolve_voc_id(word)
            created = await client.create_interpretation(
                voc_id, interpretation, cleaned_tags
            )
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        return (
            f"✅ 已为 {word} 添加释义\n"
            f"内容：{interpretation}\n"
            f"id：{created.get('id', '?')}\n\n{ATTRIBUTION}"
        )

    async def add_phrase(
        self, session_id: str, word: str, phrase: str, translation: str = ""
    ) -> str:
        """为一个单词新增例句。"""
        try:
            client = self.require_client(session_id)
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        word = (word or "").strip()
        phrase = (phrase or "").strip()
        if not word or not phrase:
            raise MaimemoServiceError(
                "用法：`/memo phrase <单词> | <例句> | <翻译>`。"
            )
        try:
            voc_id = await client.resolve_voc_id(word)
            created = await client.create_phrase(
                voc_id, phrase, interpretation=translation.strip()
            )
        except MaimemoAPIError as exc:
            raise self._wrap_error(exc) from exc
        return (
            f"✅ 已为 {word} 添加例句\n"
            f"{phrase}\n{translation}\n"
            f"id：{created.get('id', '?')}\n\n{ATTRIBUTION}"
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _word_list_hint(words: list[str]) -> str:
        cleaned = _split_words(words)
        if not cleaned:
            return "（没有可用的单词）"
        return f"`/memo np add 我的生词本 {' '.join(cleaned[:30])}`"

    def status_hint(self, session_id: str) -> str:
        """返回当前会话的鉴权状态描述（不含明文 Token）。"""
        if self.tokens.has_session_token(session_id):
            return f"当前会话已绑定个人 Token（{self.tokens.masked(session_id)}）"
        if self.tokens.global_token:
            return "使用插件配置中的全局 Token"
        return "尚未配置 Token"


# ---------------------------------------------------------------------- #
# 纯函数辅助
# ---------------------------------------------------------------------- #
def _split_words(words: Any) -> list[str]:
    """把单词列表打平、去重、保序。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in _as_str_list(words):
        for token in str(item).replace(",", " ").replace("，", " ").split():
            token = token.strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(token)
    return result


def _split_tags(tags: Any) -> list[str]:
    result: list[str] = []
    for item in _as_str_list(tags):
        for token in str(item).replace("，", ",").replace("、", ",").split(","):
            token = token.strip()
            if token and token not in result:
                result.append(token)
    return result


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _count_content_words(content: str) -> int:
    """统计云词本 content 中的词条数（忽略章节标题行）。"""
    return sum(
        1
        for line in (content or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
