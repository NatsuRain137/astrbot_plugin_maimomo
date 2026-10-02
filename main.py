"""墨墨背单词（MaiMemo）助手 —— AstrBot 插件主入口。

提供两组能力：

1. **聊天指令**：``/memo`` 指令组，供用户手动查询学习数据、管理云词本与生词。
2. **LLM 工具**：通过 ``@filter.llm_tool`` 注册的函数工具，
   让 Bot 在对话中自行调用墨墨开放 API。

墨墨开放 API 文档：https://open.maimemo.com/
"""

import functools
import inspect
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import (
    AstrMessageEvent,
    MessageChain,
    MessageEventResult,
    filter,
)
from astrbot.api.star import Context, Star, StarTools

try:  # GreedyStr 用于接收指令后的全部剩余文本（官方内建插件也这样导入）
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover - 兼容路径变化

    class GreedyStr(str):  # type: ignore[no-redef]
        """兜底实现：仅作为类型注解使用。"""


from .maimemo_api import NoteType
from .service import MaimemoService, MaimemoServiceError, PluginSettings
from .token_store import TokenStore

PLUGIN_NAME = "astrbot_plugin_maimemo"
DEFAULT_FALLBACK_CMD = "/memo"

#: 清洗用户输入中的多余横向空白
_WHITESPACE = re.compile(r"[ \t]+")

#: 唤醒前缀（AstrBot 默认是 "/"），用于把消息还原成「无前缀」形式再做文本匹配
_WAKE_PREFIX = re.compile(r"^\s*[/／!！.。]\s*")

#: 裸指令：/memo、/mm、/墨墨（不含 help 后缀；`memo help` 由子指令负责）
_BARE_COMMAND = re.compile(r"^(?:memo|mm|墨墨)$")


def _clean_prefix(text: str) -> str:
    """去掉唤醒前缀与多余空白，得到便于文本匹配的形式。"""
    return _WAKE_PREFIX.sub("", _WHITESPACE.sub(" ", (text or "").strip())).strip()


