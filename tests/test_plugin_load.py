"""加载验证：把插件当作 AstrBot 插件真实导入，检查注册结果与端到端行为。

直接运行即可（不联网、不启动 AstrBot 服务）::

    python tests/test_plugin_load.py

需要能 import 到 astrbot 包。若 AstrBot 不在 Python 默认搜索路径里，
用环境变量指定源码目录::

    ASTRBOT_APP=/path/to/AstrBot python tests/test_plugin_load.py

也可以直接用 AstrBot 自带的解释器运行（桌面版）::

    "C:/Users/me/AppData/Local/AstrBot/backend/python/python.exe" tests/test_plugin_load.py

脚本会把 AstrBot 的数据目录隔离到插件目录下的 .astrbot_test_root/，不会污染真实安装。
"""

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

# 插件所在目录：tests/ 的上一级
PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_DIR.name

# ---- 0. 确保能 import 到 astrbot ----
ASTRBOT_APP = os.environ.get(
    "ASTRBOT_APP", r"C:\Users\dell\AppData\Local\AstrBot\backend\app"
)
if ASTRBOT_APP and Path(ASTRBOT_APP).is_dir():
    sys.path.insert(0, ASTRBOT_APP)
# 把「插件目录的父目录」放到 sys.path 最前面：直接运行 tests/xxx.py 时
# Python 只会把 tests/ 加进去，而插件需要以包名被导入（AstrBot 就是这样加载的）。
_parent = str(PLUGIN_DIR.parent)
while _parent in sys.path:
    sys.path.remove(_parent)
sys.path.insert(0, _parent)

try:
    import astrbot
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "无法 import astrbot。请把 AstrBot 源码目录加入 PYTHONPATH，"
        "或设置环境变量 ASTRBOT_APP 指向它。\n"
        f"原始错误：{exc}"
    ) from exc
print(f"[..] astrbot 来源：{astrbot.__file__}  v{astrbot.__version__}")

# ---- 1. 用 ASTRBOT_ROOT 把数据目录隔离到插件目录内 ----
TMP_ROOT = PLUGIN_DIR / ".astrbot_test_root"
(TMP_ROOT / "data").mkdir(parents=True, exist_ok=True)
os.environ["ASTRBOT_ROOT"] = str(TMP_ROOT)

import astrbot.core.utils.astrbot_path as astrbot_path

TMP_DATA = Path(astrbot_path.get_astrbot_data_path())
assert str(TMP_DATA).startswith(str(PLUGIN_DIR)), f"数据目录未隔离：{TMP_DATA}"
print(f"[..] 隔离后的 AstrBot 数据目录：{TMP_DATA}")

# ---- 2. 以 AstrBot 的模块路径导入插件 ----
assert (PLUGIN_DIR / "main.py").is_file(), f"插件缺少 main.py：{PLUGIN_DIR}"
pkg = importlib.import_module(PLUGIN_NAME)
main = importlib.import_module(f"{PLUGIN_NAME}.main")
print(f"[OK] 插件模块导入成功：{main.__file__}")

from astrbot.api import AstrBotConfig
from astrbot.core.provider.register import llm_tools
from astrbot.core.star.star_handler import star_handlers_registry

# ---- 3. metadata.yaml 必需字段 ----
import yaml

meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))
missing = [k for k in ("name", "desc", "version", "author") if not meta.get(k)]
assert not missing, f"metadata.yaml 缺少必需字段: {missing}"
assert meta["name"] == PLUGIN_NAME, f"插件名与目录名不一致：{meta['name']}"
assert str(meta["name"]).isidentifier(), "插件名必须是合法 Python 标识符"
print(f"[OK] metadata.yaml 校验通过：{meta['name']} v{meta['version']}")

# ---- 4. 按 star_manager 的方式实例化插件类 ----
config = AstrBotConfig(config_path=str(TMP_DATA / "fake_conf.json"), schema={})
config.update(
    {
        "access_token": "dummy-token-for-verification",
        "enable_llm_tools": True,
        "allow_llm_write": True,
        "output": {"list_limit": 20, "word_limit": 5, "show_attribution": True},
    }
)
plugin = main.MaimemoPlugin(context=None, config=config)
print(f"[OK] 插件类实例化成功；数据目录 = {plugin.service.tokens.data_dir}")

# ---- 5. 指令注册 ----
commands = []
for md in star_handlers_registry:
    if not (md.handler_module_path or "").startswith(PLUGIN_NAME):
        continue
    for f in md.event_filters:
        names = getattr(f, "get_complete_command_names", None)
        if names:
            commands.extend(names())

