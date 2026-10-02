# 墨墨背单词助手 · astrbot_plugin_maimemo

把 [墨墨背单词（MaiMemo）开放 API](https://open.maimemo.com/) 接进 AstrBot：
既可以在聊天里用 `/memo` 指令手动操作，也可以让 AI 通过 Function Calling 工具自己调用。

## 功能

**聊天指令 `/memo`**（别名 `/mm`、`/墨墨`）

| 分类 | 指令 | 说明 |
| --- | --- | --- |
| 账号 | `token <Token>` / `token` / `token clear` | 绑定 / 查看 / 解绑本会话的 Access Token |
| 账号 | `ping` | 测试 Token 是否可用 |
| 学习数据 | `status` | 今日进度、剩余词数、学习时长、规划总词数、待复习数 |
| 学习数据 | `today [all\|done\|todo\|new]` | 今日单词列表 |
| 学习数据 | `forgot` | 今天标记为「忘记」的单词 |
| 学习数据 | `review [天数]` | 未来 N 天内待复习的单词 |
| 学习数据 | `sticky` | 反复忘记的「顽固词」 |
| 查词 | `search 词1 词2 …` | 批量查询单词 ID |
| 查词 | `word <单词>` | 该词的自定义释义 / 助记 / 例句 |
| 生词 | `add 词1 词2 …` | 加入学习规划 |
| 生词 | `advance 词1 词2 …` | 提前到立即复习（需账号等级 ≥ 10） |
| 云词本 | `np list` | 列出云词本 |
| 云词本 | `np get <序号\|id>` | 查看云词本详情 |
| 云词本 | `np add <标题> 词1 词2 …` | 新建云词本 |
| 云词本 | `np add <标题> 章节名\|词1\n词2` | 带章节新建 |
| 云词本 | `np push <序号\|id> 词1 词2 …` | 追加单词 |
| 云词本 | `np push <序号\|id> 章节名\|词1 词2` | 追加到指定章节 |
| 云词本 | `np del <序号\|id>` | 删除云词本 |
| 写入 | `note <单词> \| <助记> [\| 类型]` | 新增助记 |
| 写入 | `interp <单词> \| <释义> [\| 标签,标签]` | 新增自定义释义 |
| 写入 | `phrase <单词> \| <例句> \| <翻译>` | 新增例句 |

直接发送 `/memo` 或 `/memo help` 会打印完整帮助。

**LLM 工具（共 10 个）**

只读：`maimemo_study_status`、`maimemo_today_words`、`maimemo_review_plan`、
`maimemo_forgotten_words`、`maimemo_search_words`、`maimemo_word_detail`、`maimemo_list_notepads`

写操作：`maimemo_add_words_to_plan`、`maimemo_create_notepad`、`maimemo_add_note`

开启后可以直接对 Bot 说「我今天墨墨背了多少单词」「帮我把这几个词加进墨墨」。

## 安装

1. 把 `astrbot_plugin_maimemo` 目录放进 AstrBot 的 `data/plugins/` 下。
2. 在 WebUI 的插件页重载插件。
3. 在插件配置里填入墨墨 Access Token（可选，也可以让每个会话自行绑定）。

插件只依赖 AstrBot 自带的 `aiohttp` 与标准库，无需额外安装依赖。

## 获取 Access Token

**方式一（推荐）**：打开墨墨背单词 App → 「我的」→ 「开放 API」，复制 Token。

**方式二**：在浏览器登录 <https://open.maimemo.com/open/api/v1/tokens/openapi> 后复制 Token。
⚠️ 网页来源的 Token **有效期为 7 天**，过期后需要重新获取。

拿到 Token 后有两种用法：

* **每个会话独立绑定**（适合多人群聊，各自用自己的账号）：
  在聊天里发送 `/memo token <你的Token>`。插件会先校验再保存，明文只落地在
  `data/plugin_data/astrbot_plugin_maimemo/tokens.json`。
* **全局默认 Token**：填在插件配置的 `access_token` 里，所有未单独绑定的会话共用。
  此方式下**任意能触发 Bot 的人都将操作同一个墨墨账号**，多人场景请谨慎使用。

## 配置项

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `access_token` | string(secret) | 空 | 全局默认 Access Token |
| `base_url` | string | `https://open.maimemo.com/open/api/v1` | 开放 API 地址 |
| `timeout` | int | 20 | 请求超时（秒） |
| `max_retries` | int | 2 | 网络错误与 5xx 的重试次数 |
| `enable_llm_tools` | bool | true | 是否注册 LLM 工具 |
| `allow_llm_write` | bool | **false** | 是否允许 AI 执行写操作（改账号数据） |
| `notepad_creation` | string | `everyone` | 云词本创建权限：`everyone` / `admin` |
| `output.list_limit` | int | 20 | 列表类结果最大条数 |
| `output.word_limit` | int | 5 | 每类内容（释义/助记/例句）最大条数 |
| `output.show_attribution` | bool | true | 展示墨墨内容时附带来源标注 |

> `allow_llm_write` 默认关闭：AI 只能读取数据，不会改动你的墨墨账号。
> 需要让 AI 帮忙建词本、加单词时再打开。

## 设计说明

* **限流**：墨墨官方限流为 20 次/10 秒、40 次/60 秒、2000 次/5 小时。
  插件在客户端侧复刻了这三个滑动窗口，超限时直接给出友好提示，而不是把 429 抛给用户。
* **缓存**：只有 `单词拼写 → voc_id` 这种**不含版权内容**的标识数据会落盘缓存（TTL 7 天），
  用于减少 API 调用。释义 / 助记 / 例句等**内容数据不做持久化缓存**。
* **错误处理**：所有底层异常统一转换为面向用户的中文提示；报错文本会抹掉 Token 明文。
* **Token 隔离**：会话级 Token 存在 `data/plugin_data/` 而非插件目录，插件更新不会丢数据。
* **参数解析**：多词参数（`search` / `add` / `note` 等）使用 AstrBot 的 `GreedyStr` 注解，
  因此**不能**给这些参数加默认值——一加默认值 AstrBot 就会把它当普通 `str`，只取第一个词。

## 版权与合规

墨墨开放平台服务协议要求：展示其词汇内容（释义 / 助记 / 例句）时应标注内容来源，
且不得持久化汇编、二次分发或用于 AI 训练。本插件在相关输出末尾附加
「—— 内容来自墨墨背单词」，且不持久化、不转发这些内容。请遵守
[墨墨开放平台服务协议](https://memodocs.maimemo.com/docs/open-terms)。

## 已知限制

### 与 AstrBot 交互相关的限制

* **裸 `/memo`（以及 `/mm`、`/墨墨`）不会显示本插件的帮助。**
  原因是 AstrBot 的 `CommandGroupFilter` 遇到裸指令组名会抛「参数不足」并
  `stop_event()` 打断整条消息管道，而 `RespondStage` 在 `ProcessStage` 之后运行，
  所以插件在 filter 阶段设置的任何结果都发不出去。此时你会收到 AstrBot 自己输出的
  指令树（内容等价，只是排版不同）。**请用 `/memo help` 查看本插件的完整帮助。**
* **已修：指令结果曾被 LLM 回复覆盖。** AstrBot 的 `ProcessStage` 只在
  `event._has_send_oper` 为真时才跳过 LLM，而 `yield result` 只调用 `set_result()`
  并不发送，因此 LLM 的回复会把指令结果覆盖掉（表现为「数据要经 AI 转述」）。
  现在每个指令都会主动 `event.send()`，立刻置真 `_has_send_oper`，AstrBot 随即跳过
  LLM。回归测试见 `tests/test_pipeline_dispatch.py`。
* 若墨墨返回参数类错误，插件会把 `limit` 降级为官方默认值 10 重试一次
  （官方 CLI 固定用 10，服务端对 `limit` 可能有未公开上限）。

### 墨墨官方接口未开放的能力

以下能力**墨墨开放 API 没有提供**，插件无法实现，请勿期待：

* 连续打卡天数、打卡日历、补签
* 历史每日学习量与按天学习时长
* 词库 / 单词书列表（如「四级词汇」词表）
* 云词本的「未学习 / 已学习」分组
* 单词的官方释义、词频、柯林斯、联想等**只读**内容接口
  （`/interpretations`、`/notes`、`/phrases` 都只操作**当前用户自建**的内容）

另外学习数据类接口（`/study/*`）目前是**公测**状态：需在墨墨 App 内开启自动同步，
且当日打开过 App 才能准确计算。

## 文件结构

```
astrbot_plugin_maimemo/
├── main.py            # 插件入口：指令组 + LLM 工具注册
├── service.py         # 业务门面：把聊天需求翻译成 API 调用
├── maimemo_api.py     # 墨墨开放 API 异步客户端（可独立复用/测试）
├── render.py          # 把 API 数据渲染成聊天文本
├── token_store.py     # 会话级 Token 持久化
├── _conf_schema.json  # 插件配置 Schema
├── metadata.yaml      # 插件元数据
├── requirements.txt
├── tests/
│   ├── test_plugin_load.py        # 加载、注册、LLM 工具 schema、打桩端到端
│   └── test_pipeline_dispatch.py  # 复刻真实消息管道，验证「指令结果直接回复用户」
└── docs/
    ├── maimemo-open-api-reference.md    # 墨墨开放 API 契约整理
    └── astrbot-plugin-api-reference.md  # AstrBot 插件 API 整理
```

## 开发验证

插件在 **AstrBot 4.26.4** 环境做过真实加载与管道验证，覆盖：

* 模块导入、插件类实例化、`metadata.yaml` 必需字段
* 指令与别名注册（`memo` / `mm` / `墨墨`，含子指令与嵌套组）
* LLM 工具的 JSON Schema 生成，并断言 schema 里没有泄漏 `event` 参数
* `GreedyStr` 吞参行为（`['apple','banana','cherry'] -> 'apple banana cherry'`）
* 响应信封解包（`{data, errors, success}`）与无信封形态的兼容
* 401 / 404 / 429 错误分类、本地限流器、`voc_id` 缓存命中
* 各指令的渲染输出与内容来源标注
* **消息管道复刻**：复现 `WakingCheckStage → ProcessStage → RespondStage`，
  断言每条指令「恰好 1 条回复」且 `_has_send_oper=True`（即不会被 LLM 覆盖）

运行方式（需要能 import 到 `astrbot`）：

```bash
python tests/test_plugin_load.py
python tests/test_pipeline_dispatch.py
# 或指定 AstrBot 源码目录 / 使用其自带解释器
ASTRBOT_APP=/path/to/AstrBot python tests/test_plugin_load.py
"C:/Users/me/AppData/Local/AstrBot/backend/python/python.exe" tests/test_plugin_load.py
```

脚本会把 AstrBot 的数据目录隔离到插件目录下的 `.astrbot_test_root/`、`.astrbot_pipe_root/`，
不会污染真实安装。

## 数据来源

接口契约来自墨墨官方技能仓库 [maimemo/memo-skills](https://github.com/maimemo/memo-skills)
与官方 CLI [maimemo/memo-api-cli](https://github.com/maimemo/memo-api-cli)，
以及[墨墨开放平台文档](https://open.maimemo.com/)。
