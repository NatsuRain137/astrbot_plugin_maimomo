# 墨墨背单词（MaiMemo）开放 API 契约参考

本文件是编写本插件时整理并**逐条核对**过的接口契约，供后续维护参考。

来源：

* 墨墨官方技能仓库 [maimemo/memo-skills](https://github.com/maimemo/memo-skills)
  （`memo-api/SKILL.md` 与 `memo-api/references/` 下 6 个文件）
* 墨墨官方 CLI [maimemo/memo-api-cli](https://github.com/maimemo/memo-api-cli)
  与 npm 包 `@maimemo/memo-api`（v1.2.0）
* [墨墨开放平台文档](https://open.maimemo.com/)
* [墨墨百科 · 开放平台](https://memodocs.maimemo.com/docs/open) 与
  [开放 API 功能介绍](https://memodocs.maimemo.com/docs/PNvOw2E1AivPtlkRlQgce1W1n2c)、
  [服务协议](https://memodocs.maimemo.com/docs/open-terms)

## 1. 基础约定

| 项目 | 值 |
| --- | --- |
| Base URL | `https://open.maimemo.com/open/api/v1` |
| 鉴权头 | `Authorization: Bearer <access_token>` |
| 请求体 | `application/json` |
| 更新语义 | `POST /资源/{id}`，**不用** PUT/PATCH |
| 时间格式 | ISO 8601；学习日期筛选按北京时间（UTC+8） |
| 状态枚举 | `PUBLISHED` / `UNPUBLISHED` / `DELETED`（各域略有差异） |

### 响应信封

成功：

```json
{"data": {"...业务数据..."}, "errors": [], "success": true}
```

失败（已实测 401 / 404）：

```json
{"errors": [{"code": "common_unauthorized", "msg": "Authorization failed", "info": ""}], "success": false}
{"errors": [{"code": "common_not_found", "msg": "Resource or Api not found", "info": ""}], "success": false}
```

* 失败也可能是 **HTTP 200 + `success: false`**。
* 技能文档里 `Response:` 写的是**解包后**的 payload 形状。
* 客户端应同时兼容「有信封」与「无信封」两种形态（官方 CLI 就是这么做的）。

### 限流

按 **Access Token 对应的用户**计数（不是按开发者应用）：
**20 次 / 10 秒、40 次 / 60 秒、2000 次 / 5 小时**。

### Token 获取

* App：墨墨背单词 → 我的 → 开放 API
* 网页：<https://open.maimemo.com/open/api/v1/tokens/openapi>
  （会 302 跳转到 `accounts.maimemo.com` 登录；**网页来源的 Token 有效期 7 天**）
* 第三方应用：OIDC，issuer `https://accounts.maimemo.com/oidc`，
  scope 命名空间为 `open.memo.content` / `open.memo.study`

## 2. 接口清单

| # | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| 1 | GET | `/vocabulary?spelling=<word>` | 按拼写取单词 |
| 2 | POST | `/vocabulary/query` | 批量按 spelling / id 取单词（≤1000） |
| 3 | GET | `/notepads?limit=&offset=` | 云词本列表 |
| 4 | GET | `/notepads/{id}` | 云词本详情（含 content 与 list） |
| 5 | POST | `/notepads` | 新建云词本 |
| 6 | POST | `/notepads/{id}` | 更新云词本（全字段必填） |
| 7 | DELETE | `/notepads/{id}` | 删除云词本 |
| 8 | GET | `/interpretations?voc_id=` | 自定义释义列表 |
| 9 | POST | `/interpretations` | 新建释义 |
| 10 | POST | `/interpretations/{id}` | 更新释义 |
| 11 | DELETE | `/interpretations/{id}` | 删除释义 |
| 12 | GET | `/notes?voc_id=` | 助记列表 |
| 13 | POST | `/notes` | 新建助记 |
| 14 | POST | `/notes/{id}` | 更新助记 |
| 15 | DELETE | `/notes/{id}` | 删除助记 |
| 16 | GET | `/phrases?voc_id=` | 例句列表 |
| 17 | POST | `/phrases` | 新建例句 |
| 18 | POST | `/phrases/{id}` | 更新例句 |
| 19 | DELETE | `/phrases/{id}` | 删除例句 |
| 20 | POST | `/study/get_study_progress` | 今日学习进度（空 body） |
| 21 | POST | `/study/get_today_items` | 今日单词列表 |
| 22 | POST | `/study/query_study_records` | 学习记录 / 复习计划 |
| 23 | POST | `/study/add_words` | 加入学习规划 |
| 24 | POST | `/study/advance_study` | 提前复习（需等级 ≥ 10） |

## 3. 关键数据结构

### Vocabulary

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 唯一 `voc_id`，多数接口都要它 |
| `spelling` | string | 单词拼写 |

> 只返回 id 与拼写，**不含释义 / 词频 / 柯林斯 / 联想**。

### Notepad

`id`、`type`(`${FAVORITE}`/`NOTEPAD`)、`creator`(int)、`status`、`title`、`brief`、
`tags`(string[])、`created_time`、`updated_time`，详情额外有：

| 字段 | 说明 |
| --- | --- |
| `content` | 原始文本，一行一个单词，`#` 开头为章节标题 |
| `list` | 解析结果 `NotepadParsedItem[]`，每项 `{type: "CHAPTER"\|"WORD", data: {chapter, word}}` |

创建 body：

```json
{"notepad": {"title": "GRE高频词", "brief": "GRE常考词汇",
             "content": "# Chapter 1\napple\nbanana", "tags": ["GRE"], "status": "PUBLISHED"}}
```

> `POST /notepads/{id}` 要求**所有字段必填**，因此「追加单词」必须先 GET 再合并回写。

### Note（助记）

创建 body：`{"note": {"voc_id": "...", "note_type": "谐音", "note": "..."}}`
（创建时**不接受** `status`）。更新 body：`{"note": {"note_type": "...", "note": "..."}}`。

`note_type` 取值：联想·谐音·派生·词根·词源·固搭·语法·对比·近义·反义·扩展·串记·
口诀·合成·吐槽·其他·固定搭配·词根词缀·辨析·近反义词·图例

### Interpretation（释义）

创建 body：`{"interpretation": {"voc_id": "...", "interpretation": "n. 苹果", "tags": ["简明"], "status": "PUBLISHED"}}`

标签（最多 3 个）：简明·详细·英英·小学·初中·高中·四级·六级·专升本·专四·专八·
考研·考博·雅思·托福·托业·新概念·GRE·GMAT·BEC·MBA·SAT·ACT·法学·医学

### Phrase（例句）

创建 body：

```json
{"phrase": {"voc_id": "...", "phrase": "This is an apple.",
            "interpretation": "这是一个苹果。", "tags": ["词典"], "origin": "自编"}}
```

标签（最多 3 个）：释义标签 + 词典·短语。`highlight` 为半开区间 `[start, end)` 数组。

### Study

`StudyProgress`：`finished`(今日已完成)、`total`(今日目标)、`study_time`(**毫秒**)

`StudyTodayItem`：`voc_id`、`voc_spelling`、`order`、`first_response`(可选)、
`is_new`、`is_finished`

`StudyRecord`：`voc_id`、`voc_spelling`、`add_date`、`first_study_date`、
`last_study_date`、`next_study_date`、`last_response`、`study_count`、
`tags`（`STICKING` / `WELL_FAMILIAR`）

`StudyResponse` 枚举：`FAMILIAR` 认识 / `VAGUE` 模糊 / `FORGET` 忘记 /
`WELL_FAMILIAR` 熟知 / `CANCEL_WELL_FAMILIAR` 取消熟知

`/study/add_words` body：`{"words": [{"id": "voc_id"}], "advance": false}`，返回 `{"added_count": n}`

`/study/advance_study` body：`{"voc_ids": ["..."]}`，返回 `{"advanced_count": n}`

> `/study/*` 全部是 **POST + 公测（Beta）**：需在 App 内开启自动同步，
> 且当日打开过 App 才能准确计算。

## 4. 明确「没有开放」的能力

设计时不要踩空：

| 需求 | 结论 |
| --- | --- |
| 连续打卡天数、打卡日历、补签 | **无公开接口** |
| 历史每日学习量、按天学习时长 | **无公开接口** |
| 今日「已学 vs 已复习」分开计数 | 只能拿 `is_new` + `is_finished` + `order`，没有分列计数 |
| 词库 / 单词书列表（如「四级词汇」词表） | **无公开接口** |
| 云词本「未学习 / 已学习」分组、词条带释义 | **无** |
| 单词官方释义 / 词频 / 柯林斯 / 联想**只读**接口 | **未公开**；`/interpretations`、`/notes`、`/phrases` 都只操作当前用户自建内容 |

## 5. 合规要点（服务协议）

* 释义 / 助记 / 例句属版权内容，仅授予**展示**许可，且与 ClientId 绑定。
* 临时缓存**不得超过 168 小时**；禁止持久化汇编、修改演绎、二次分发、
  用于 AI 训练、爬虫批量获取、绕过频率限制、就内容本身收费。
* 展示时须标注「内容来自墨墨背单词」并保留作者署名。

本插件的做法：只持久化缓存 `spelling -> voc_id`（不含版权内容），
展示释义 / 助记 / 例句时统一附带来源标注。