commands = sorted(set(commands))
expected = [
    "memo help", "memo token", "memo ping", "memo status", "memo today",
    "memo forgot", "memo review", "memo sticky", "memo search", "memo word",
    "memo add", "memo advance", "memo np list", "memo np get", "memo np add",
    "memo np push", "memo np del", "memo note", "memo interp", "memo phrase",
]
missing_cmds = [c for c in expected if c not in commands]
assert not missing_cmds, f"缺失指令: {missing_cmds}"
print(f"[OK] 注册指令 {len(commands)} 个（含别名展开）")

for alias in ("mm", "墨墨"):
    wrong = [c for c in expected if c.replace("memo", alias, 1) not in commands]
    assert not wrong, f"别名 {alias} 未完全展开：{wrong}"
print("[OK] 指令组别名（mm / 墨墨）已展开")

# 裸指令兜底：正则必须精确命中「裸」写法，且不误抢子指令
# 注意 handler 拿到的是已被唤醒阶段剥掉前缀的 message_str，所以这里也先清洗前缀
bare_re = main._BARE_COMMAND
for probe in ["/memo", "memo", "/mm", "/墨墨", "  /memo  "]:
    assert bare_re.match(main._clean_prefix(probe)), f"裸指令正则未命中：{probe}"
for probe in ["/memo help", "/memo status", "/memo token", "/mm add apple"]:
    assert not bare_re.match(main._clean_prefix(probe)), f"裸指令正则误命中：{probe}"
print("[OK] 裸指令兜底正则精确命中裸指令，且不误抢 help/子指令")

# 低优先级的 @filter.regex / 空名子指令都是已知陷阱，确保没有再出现
assert not any(
    "RegexFilter" in type(f).__name__
    for md in star_handlers_registry
    if (md.handler_module_path or "").startswith(PLUGIN_NAME)
    for f in md.event_filters
), "@filter.regex 在类体内拿不到 self，不要使用"
assert "from __future__ import annotations" not in (
    PLUGIN_DIR / "main.py"
).read_text(encoding="utf-8"), "main.py 不能使用 PEP 563，否则 GreedyStr 检测失效"
print("[OK] 未使用 @filter.regex，且 main.py 无 PEP 563")

# ---- 6. LLM 工具及其 JSON Schema ----
tool_names = sorted(t.name for t in llm_tools.func_list if t.name.startswith("maimemo_"))
expected_tools = [
    "maimemo_add_note", "maimemo_add_words_to_plan", "maimemo_create_notepad",
    "maimemo_forgotten_words", "maimemo_list_notepads", "maimemo_review_plan",
    "maimemo_search_words", "maimemo_study_status", "maimemo_today_words",
    "maimemo_word_detail",
]
assert tool_names == expected_tools, f"工具清单不匹配: {tool_names}"
print(f"[OK] 注册 LLM 工具 {len(tool_names)} 个")

_allowed_types = {"string", "number", "object", "array", "boolean"}
for tool in llm_tools.func_list:
    if not tool.name.startswith("maimemo_"):
        continue
    props = (tool.parameters or {}).get("properties", {})
    # 框架以 handler(event, **kwargs) 调用，schema 里绝不能出现 event
    assert "event" not in props, f"{tool.name} 的 schema 泄漏了 event 参数！"
    for key, spec in props.items():
        assert spec.get("type") in _allowed_types, (
            f"{tool.name}.{key} 类型非法：{spec.get('type')}"
        )
    assert tool.description, f"{tool.name} 缺少描述"
print("[OK] 所有工具的 schema 合法且未泄漏 event 参数")

# ---- 7. 渲染链路自检 ----
render = importlib.import_module(f"{PLUGIN_NAME}.render")
svc = importlib.import_module(f"{PLUGIN_NAME}.service")
api = importlib.import_module(f"{PLUGIN_NAME}.maimemo_api")

print("\n[OK] 渲染链路自检：")
print(render.format_study_progress(
    {"finished": 10, "total": 20, "study_time": 1_145_140},
    plan_total=500,
    due_today=42,
))
bundle = {
    "voc": {"id": "v1", "spelling": "apple"},
    "interpretations": [{"interpretation": "n. 苹果", "tags": ["简明"]}],
    "notes": [{"note_type": "谐音", "note": "阿婆吃苹果"}],
    "phrases": [{"phrase": "This is an apple.", "interpretation": "这是一个苹果。"}],
}
text = render.format_word_bundle(bundle)
assert render.ATTRIBUTION in text, "展示墨墨内容时必须带来源标注"
print(text)

