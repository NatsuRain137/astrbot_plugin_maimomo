# AstrBot 插件 API 契约参考（v4.26.4 实测）

编写本插件时逐条核对过的 AstrBot 插件 API 要点。核对方式是**直接读本机安装的
AstrBot 4.26.4 源码**（`%LOCALAPPDATA%\AstrBot\backend\app\astrbot\`）
并用 `tests/test_plugin_load.py` 做真实加载验证，不是仅凭文档推断。

## 1. 目录布局与必需文件

```
data/plugins/astrbot_plugin_xxx/
├── metadata.yaml      # 必需（或 metadata.yml）
├── main.py            # 必需，插件类必须在这个文件里
├── requirements.txt   # 可选
├── _conf_schema.json  # 可选
├── logo.png           # 可选，1:1，推荐 256x256
└── tests/、docs/       # 可选，随插件分发但不会被加载
```

数据落盘位置（**不要**写进插件自身目录）：

* 插件配置：`data/config/<插件目录名>_config.json`（由 schema 自动生成）
* 插件数据：`data/plugin_data/<插件名>/`

## 2. metadata.yaml

**必需字段**：`name`、`desc`、`version`、`author`（`description` 可作为 `desc` 的别名）。
缺失或非非空字符串会导致插件加载失败。`name` 必须是合法 Python 标识符、不含 `/`。

可选字段：`repo`、`display_name`、`short_desc`、`support_platforms`(list[str])、
`astrbot_version`(PEP 440 范围，如 `">=4.14,<5"`，**不加 v 前缀**)、`pages`。

> `help` / `tags` / `keywords` / `social_link` / `entry` 在 4.26.4 源码中**未被读取**，
> 写了不产生运行时行为；入口固定是 `main.py`。

## 3. 插件类

```python
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools


class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config

    async def initialize(self) -> None: ...   # 激活时调用
    async def terminate(self) -> None: ...    # 禁用/重载时调用
```

`Star.__init__(self, context, config=None)`；框架会按
`star_cls_type(context=..., config=...)` 实例化，`TypeError` 时回退为只传 `context`，
所以两种签名都能用。框架还会注入类属性 `name`、`author`、`plugin_id`。

`Star` 提供 `self.logger`（插件专属 logger）、`text_to_image()`、`html_render()`，
以及 KV 存储 `put_kv_data()` / `get_kv_data()` / `delete_kv_data()`（v4.9.2+）。

## 4. _conf_schema.json

支持的 `type`：`string`、`text`、`int`、`float`、`bool`、`list`、`file`、
`object`、`template_list`。

⚠️ **`dict` 不在支持列表里**（尽管文档说支持），用了会抛
`TypeError: 不受支持的配置类型`，直接导致插件加载失败。需要字典时用
`"type": "object"` + `"items": {...}`。

配置项可用键：`type`(必填)、`description`、`hint`、`obvious_hint`、`default`、
`items`(object 用)、`invisible`、`secret`(string/list)、`options`、`labels`、
`slider`(int/float)、`editor_mode`/`editor_language`/`editor_theme`、
`_special`(如 `select_provider`、`select_persona`、`select_knowledgebase`)、
`file_types`(file)、`templates`/`template_schema`。

读取：`AstrBotConfig` 继承 `dict`，支持 `.get(k, default)` / `[k]` / `.k`；
保存用 **同步** 的 `self.config.save_config()`（注意它同时也会写回 `data/` 下的配置实体
—— 直接手改配置并保存会影响全局配置，插件应尽量避免）。

## 5. 指令与指令组

```python
@filter.command("hello", alias={"你好"})
async def hello(self, event: AstrMessageEvent):
    yield event.plain_result("hi")

@filter.command_group("memo", alias={"mm"})
def memo(self):
    ...

@memo.command("sub")          # 子指令
async def sub(self, event): ...

@memo.group("np")             # 嵌套组用 .group()，不是 .command_group()
def np(self): ...

@np.command("list")
async def np_list(self, event): ...
```

* **指令名不能带空格**（会被解析成第二个参数）——需要多级时用指令组。
* 参数解析规则（`CommandFilter.validate_and_convert_params`）：
  消息先 `\s+`→单空格归一化，再按空格切词；跳过前两个参数 `self`、`event`，
  其余按签名一一对应。**多余 token 被静默忽略**。
* 无默认值的参数缺失 → `ValueError("必要参数缺失。...")`，
  框架会把这条错误**直接发给用户**。因此「可选参数」建议用默认值。
* 取「指令后的全部剩余文本」要用 `GreedyStr`：

```python
from astrbot.core.star.filter.command import GreedyStr

@filter.command("search")
async def search(self, event: AstrMessageEvent, words: GreedyStr):
    ...  # /search apple banana -> "apple banana"
