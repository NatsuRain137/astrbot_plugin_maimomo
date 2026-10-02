"""自动回复回归测试：复刻 AstrBot 的真实消息管道，验证指令结果会直接发给用户。

这个测试专门覆盖曾经出过的一类问题：**指令结果被 LLM 回复覆盖**。
AstrBot 的 `ProcessStage` 只在 `event._has_send_oper` 为真时才跳过 LLM：

    if not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm:
        ...  # 调用 LLM

而 `yield result` 只走 `event.set_result()`（并不发送），所以若插件没有真正发送过
消息，LLM 的回复会把指令结果覆盖掉 —— 用户只会看到 AI 的转述。

**测不到这一层是之前的主要盲点**：`test_plugin_load.py` 只检查注册，不检查管道。

复刻的关键行为：
  1. star_manager 载入插件时执行 functools.partial(handler, star_cls) 绑定实例
  2. 唤醒阶段按 priority 从大到小评估 handler；filter 抛异常 → 发报错 + stop_event()
  3. 子指令（CommandFilter）也会被激活；带 CommandGroupFilter 的组 handler 不会
  4. ProcessStage 只在 `not _has_send_oper and is_at_or_wake_command` 时才调用 LLM
  5. RespondStage 读取 event.get_result() 并 event.send()
"""

import asyncio
import functools
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_DIR.name

ASTRBOT_APP = os.environ.get(
    "ASTRBOT_APP", r"C:\Users\dell\AppData\Local\AstrBot\backend\app"
)
if ASTRBOT_APP and Path(ASTRBOT_APP).is_dir():
    sys.path.insert(0, ASTRBOT_APP)
parent = str(PLUGIN_DIR.parent)
while parent in sys.path:
    sys.path.remove(parent)
sys.path.insert(0, parent)

try:
    import astrbot
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "无法 import astrbot，请设置 ASTRBOT_APP 指向 AstrBot 源码目录。"
        f"\n原始错误：{exc}"
    ) from exc

TMP_ROOT = PLUGIN_DIR / ".astrbot_pipe_root"
(TMP_ROOT / "data").mkdir(parents=True, exist_ok=True)
os.environ["ASTRBOT_ROOT"] = str(TMP_ROOT)

import importlib  # noqa: E402

from astrbot.api import AstrBotConfig  # noqa: E402
from astrbot.core.message.components import Plain  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402
from astrbot.core.pipeline.context_utils import call_handler  # noqa: E402
from astrbot.core.pipeline.respond.stage import RespondStage  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.platform.astrbot_message import (  # noqa: E402
    AstrBotMessage,
    MessageMember,
)
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.star.filter.command_group import CommandGroupFilter  # noqa: E402
from astrbot.core.star.filter.permission import PermissionTypeFilter  # noqa: E402
from astrbot.core.star.star import star_map  # noqa: E402
from astrbot.core.star.star_handler import (  # noqa: E402
    EventType,
    star_handlers_registry,
)

main = importlib.import_module(f"{PLUGIN_NAME}.main")

_BOUND: set[str] = set()


def bind_handlers(plugin_obj) -> None:
    """复刻 star_manager.py 的 handler 绑定（functools.partial）。"""
    for md in star_handlers_registry:
        if not (md.handler_module_path or "").startswith(PLUGIN_NAME):
            continue
        if md.handler_full_name in _BOUND:
            continue
        md.handler = functools.partial(md.handler, plugin_obj)
        _BOUND.add(md.handler_full_name)


class RecordingEvent(AstrMessageEvent):
    """把 send() 记下来，模拟真实平台适配器。"""

    def __init__(self, message_str: str) -> None:
        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.self_id = "bot"
        abm.session_id = "user1"
        abm.message_id = "m1"
        abm.sender = MessageMember(user_id="user1", nickname="tester")
        abm.message = [Plain(message_str)]
        abm.message_str = message_str
        super().__init__(
            message_str,
            abm,
            PlatformMetadata(name="aiocqhttp", id="p1", description="test"),
            "user1",
        )
        self.sent: list[str] = []

    async def send(self, message: MessageChain) -> None:
        self._has_send_oper = True
        if message is not None:
            text = message.get_plain_text()
            if text:
                self.sent.append(text)