# ---- 8. Token 存储 ----
store = plugin.service.tokens
store.set_token("test:session:1", "tok-abcdefghijklmnop")
assert store.get_token("test:session:1") == "tok-abcdefghijklmnop"
assert store.masked("test:session:1").startswith("tok-ab")
assert str(TMP_DATA) in str(Path(store.path).parent)
store.remove_token("test:session:1")
print("\n[OK] Token 存储读写正常（落在 data/plugin_data 而非插件目录）")


# ---- 9. 打桩 HTTP 层，端到端跑通 service ----
class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload, ensure_ascii=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """按 (method, path) 返回预设信封，并记录调用。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    @property
    def closed(self):
        return False

    def request(self, method, url, params=None, json=None, headers=None):
        path = url.split("/open/api/v1", 1)[-1]
        self.calls.append((method, path, json))
        if (method, path) not in self.routes:
            return _FakeResponse(
                404, {"errors": [{"code": "common_not_found"}], "success": False}
            )
        status, payload = self.routes[(method, path)]
        return _FakeResponse(status, payload)

    async def close(self):
        return None


def envelope(data):
    """官方成功响应形态。"""
    return 200, {"data": data, "errors": [], "success": True}


async def run_service_checks():
    client = plugin.service.client
    fake = _FakeSession({
        ("POST", "/study/get_study_progress"): envelope(
            {"progress": {"finished": 12, "total": 30, "study_time": 900_000}}
        ),
        ("POST", "/study/query_study_records"): envelope({"records": [], "count": 678}),
        ("GET", "/vocabulary"): envelope({"voc": {"id": "V_APPLE", "spelling": "apple"}}),
        ("POST", "/vocabulary/query"): envelope(
            {"voc": [{"id": "V_APPLE", "spelling": "apple"}]}
        ),
        ("GET", "/notes"): envelope(
            {"notes": [{"id": "n1", "note_type": "谐音", "note": "阿婆吃苹果"}]}
        ),
        ("GET", "/interpretations"): envelope(
            {"interpretations": [
                {"id": "i1", "interpretation": "n. 苹果", "tags": ["简明"]}
            ]}
        ),
        ("GET", "/phrases"): envelope(
            {"phrases": [
                {"id": "p1", "phrase": "This is an apple.",
                 "interpretation": "这是一个苹果。"}
            ]}
        ),
        ("GET", "/notepads"): envelope({"notepads": [
            {"id": "NP1", "title": "GRE高频词", "brief": "GRE", "type": "NOTEPAD",
             "status": "PUBLISHED", "tags": ["GRE"]},
        ]}),
        ("GET", "/notepads/NP1"): envelope({"notepad": {
            "id": "NP1", "title": "GRE高频词", "brief": "GRE", "status": "PUBLISHED",
            "tags": [], "content": "# C1\napple\nbanana",
            "list": [{"type": "CHAPTER", "data": {"chapter": "C1"}},
                     {"type": "WORD", "data": {"word": "apple"}},
                     {"type": "WORD", "data": {"word": "banana"}}],
        }}),
        ("POST", "/study/add_words"): envelope({"added_count": 1}),
        ("POST", "/study/advance_study"): envelope({"advanced_count": 1}),
        ("POST", "/notes"): envelope({"note": {"id": "n2"}}),
    })
    client._session = fake
    client.set_token("fake-token-1234567890")
    sid = "test:GroupMessage:10086"

    status_text = await plugin.service.study_status(sid)
    assert "12/30" in status_text and "678" in status_text, status_text
    print("\n[OK] study_status（信封解包 + 统计）：\n" + status_text)

    print("\n[OK] search_words：")
    print(await plugin.service.search_words(sid, ["apple", "banana"]))

    detail = await plugin.service.word_detail(sid, "apple")
    assert render.ATTRIBUTION in detail, detail
    print("\n[OK] word_detail（含来源标注）：\n" + detail)

    listing = await plugin.service.list_notepads(sid)
    assert "GRE高频词" in listing
    print("\n[OK] list_notepads：\n" + listing)
    assert plugin.service._resolve_notepad_ref(sid, "1") == "NP1"
    print("[OK] 云词本序号 1 -> NP1")

    print("\n[OK] add_words：")
    print(await plugin.service.add_words(sid, ["apple"], advance=True))
    print("\n[OK] advance_words：")
    print(await plugin.service.advance_words(sid, ["apple"]))
    print("\n[OK] add_note：")
    print(await plugin.service.add_note(sid, "apple", "阿婆吃苹果", "谐音"))

    # 错误分类：401 -> 提示重新获取 Token
    fake.routes[("POST", "/study/get_study_progress")] = (
        401,
        {"errors": [{"code": "common_unauthorized", "msg": "Authorization failed"}],
         "success": False},
    )
    try:
        await plugin.service.study_status(sid)
    except svc.MaimemoServiceError as exc:
        assert "Token" in str(exc), exc
        print(f"\n[OK] 401 被转换为友好提示：{exc}")
    else:
        raise AssertionError("401 应当抛出 MaimemoServiceError")

    # 完全未配置 Token
    bare_service = svc.MaimemoService(
        str(TMP_DATA / "plugin_data" / "bare"), svc.PluginSettings()
    )
    try:
        await bare_service.word_detail("test:GroupMessage:1", "apple")
    except svc.MaimemoServiceError as exc:
        assert "Token" in str(exc), exc
        print(f"[OK] 完全未配置 Token 时提示：{str(exc).splitlines()[0]}")
    else:
        raise AssertionError("未配置 Token 应当抛出 MaimemoServiceError")

    # 未绑定会话应回退到全局 Token
    client.set_token("")
    assert plugin.service.tokens.get_token(sid) == "dummy-token-for-verification"
    assert plugin.service.status_hint(sid) == "使用插件配置中的全局 Token"
    print("[OK] 会话未绑定时正确回退到全局 Token")

    # 本地限流器：20 次 / 10 秒
    limiter = api.MaimemoClient(access_token="x")
    for i in range(21):
        try:
            limiter._charge_rate_limit()
        except api.MaimemoRateLimitError as exc:
            assert i == 20, f"限流触发时机不对：第 {i + 1} 次"
            print(f"[OK] 限流器在第 21 次请求时拦截：{exc}")
            break
    else:
        raise AssertionError("限流器未生效")

    # 无信封形态的兼容
    client.set_token("fake-token-1234567890")
    fake.routes[("POST", "/study/get_study_progress")] = (
        200, {"progress": {"finished": 1, "total": 2, "study_time": 1000}}
    )
    bare = await client.get_study_progress()
    assert bare["finished"] == 1, bare
    print("[OK] 兼容无信封的响应形态")

    # 缓存命中
    before = len(fake.calls)
    await client.resolve_voc_id("apple")
    await client.resolve_voc_id("apple")
    assert len(fake.calls) == before, "缓存未命中，重复请求了墨墨接口"
    print("[OK] voc_id 缓存生效，未重复请求")

    # 查不到单词时报错清晰
    try:
        await client.get_voc("zzzznotaword")
    except api.MaimemoAPIError as exc:
        print(f"[OK] 查不到单词时报错清晰：{exc}")


asyncio.run(run_service_checks())

# ---- 10. GreedyStr 必须能吞掉整行剩余文本 ----
for md in star_handlers_registry:
    if (md.handler_module_path or "").endswith("main") and md.handler_name == "memo_search":
        assert md.event_filters[0].handler_params["words"] is main.GreedyStr, (
            "memo_search 的 words 参数未使用 GreedyStr —— 多词参数会被截断。"
            "检查是否给 GreedyStr 加了默认值，"
            "或文件里是否误加了 from __future__ import annotations"
        )
        cf = md.event_filters[0]
        joined = cf.validate_and_convert_params(
            ["apple", "banana", "cherry"], cf.handler_params
        )
        assert joined["words"] == "apple banana cherry", joined
        print(
            f"\n[OK] GreedyStr 吞参生效："
            f"['apple','banana','cherry'] -> {joined['words']!r}"
        )
        break
else:
    raise AssertionError("未找到 memo_search 的注册信息")

assert "from __future__ import annotations" not in (
    PLUGIN_DIR / "main.py"
).read_text(encoding="utf-8"), "main.py 不能使用 PEP 563，否则 GreedyStr 检测会失效"
print("[OK] main.py 未使用 from __future__ import annotations")

# 其他指令的参数类型逐项打印，便于人工核对
for md in star_handlers_registry:
    if (md.handler_module_path or "").endswith("main") and md.handler_name in {
        "memo_today", "memo_review", "memo_np_get", "memo_np_add", "memo_token",
    }:
        cf = md.event_filters[0]
        print(f"    {md.handler_name}: {cf.print_types()}")

# ---- 11. 生命周期 ----
asyncio.run(plugin.terminate())
print("[OK] terminate() 正常执行")

print("\n===== 全部验证通过 =====")