```

### ⚠️ GreedyStr 的两个大坑（本项目都踩过）

1. **不能给 GreedyStr 参数加默认值**。
   `CommandFilter.init_handler_md` 的逻辑是「有默认值就存默认值，没有才存注解」，
   而 `validate_and_convert_params` 判断吞参用的是
   `param_type_or_default_val is GreedyStr`（**类身份比较**）。
   一旦写成 `words: GreedyStr = ""`，存进去的是 `""`，判断失败，
   该参数退化成普通 `str`，**只取第一个词，其余静默丢弃**。
2. **不能在 main.py 里写 `from __future__ import annotations`**。
   这样一来注解变成字符串，`inspect.signature(..., eval_str=True)` 求值后
   依然是字符串 `'GreedyStr'`，`is GreedyStr` 同样失败。
   官方内建指令文件也没有这个 future import。

两条都要满足，`GreedyStr` 才会真正生效。

## 6. 事件钩子

```python
@filter.on_astrbot_loaded()          # (self)
@filter.on_waiting_llm_request()     # (self, event) 拿锁前；不能 yield，用 await event.send(...)
@filter.on_llm_request()             # (self, event, req: ProviderRequest) 必须 3 个参数
@filter.on_llm_response()            # (self, event, response)
@filter.on_agent_begin() / on_agent_done()
@filter.on_using_llm_tool()          # (self, event, tool, tool_args)
@filter.on_llm_tool_respond()        # (self, event, tool, tool_args, tool_result)
@filter.on_decorating_result() / after_message_sent()
@filter.on_plugin_loaded() / on_plugin_unloaded() / on_plugin_error()
```

钩子**不能**与 `command` / `command_group` / `event_message_type` /
`platform_adapter_type` / `permission_type` 混用。

## 7. 事件 API（AstrMessageEvent）

常用属性：`message_str`、`message_obj`、`session`、`unified_msg_origin`
（`platform:message_type:session_id`，**推荐作为存储 key**）、`session_id`、
`role`、`is_wake`、`is_at_or_wake_command`。

常用方法：

```python
event.get_sender_id() / get_sender_name() / get_group_id() / get_session_id()
event.get_platform_name() / get_platform_id()
event.is_admin() / is_private_chat() / is_wake_up() / is_stopped()
event.set_extra(k, v) / get_extra(k, default) / clear_extra()
event.plain_result(text) / image_result(url_or_path) / chain_result(chain) / make_result()
await event.send(MessageChain(...))     # 参数是 MessageChain，不是 str
event.stop_event() / continue_event()
event.set_result(...) / get_result() / clear_result()
```

## 8. LLM 工具（Function Calling）

```python
@filter.llm_tool(name="get_weather")
async def get_weather(self, event: AstrMessageEvent, location: str) -> str:
    """查询指定城市的实时天气。

    Args:
        location(string): 城市名称
    """
    return f"{location} 今天晴"
```

* 实现用 `docstring_parser.parse(func_doc)`：**工具描述取 docstring 第一段，
  参数 schema 完全来自 `Args:` 段**。注释里缺类型 → 注册时抛
  `ValueError: ... 缺少类型注释`；`Args:` 段写错 → schema 为空 →
  LLM 传的参数被静默丢弃 → 调用时缺参报错。
* 支持的类型：`string`、`number`、`object`、`array`、`boolean`；
  数组子类型写 `array[string]`。
* 返回值：**所有非 None 值都会被 `str()` 后回给 LLM**；
  `yield event.plain_result(...)` + 返回 `None` 才是「直接发给用户、不进下一轮 prompt」。

### ⚠️ 不要在 docstring 里写 event 参数

调用链是 `_PermissionGuardedTool.call` →
`self._wrapped.handler(event, **kwargs)`，其中 `**kwargs` 是 LLM 按 schema 传来的参数。
若 schema 里含 `event`，LLM 可能传 `event=...`，与位置参数冲突直接
`TypeError`。**Args 段里只列业务参数。**

### ⚠️ 装饰器求值时机

`@filter.llm_tool(...)` 下方的装饰器是在**类体执行阶段**被调用的，那时还没有实例。
所以包装器必须是**模块层函数**（或用别的方式避开 `self`），写成实例方法是
`TypeError: missing 1 required positional argument: 'self'`。

## 9. 其它

* `StarTools.get_data_dir(plugin_name=None)` → `Path`，指向
  `data/plugin_data/<插件名>`，目录会自动创建。
* 网络请求用 `aiohttp` / `httpx`；**不要用 `requests`**（会阻塞事件循环）。
* `Context` 常用：`get_using_provider(umo)`、`get_config(umo)`、
  `send_message(session, chain)`、`get_llm_tool_manager()`、`add_llm_tools(...)`。
* 插件名建议 `astrbot_plugin_` 前缀、全小写、无空格。
* 发布：推到 GitHub 后在 <https://cloud.astrbot.app/publish> 提交，
  zip 不得超过 16MB。