def waking_stage(event: RecordingEvent, config) -> tuple[list, str]:
    """复刻 WakingCheckStage：唤醒判定 + 逐个 handler 跑 filter。"""
    event.message_str = event.message_str.strip()
    for prefix in config.get("wake_prefix", ["/"]):
        if event.message_str.startswith(prefix):
            event.is_wake = True
            event.is_at_or_wake_command = True
            event.message_str = event.message_str[len(prefix):].strip()
            break

    activated = []
    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent
    ):
        if not (handler.handler_module_path or "").startswith(PLUGIN_NAME):
            continue
        if not handler.event_filters:
            continue
        passed = True
        for flt in handler.event_filters:
            try:
                if isinstance(flt, PermissionTypeFilter):
                    if not flt.filter(event, config):
                        passed = False
                        break
                elif not flt.filter(event, config):
                    passed = False
                    break
            except Exception as exc:
                name = star_map[handler.handler_module_path].name
                # AstrBot 的真实行为：发报错消息 + 打断整条管道
                event.sent.append(f"插件 {name}: {exc}")
                event._has_send_oper = True
                event.stop_event()
                return activated, f"{type(exc).__name__}: {exc}"
        if passed:
            event.is_wake = True
            if not any(
                isinstance(f, CommandGroupFilter) for f in handler.event_filters
            ):
                activated.append(handler)
                params = event.get_extra("parsed_params")
                if params:
                    event.set_extra(f"__p__{handler.handler_full_name}", params)
        event._extras.pop("parsed_params", None)
    return activated, ""


async def process_stage(event: RecordingEvent, activated: list, respond) -> bool:
    """返回 True 表示还会调用 LLM（即指令结果会被 LLM 覆盖）。"""
    for handler in activated:
        if event.is_stopped():
            break
        params = event.get_extra(f"__p__{handler.handler_full_name}") or {}
        async for _ in call_handler(event, handler.handler, **params):
            pass
    await respond.process(event)
    return (
        not event._has_send_oper
        and event.is_at_or_wake_command
        and not event.call_llm
    )


def make_respond(config) -> RespondStage:
    r = RespondStage()
    r.ctx = type("C", (), {"astrbot_config": config})()
    r.config = config
    r.platform_settings = config.get("platform_settings", {})
    r.reply_with_mention = False
    r.reply_with_quote = False
    r.enable_seg = False
    r.only_llm_result = True
    r.interval_method = "random"
    r.log_base = 2.6
    r.interval = [1.5, 3.5]
    return r


async def main_test() -> None:
    config = AstrBotConfig(
        config_path=str(TMP_ROOT / "data" / "fake_conf.json"), schema={}
    )
    config.update({
        "access_token": "dummy-token",
        "output": {"list_limit": 20, "word_limit": 5, "show_attribution": True},
    })
    plugin = main.MaimemoPlugin(context=None, config=config)
    bind_handlers(plugin)
    respond = make_respond(config)

    # 这些不需要真实 Token：help/token 是本地文案；status/np list 会因假 Token 报鉴权错，
    # 但同样必须「直接把错误发给用户」而不是交给 LLM。
    cases = [
        "/memo help",
        "/mm help",
        "/memo token",
        "/memo token clear",
        "/memo status",
        "/memo np list",
    ]

    print("=== 自动回复回归测试（waking → process → respond）===")
    failures = []
    for text in cases:
        event = RecordingEvent(text)
        activated, err = waking_stage(event, config)
        if err:
            print(f"[FAIL] {text!r:18} filter 异常打断管道：{err}")
            failures.append(text)
            continue
        would_call_llm = await process_stage(event, activated, respond)

        if not event.sent:
            print(f"[FAIL] {text!r:18} 没有任何回复（会被 LLM 接管）")
            failures.append(text)
            continue
        if len(event.sent) != 1:
            print(f"[FAIL] {text!r:18} 重复回复 {len(event.sent)} 次")
            failures.append(text)
            continue
        if would_call_llm:
            print(f"[FAIL] {text!r:18} 仍会进 LLM，结果会被覆盖")
            failures.append(text)
            continue

        first = event.sent[0].strip().splitlines()[0]
        print(f"[OK ] {text!r:18} 1 条回复，跳过 LLM → {first[:52]}")

    print()
    if failures:
        print("未通过的指令：", failures)
        raise SystemExit(1)
    print("所有指令结果都直接发给用户，且 _has_send_oper=True → AstrBot 跳过 LLM")


asyncio.run(main_test())