# ---------------------------------------------------------------------- #
# 装饰器与小工具
# ---------------------------------------------------------------------- #
def _command_guard(
    handler: Callable[..., AsyncGenerator[MessageEventResult, None]],
) -> Callable[..., AsyncGenerator[MessageEventResult, None]]:
    """统一处理指令中的异常，并把 Token 等敏感串从报错里抹掉。

    加了这个包装后，指令处理函数既可以是异步生成器，也可以是普通协程。
    """
    if inspect.isasyncgenfunction(handler):

        @functools.wraps(handler)
        async def gen_wrapper(self, event: AstrMessageEvent, *args: Any, **kwargs: Any):
            try:
                async for result in handler(self, event, *args, **kwargs):
                    yield result
            except Exception as exc:  # noqa: BLE001 - 兜底，避免插件崩溃
                yield event.plain_result(_friendly_error(exc, self))

        return gen_wrapper

    @functools.wraps(handler)
    async def coro_wrapper(self, event: AstrMessageEvent, *args: Any, **kwargs: Any):
        try:
            result = await handler(self, event, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(_friendly_error(exc, self))
            return
        if result is not None:
            yield result

    return coro_wrapper


def _command_guard_send(
    handler: Callable[..., AsyncGenerator[MessageEventResult, None]],
) -> Callable[..., AsyncGenerator[MessageEventResult, None]]:
    """与 :func:`_command_guard` 相同，但**主动发送**指令结果。

    为什么要主动 ``await event.send()`` 而不是 ``yield``：
    AstrBot 的 ``ProcessStage`` 只在 ``event._has_send_oper`` 为真时才跳过 LLM：

        if not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm:
            ...  # 调用 LLM

    而 ``yield result`` 只走 ``event.set_result()``（并不发送），所以若本插件没有
    实际发送过消息，就会继续进 LLM —— LLM 的回复会把指令结果**覆盖**掉，用户只看到
    AI 的转述。主动 ``send()`` 会立刻把 ``_has_send_oper`` 置真，从而跳过 LLM。

    注意：发送后必须 ``yield None``（不设置 result），否则 RespondStage 会再发一遍。
    """

    @functools.wraps(handler)
    async def wrapper(self, event: AstrMessageEvent, *args: Any, **kwargs: Any):
        async def send_text(text: str) -> None:
            if text:
                await event.send(MessageChain().message(text))

        try:
            async for result in handler(self, event, *args, **kwargs):
                chain = getattr(result, "chain", None)
                if result is not None and chain:
                    # 立刻发送，并让 RespondStage 无内容可发（避免重复）
                    await event.send(MessageChain(chain=list(chain)))
                    event.clear_result()
                yield None
        except Exception as exc:  # noqa: BLE001 - 兜底，避免插件崩溃
            await send_text(_friendly_error(exc, self))
            event.clear_result()
            yield None

    return wrapper


def _llm_tool_guard(
    *, write: bool = False
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """包装 LLM 工具函数：统一异常处理与写操作开关。

    必须在模块层定义：``@filter.llm_tool`` 下方的装饰器会在**类体执行阶段**就被调用，
    那时还没有插件实例，因此这里只返回闭包，真正的 ``self`` 由包装函数在调用时接收。

    Args:
        write: 该工具是否会修改用户账号数据；为 True 时需要管理员开启 ``allow_llm_write``。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> str:
            service = self.service
            if write and not service.settings.allow_llm_write:
                return (
                    "错误：管理员未开启「允许 AI 执行写操作」，本次操作被拒绝。"
                    "请提示用户手动使用 /memo 指令，或让管理员打开该开关。"
                )
            try:
                return await func(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 工具不应把异常抛给 LLM
                logger.exception("墨墨 LLM 工具执行失败：%s", exc)
                if isinstance(exc, MaimemoServiceError):
                    return f"错误：{exc}"
                return f"错误：{type(exc).__name__}: {exc}"

        return wrapper

    return decorator


def _friendly_error(exc: Exception, plugin: Any) -> str:
    """把异常转换成用户可读的提示，同时记录日志并抹掉 Token 明文。"""
    logger.exception("墨墨插件处理失败：%s", exc)
    text = str(exc) if isinstance(exc, MaimemoServiceError) else f"⚠️ 处理失败：{exc}"
    client = getattr(getattr(plugin, "service", None), "client", None)
    secret = getattr(client, "access_token", "") if client else ""
    if secret and len(secret) > 8:
        text = text.replace(secret, "***")
    return text


def _strip_prefix(text: str, event: AstrMessageEvent) -> str:
    """去掉消息开头的唤醒前缀，便于在帮助文本里给出正确示例。"""
    stripped = (text or "").strip()
    for prefix in getattr(getattr(event, "session", None), "wake_prefix", None) or []:
        if prefix and stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def _split_pipe(raw: str) -> list[str]:
    """按 ``|`` 或 ``｜`` 切分参数。"""
    return [part.strip() for part in re.split(r"[|｜]", raw or "")]


def _clean(text: str) -> str:
    """把换行与连续空白压成单个空格。"""
    return _WHITESPACE.sub(" ", (text or "").replace("\n", " ")).strip()


def _words(text: str) -> list[str]:
    """把一段文本切成单词列表。"""
    return [token for token in _clean(text).split(" ") if token]


# ---------------------------------------------------------------------- #
# 插件主体
# ---------------------------------------------------------------------- #
class MaimemoPlugin(Star):
    """墨墨背单词助手插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config
        settings = PluginSettings.from_config(config or {})
        try:
            data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:  # noqa: BLE001 - 数据目录不可用时退回当前目录
            logger.warning("无法解析插件数据目录，将使用当前目录：%s", exc)
            data_dir = "."
        self.service = MaimemoService(data_dir, settings)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """插件激活时同步一次配置。"""
        if self.config is not None:
            self.service.apply_settings(PluginSettings.from_config(self.config))
        logger.info("墨墨背单词助手已加载。")

    async def terminate(self) -> None:
        """插件卸载/停用时释放连接池并落盘缓存。"""
        await self.service.close()
        logger.info("墨墨背单词助手已卸载。")

    # ------------------------------------------------------------------ #
    # 帮助
    # ------------------------------------------------------------------ #
    def _help_text(self, event: AstrMessageEvent) -> str:
        """生成帮助文本，并把用户使用的别名还原成 memo。"""
        cmd = _strip_prefix(event.message_str, event) or DEFAULT_FALLBACK_CMD
        for alias in ("mm", "墨墨"):
            if cmd.startswith(alias):
                cmd = "memo" + cmd[len(alias):]
                break
        return f"""📖 墨墨背单词助手

先把墨墨的 Access Token 绑定到本会话（App → 我的 → 开放 API）：
  {cmd} token <你的Token>

【学习数据】
  {cmd} status               今日进度、剩余词数、学习时长
  {cmd} today [all|done|todo]  今日单词
  {cmd} forgot               今天标记为「忘记」的单词
  {cmd} review [天数]        未来 N 天内待复习的单词（默认 1 天）
  {cmd} sticky               反复忘记的顽固词

【查词与生词】
  {cmd} search 词1 词2 ...   批量查单词 ID
  {cmd} word <单词>          查看该词的释义 / 助记 / 例句
  {cmd} add 词1 词2 ...      加入学习规划
  {cmd} advance 词1 词2 ...  提前到立即复习（需账号等级 ≥ 10）

【云词本】
  {cmd} np list              列出云词本
  {cmd} np get <序号|id>      查看云词本详情
  {cmd} np add <标题> 词1 词2  新建云词本
  {cmd} np add <标题> 章节名|apple\\nbanana   带章节新建
  {cmd} np push <序号|id> 词1 词2              追加单词
  {cmd} np push <序号|id> 章节名|词1 词2        追加到指定章节
  {cmd} np del <序号|id>      删除云词本

【助记 / 释义 / 例句】
  {cmd} note <单词> | <助记> [| 类型]
  {cmd} interp <单词> | <释义> [| 标签,标签]
  {cmd} phrase <单词> | <例句> | <翻译>

【账号】
  {cmd} token                查看当前绑定状态
  {cmd} token clear          解绑本会话 Token
  {cmd} ping                 测试 Token 是否可用

💡 学习数据接口为墨墨公测功能，需先在 App 内开启自动同步。
💡 也可以直接对 Bot 说「我今天墨墨背了多少单词」，由 AI 调用工具回答。"""

    # ------------------------------------------------------------------ #
    # 指令组
    # ------------------------------------------------------------------ #
    @filter.command_group("memo", alias={"mm", "墨墨"})
    def memo(self):
        """墨墨背单词助手指令组。"""

    @memo.command("help")
    @_command_guard_send
    async def memo_help(self, event: AstrMessageEvent):
        """查看墨墨助手的所有指令。"""
        yield event.plain_result(self._help_text(event))

    # 注意：这里**不能**用 @filter.regex。
    # 在类体里执行装饰器时方法还是未绑定的普通函数，而 filter 的执行发生在
    # star_manager 绑定 handler 之前（绑定是 functools.partial(handler, star_cls)），
    # regex filter 里拿不到 self，因此行为不可靠。
    #
    # 也**不能**注册「名字为空的子指令」来兜底裸 `/memo`：CommandFilter 的匹配靠
    # startswith("memo ")，空名会让 `/memo help`、`/memo np list` 等全部被它抢走。
    #
    # 因此这里用 event_message_type + 函数体内判断文本，并给 **高优先级**。
    # 现状说明：裸 `/memo` 仍会被 CommandGroupFilter 判定为「参数不足」并 stop_event()
    # 打断管道（RespondStage 在 ProcessStage 之后，结果发不出去），所以裸指令只由
    # AstrBot 自己那张指令树兜底；`/memo help` 则正常工作。
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    @_command_guard_send
    async def memo_bare(self, event: AstrMessageEvent):
        """裸指令（/memo、/mm、/墨墨）显示帮助。"""
        if not _BARE_COMMAND.match(_clean_prefix(event.message_str)):
            return
        yield event.plain_result(self._help_text(event))

    # ---------------------------- 账号 ---------------------------- #
    # 说明：裸 `/memo token`（不带参数）**不需要**额外的兜底 handler。
    # 实测 CommandFilter.validate_and_convert_params([], {"action": GreedyStr})
    # 会返回 {'action': ''}，子指令本身就会执行，因此再加一条高优先级兜底会导致
    # 同一条状态被发两次（`yield` 现在走 event.send()，重复是真实可见的）。
    @memo.command("token")
    @_command_guard_send
    async def memo_token(self, event: AstrMessageEvent, action: GreedyStr):
        """绑定、查看或解绑墨墨 Access Token。"""
        raw = (action or "").strip()
        session_id = event.unified_msg_origin

        if raw.lower() in {"clear", "unset", "解绑", "删除"}:
            removed = self.service.tokens.remove_token(session_id)
            self.service.client.set_token(self.service.tokens.get_token(session_id))
            yield event.plain_result(
                "已解绑本会话的 Token。" if removed else "本会话本来就没有绑定 Token。"
            )
            return

        parts = _words(raw)
        token = parts[0] if parts else ""
        if len(token) < 8:
            # 例如 `/memo token foo`：参数太短，给出用法
            yield event.plain_result(
                f"🔑 当前状态：{self.service.status_hint(session_id)}\n\n"
                "用法：`/memo token <你的Token>` 绑定，`/memo token clear` 解绑。"
            )
            return

        # 先校验一次，避免存下一个无效 Token
        self.service.client.set_token(token)
        try:
            await self.service.client.get_study_progress()
        except Exception as exc:  # noqa: BLE001 - 校验失败直接反馈
            self.service.client.set_token(self.service.tokens.get_token(session_id))
            yield event.plain_result(_friendly_error(exc, self))
            return

        self.service.tokens.set_token(session_id, token, source="chat")
        yield event.plain_result(
            "✅ 绑定成功，Token 校验通过。\n"
            "接下来可以试试 `/memo status` 或 `/memo word apple`。"
        )

    @memo.command("ping")
    @_command_guard_send
    async def memo_ping(self, event: AstrMessageEvent):
        """测试墨墨 Token 是否可用。"""
        session_id = event.unified_msg_origin
        client = self.service.require_client(session_id)
        progress = await client.get_study_progress()
        finished = int(progress.get("finished") or 0)
        total = int(progress.get("total") or 0)
        yield event.plain_result(
            f"✅ 墨墨接口连通正常。\n今日进度：{finished}/{total}\n"
            f"鉴权方式：{self.service.status_hint(session_id)}"
        )

    # -------------------------- 学习数据 -------------------------- #
    @memo.command("status", alias={"progress", "进度"})
    @_command_guard_send
    async def memo_status(self, event: AstrMessageEvent):
        """查看今日学习进度。"""
        yield event.plain_result(
            await self.service.study_status(event.unified_msg_origin)
        )

    @memo.command("today")
    @_command_guard_send
    async def memo_today(self, event: AstrMessageEvent, mode: str = "all"):
        """查看今日单词列表。"""
        mapping = {
            "all": None,
            "done": True,
            "finished": True,
            "todo": False,
            "unfinished": False,
            "new": None,
        }
        key = (mode or "all").strip().lower()
        if key not in mapping:
            yield event.plain_result(
                "用法：`/memo today [all|done|todo|new]`，省略则显示全部今日单词。"
            )
            return
        yield event.plain_result(
            await self.service.today_words(
                event.unified_msg_origin, is_finished=mapping[key]
            )
        )

    @memo.command("forgot", alias={"忘记"})
    @_command_guard_send
    async def memo_forgot(self, event: AstrMessageEvent):
        """查看今天标记为「忘记」的单词。"""
        yield event.plain_result(
            await self.service.today_forgotten(event.unified_msg_origin)
        )

    @memo.command("review", alias={"复习"})
    @_command_guard_send
    async def memo_review(self, event: AstrMessageEvent, days: int = 1):
        """查看未来 N 天内需要复习的单词。"""
        yield event.plain_result(
            await self.service.review_plan(event.unified_msg_origin, days=days)
        )

    @memo.command("sticky")
    @_command_guard_send
    async def memo_sticky(self, event: AstrMessageEvent):
        """查看反复忘记的顽固词。"""
        yield event.plain_result(
            await self.service.sticky_words(event.unified_msg_origin)
        )

    # ---------------------------- 查词 ---------------------------- #
    @memo.command("search", alias={"查词", "query"})
    @_command_guard_send
    async def memo_search(self, event: AstrMessageEvent, words: GreedyStr):
        """批量查询单词，返回单词 ID。"""
        yield event.plain_result(
            await self.service.search_words(event.unified_msg_origin, _words(words))
        )

    @memo.command("word", alias={"单词"})
    @_command_guard_send
    async def memo_word(self, event: AstrMessageEvent, word: GreedyStr):
        """查看单词的释义、助记与例句。"""
        yield event.plain_result(
            await self.service.word_detail(event.unified_msg_origin, _clean(word))
        )

    @memo.command("add", alias={"添加"})
    @_command_guard_send
    async def memo_add(self, event: AstrMessageEvent, words: GreedyStr):
        """把单词加入学习规划。"""
        yield event.plain_result(
            await self.service.add_words(event.unified_msg_origin, _words(words))
        )

    @memo.command("advance")
    @_command_guard_send
    async def memo_advance(self, event: AstrMessageEvent, words: GreedyStr):
        """把单词提前到立即复习。"""
        yield event.plain_result(
            await self.service.advance_words(event.unified_msg_origin, _words(words))
        )

    # --------------------------- 云词本 --------------------------- #
    @memo.group("np")
    def memo_np(self):
        """云词本相关指令。"""

    @memo_np.command("list")
    @_command_guard_send
    async def memo_np_list(self, event: AstrMessageEvent):
        """列出云词本。"""
        yield event.plain_result(
            await self.service.list_notepads(event.unified_msg_origin)
        )

    @memo_np.command("get")
    @_command_guard_send
    async def memo_np_get(self, event: AstrMessageEvent, ref: str):
        """按序号或 id 查看云词本详情。"""
        yield event.plain_result(
            await self.service.get_notepad(event.unified_msg_origin, ref)
        )

    @memo_np.command("add", alias={"create"})
    @_command_guard_send
    async def memo_np_add(
        self, event: AstrMessageEvent, title: str, body: GreedyStr
    ):
        """新建云词本；`/memo np add <标题> 词1 词2` 或 `标题 章节名|词1\\n词2`。"""
        if self.service.settings.notepad_creation == "admin" and not event.is_admin():
            yield event.plain_result("⚠️ 当前配置只允许管理员创建云词本。")
            return
        raw = (body or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：`/memo np add <标题> <单词1 单词2 ...>`\n"
                "或带章节：`/memo np add <标题> 第一章|apple\\nbanana`"
            )
            return

        parts = _split_pipe(raw)
        if len(parts) > 1:
            content = "\n".join(parts[1:]).replace("\\n", "\n")
            if parts[0]:
                content = f"# {parts[0]}\n{content}"
            yield event.plain_result(
                await self.service.create_notepad(
                    event.unified_msg_origin, title, content=content
                )
            )
            return

        yield event.plain_result(
            await self.service.create_notepad(
                event.unified_msg_origin, title, words=_words(raw)
            )
        )

    @memo_np.command("push", alias={"append"})
    @_command_guard_send
    async def memo_np_push(
        self, event: AstrMessageEvent, ref: str, body: GreedyStr
    ):
        """向已有云词本追加单词：`/memo np push <序号|id> [章节名|]词1 词2`。"""
        parts = _split_pipe(body)
        chapter = parts[1] if len(parts) > 1 else ""
        yield event.plain_result(
            await self.service.append_words(
                event.unified_msg_origin,
                ref,
                _words(parts[0] if parts else ""),
                chapter=chapter,
            )
        )

    @memo_np.command("del", alias={"delete", "rm"})
    @_command_guard_send
    async def memo_np_del(self, event: AstrMessageEvent, ref: str):
        """删除云词本。"""
        yield event.plain_result(
            await self.service.delete_notepad(event.unified_msg_origin, ref)
        )

    # ---------------------- 助记 / 释义 / 例句 ---------------------- #
    @memo.command("note", alias={"助记"})
    @_command_guard_send
    async def memo_note(self, event: AstrMessageEvent, payload: GreedyStr):
        """为单词添加助记：`/memo note <单词> | <助记> [| 类型]`。"""
        parts = _split_pipe(payload)
        if len(parts) < 2:
            yield event.plain_result(
                "用法：`/memo note <单词> | <助记内容> [| 类型]`\n"
                "类型可选：" + "、".join(NoteType.ALL)
            )
            return
        note_type = parts[2] if len(parts) > 2 and parts[2] else "其他"
        yield event.plain_result(
            await self.service.add_note(
                event.unified_msg_origin, parts[0], parts[1], note_type
            )
        )

    @memo.command("interp", alias={"释义"})
    @_command_guard_send
    async def memo_interp(self, event: AstrMessageEvent, payload: GreedyStr):
        """为单词添加自定义释义：`/memo interp <单词> | <释义> [| 标签]`。"""
        parts = _split_pipe(payload)
        if len(parts) < 2:
            yield event.plain_result(
                "用法：`/memo interp <单词> | <释义内容> [| 标签1,标签2]`"
            )
            return
        tags = parts[2].split(",") if len(parts) > 2 else []
        yield event.plain_result(
            await self.service.add_interpretation(
                event.unified_msg_origin, parts[0], parts[1], tags
            )
        )

    @memo.command("phrase", alias={"例句"})
    @_command_guard_send
    async def memo_phrase(self, event: AstrMessageEvent, payload: GreedyStr):
        """为单词添加例句：`/memo phrase <单词> | <例句> | <翻译>`。"""
        parts = _split_pipe(payload)
        if len(parts) < 2:
            yield event.plain_result("用法：`/memo phrase <单词> | <例句> | <翻译>`")
            return
        translation = parts[2] if len(parts) > 2 else ""
        yield event.plain_result(
            await self.service.add_phrase(
                event.unified_msg_origin, parts[0], parts[1], translation
            )
        )

    # ------------------------------------------------------------------ #
    # LLM 工具（Function Calling）
    #
    # 注意 1：工具函数的 docstring 会被 docstring_parser 解析成 JSON Schema，
    #   因此 **不要** 在 Args 段里写 event 参数 —— 框架以 handler(event, **kwargs)
    #   调用，LLM 若按 schema 传入 event 会触发 TypeError。
    # 注意 2：@filter.llm_tool 下方的装饰器在类体执行阶段就被调用，
    #   所以只能用模块层的 _llm_tool_guard，不能用实例方法。
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="maimemo_study_status")
    @_llm_tool_guard()
    async def tool_study_status(self, event: AstrMessageEvent) -> str:
        """查询墨墨背单词的今日学习进度。

        返回今日已完成词数、目标词数、学习时长，以及学习规划总词数与待复习词数。
        当用户询问「今天背了多少单词」「墨墨进度」「还要背多少」时使用本工具。
        """
        return await self.service.study_status(event.unified_msg_origin)

    @filter.llm_tool(name="maimemo_today_words")
    @_llm_tool_guard()
    async def tool_today_words(
        self, event: AstrMessageEvent, status: str = "all"
    ) -> str:
        """查询墨墨今日单词列表。

        Args:
            status(string): 过滤条件，可选值：all（全部）、done（已完成）、todo（未完成）、new（新学单词）。
        """
        mapping = {
            "all": None,
            "done": True,
            "finished": True,
            "todo": False,
            "unfinished": False,
            "new": None,
        }
        key = (status or "all").strip().lower()
        if key not in mapping:
            return "错误：status 只能是 all / done / todo / new。"
        return await self.service.today_words(
            event.unified_msg_origin, is_finished=mapping[key]
        )

    @filter.llm_tool(name="maimemo_review_plan")
    @_llm_tool_guard()
    async def tool_review_plan(self, event: AstrMessageEvent, days: int = 1) -> str:
        """查询墨墨未来若干天内需要复习的单词。

        Args:
            days(number): 查询未来多少天内到期的复习单词，取值范围 1-365，默认 1。
        """
        return await self.service.review_plan(
            event.unified_msg_origin, days=int(days or 1)
        )

    @filter.llm_tool(name="maimemo_forgotten_words")
    @_llm_tool_guard()
    async def tool_forgotten(self, event: AstrMessageEvent) -> str:
        """查询今天在墨墨中被标记为「忘记」的单词。

        适合在用户说「今天哪些词没记住」「帮我整理生词」时使用。
        """
        return await self.service.today_forgotten(event.unified_msg_origin)

    @filter.llm_tool(name="maimemo_search_words")
    @_llm_tool_guard()
    async def tool_search_words(self, event: AstrMessageEvent, words: str) -> str:
        """在墨墨词库中查询单词是否存在并返回其 ID。

        Args:
            words(string): 要查询的单词，多个单词用空格分隔。
        """
        return await self.service.search_words(event.unified_msg_origin, _words(words))

    @filter.llm_tool(name="maimemo_word_detail")
    @_llm_tool_guard()
    async def tool_word_detail(self, event: AstrMessageEvent, word: str) -> str:
        """查询某个单词在墨墨中的自定义释义、助记与例句。

        Args:
            word(string): 要查询的英文单词，例如 apple。
        """
        return await self.service.word_detail(event.unified_msg_origin, word)

    @filter.llm_tool(name="maimemo_list_notepads")
    @_llm_tool_guard()
    async def tool_list_notepads(self, event: AstrMessageEvent) -> str:
        """列出当前墨墨账号下的云词本。"""
        return await self.service.list_notepads(event.unified_msg_origin)

    @filter.llm_tool(name="maimemo_add_words_to_plan")
    @_llm_tool_guard(write=True)
    async def tool_add_words(
        self, event: AstrMessageEvent, words: str, advance: bool = False
    ) -> str:
        """把单词加入墨墨的学习规划（会修改用户账号数据）。

        Args:
            words(string): 要加入学习规划的单词，多个单词用空格分隔。
            advance(boolean): 是否同时把单词提前到立即复习，默认 false。注意提前复习需要账号等级达到 10 级。
        """
        return await self.service.add_words(
            event.unified_msg_origin, _words(words), advance=bool(advance)
        )

    @filter.llm_tool(name="maimemo_create_notepad")
    @_llm_tool_guard(write=True)
    async def tool_create_notepad(
        self, event: AstrMessageEvent, title: str, words: str
    ) -> str:
        """新建一个墨墨云词本并写入单词（会修改用户账号数据）。

        Args:
            title(string): 云词本标题。
            words(string): 词条内容，多个单词用空格分隔。
        """
        if self.service.settings.notepad_creation == "admin" and not event.is_admin():
            return "错误：当前配置只允许管理员创建云词本。"
        return await self.service.create_notepad(
            event.unified_msg_origin, title, words=_words(words)
        )

    @filter.llm_tool(name="maimemo_add_note")
    @_llm_tool_guard(write=True)
    async def tool_add_note(
        self, event: AstrMessageEvent, word: str, note: str, note_type: str = "其他"
    ) -> str:
        """为墨墨中的某个单词添加助记（会修改用户账号数据）。

        Args:
            word(string): 目标英文单词。
            note(string): 助记内容，例如谐音、词根、联想等。
            note_type(string): 助记类型，可选值：联想、谐音、派生、词根、词源、固搭、语法、对比、近义、反义、扩展、串记、口诀、合成、吐槽、其他、固定搭配、词根词缀、辨析、近反义词、图例。默认「其他」。
        """
        return await self.service.add_note(
            event.unified_msg_origin, word, note, note_type or "其他"
        )


__all__ = ["MaimemoPlugin", "TokenStore"]
