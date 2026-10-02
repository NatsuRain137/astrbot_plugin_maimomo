"""把墨墨 API 的数据渲染成聊天可读的文本。

所有函数都是纯函数，便于单元测试。

版权合规：墨墨开放平台服务协议要求展示其词汇内容（释义 / 助记 / 例句）时
标注内容来源，因此这里统一使用 :data:`ATTRIBUTION` 常量在相关输出末尾署名。
"""

import re
from datetime import datetime
from typing import Any

#: 展示墨墨版权内容时必须附带的内容来源标注
ATTRIBUTION = "—— 内容来自墨墨背单词"

#: 单条文本的最大字符数，超出则截断，避免刷屏
MAX_FIELD = 200

_STUDY_LABELS = {
    "FAMILIAR": "认识",
    "VAGUE": "模糊",
    "FORGET": "忘记",
    "WELL_FAMILIAR": "熟知",
    "CANCEL_WELL_FAMILIAR": "取消熟知",
}

_RECORD_TAG_LABELS = {
    "STICKING": "顽固词",
    "WELL_FAMILIAR": "熟知",
    "STUDY_RECORD_TAG_UNSPECIFIED": "",
}


# ---------------------------------------------------------------------- #
# 通用工具
# ---------------------------------------------------------------------- #
def truncate(text: Any, limit: int = MAX_FIELD) -> str:
    """压缩空白并截断过长文本。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def progress_bar(finished: int, total: int, width: int = 12) -> str:
    """生成一个简单的文本进度条。"""
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, finished / total))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def format_minutes(milliseconds: Any) -> str:
    """把毫秒渲染成 ``x 分 y 秒``。"""
    try:
        total_seconds = int(milliseconds) // 1000
    except (TypeError, ValueError):
        return "未知"
    if total_seconds <= 0:
        return "0 分"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes == 0:
        return f"{seconds} 秒"
    return f"{minutes} 分 {seconds} 秒"


def _iso_to_display(value: Any) -> str:
    """把 ISO 8601 时间戳改写为 ``MM-DD HH:MM``。"""
    if not value:
        return ""
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return moment.strftime("%m-%d %H:%M")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ---------------------------------------------------------------------- #
# 学习数据
# ---------------------------------------------------------------------- #
def format_study_progress(
    progress: dict[str, Any],
    *,
    plan_total: int | None = None,
    due_today: int | None = None,
) -> str:
    """渲染 ``/memo status`` 的主体内容。"""
    finished = int(progress.get("finished") or 0)
    total = int(progress.get("total") or 0)
    study_time = progress.get("study_time", progress.get("study_time_ms"))

    lines = ["📚 今日墨墨学习进度", ""]
    lines.append(f"{progress_bar(finished, total)}  {finished}/{total}")
    lines.append(f"剩余待学：{max(0, total - finished)} 个")
    lines.append(f"今日学习时长：{format_minutes(study_time)}")
    if plan_total is not None:
        lines.append(f"学习规划总词数：{plan_total} 个")
    if due_today is not None:
        lines.append(f"今日及此前待复习：{due_today} 个")
    if total and finished >= total:
        lines.append("")
        lines.append("🎉 今日任务已完成，记得去墨墨签到！")
    lines.append("")
    lines.append("ℹ️ 学习数据为墨墨公测接口，需在 App 内开启自动同步后才会准确。")
    return "\n".join(lines)


def format_study_items(
    items: list[dict[str, Any]], *, title: str = "今日单词", limit: int = 30
) -> str:
    """渲染今日单词列表。"""
    if not items:
        return (
            f"📭 {title}：暂无数据。\n"
            "（若确实有学习记录，请先在墨墨 App 内打开一次以完成同步。）"
        )

    lines = [f"📝 {title}（共 {len(items)} 个，显示前 {min(limit, len(items))} 个）", ""]
    for index, item in enumerate(items[:limit], start=1):
        spelling = str(item.get("voc_spelling") or item.get("voc_id") or "?")
        flags: list[str] = []
        if item.get("is_new"):
            flags.append("新词")
        flags.append("已完成" if item.get("is_finished") else "未完成")
        response = _STUDY_LABELS.get(str(item.get("first_response") or ""), "")
        if response:
            flags.append(response)
        order = item.get("order")
        prefix = f"{order}. " if isinstance(order, int) else f"{index}. "
        lines.append(f"{prefix}{spelling}（{'/'.join(flags)}）")
    if len(items) > limit:
        lines.append(f"… 其余 {len(items) - limit} 个已省略")
    return "\n".join(lines)


def format_study_records(
    records: list[dict[str, Any]],
    count: int,
    *,
    title: str = "待复习单词",
    limit: int = 30,
) -> str:
    """渲染学习记录 / 复习计划。"""
    total = count or len(records)
    if not records:
        return f"📭 {title}：共 {total} 个，当前无明细可展示。"

    lines = [f"📖 {title}（共 {total} 个，显示前 {min(limit, len(records))} 个）", ""]
    for record in records[:limit]:
        spelling = str(record.get("voc_spelling") or record.get("voc_id") or "?")
        parts = [spelling]
        next_date = _iso_to_display(record.get("next_study_date"))
        if next_date:
            parts.append(f"下次 {next_date}")
        study_count = record.get("study_count")
        if study_count:
            parts.append(f"已学 {study_count} 次")
        tags = [
            _RECORD_TAG_LABELS.get(str(tag), str(tag))
            for tag in _as_list(record.get("tags"))
        ]
        tags = [t for t in tags if t]
        if tags:
            parts.append("/".join(tags))
        lines.append("· " + "，".join(parts))
    if len(records) > limit:
        lines.append(f"… 其余 {len(records) - limit} 个已省略")
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# 单词详情
# ---------------------------------------------------------------------- #
def format_word_bundle(bundle: dict[str, Any], *, limit: int = 5) -> str:
    """渲染单词的自定义释义 / 助记 / 例句（含来源标注）。"""
    voc = bundle.get("voc") or {}
    spelling = str(voc.get("spelling") or "?")
    voc_id = str(voc.get("id") or "")

    interpretations = _as_list(bundle.get("interpretations"))
    notes = _as_list(bundle.get("notes"))
    phrases = _as_list(bundle.get("phrases"))

    lines = [f"🔤 {spelling}"]
    if voc_id:
        lines.append(f"voc_id：{voc_id}")
    lines.append("")

    lines.append(f"【自定义释义】{len(interpretations)} 条")
    if interpretations:
        for item in interpretations[:limit]:
            tags = "/".join(str(t) for t in _as_list(item.get("tags")))
            suffix = f"（{tags}）" if tags else ""
            lines.append(f"· {truncate(item.get('interpretation'))}{suffix}")
    else:
        lines.append("· 暂无")
    lines.append("")

    lines.append(f"【助记】{len(notes)} 条")
    if notes:
        for item in notes[:limit]:
            note_type = str(item.get("note_type") or "")
            lines.append(
                f"· [{note_type}] {truncate(item.get('note'))}"
                if note_type
                else f"· {truncate(item.get('note'))}"
            )
    else:
        lines.append("· 暂无")
    lines.append("")

    lines.append(f"【例句】{len(phrases)} 条")
    if phrases:
        for item in phrases[:limit]:
            lines.append(f"· {truncate(item.get('phrase'))}")
            translation = truncate(item.get("interpretation"), 120)
            if translation:
                lines.append(f"  {translation}")
    else:
        lines.append("· 暂无")

    if interpretations or notes or phrases:
        lines.append("")
        lines.append(ATTRIBUTION)
    return "\n".join(lines)


def format_vocabulary(items: list[dict[str, Any]], *, title: str = "单词查询") -> str:
    """渲染 ``spelling -> voc_id`` 的批量查询结果。"""
    if not items:
        return f"📭 {title}：未找到匹配的单词。"
    lines = [f"🔍 {title}（{len(items)} 个）", ""]
    for item in items:
        lines.append(f"· {item.get('spelling', '?')} → {item.get('id', '?')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# 云词本
# ---------------------------------------------------------------------- #
def format_notepads(
    notepads: list[dict[str, Any]], *, title: str = "云词本列表", limit: int = 20
) -> str:
    """渲染云词本列表。"""
    if not notepads:
        return f"📭 {title}：还没有云词本。"

    lines = [f"📒 {title}（共 {len(notepads)} 个）", ""]
    for index, item in enumerate(notepads[:limit], start=1):
        name = truncate(item.get("title"), 40) or "(无标题)"
        parts = [f"{index}. {name}", f"id={item.get('id', '?')}"]
        if item.get("type") == "FAVORITE":
            parts.append("我的收藏")
        brief = truncate(item.get("brief"), 40)
        if brief:
            parts.append(brief)
        lines.append("· " + "，".join(parts))
    if len(notepads) > limit:
        lines.append(f"… 其余 {len(notepads) - limit} 个已省略")
    return "\n".join(lines)


def format_notepad_detail(notepad: dict[str, Any], *, limit: int = 60) -> str:
    """渲染云词本详情：标题信息 + 词条预览。"""
    if not notepad:
        return "⚠️ 没有找到该云词本。"

    lines = [f"📒 {truncate(notepad.get('title'), 60) or '(无标题)'}"]
    lines.append(f"id：{notepad.get('id', '?')}")
    brief = truncate(notepad.get("brief"), 80)
    if brief:
        lines.append(f"简介：{brief}")
    tags = [str(t) for t in _as_list(notepad.get("tags"))]
    if tags:
        lines.append(f"标签：{'、'.join(tags)}")
    lines.append(f"状态：{notepad.get('status', '?')}")

    parsed = _as_list(notepad.get("list"))
    words: list[str] = []
    chapters: list[str] = []
    for item in parsed:
        data = item.get("data") or {}
        if item.get("type") == "CHAPTER":
            chapters.append(str(data.get("chapter") or ""))
        else:
            words.append(str(data.get("word") or ""))

    if not parsed:
        # 兜底：接口未返回 list 时自行解析 content
        for line in str(notepad.get("content") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                chapters.append(line.lstrip("#").strip())
            else:
                words.append(line)

    lines.append("")
    lines.append(f"章节 {len(chapters)} 个，词条 {len(words)} 个")
    if chapters:
        lines.append("章节：" + "、".join(truncate(c, 20) for c in chapters[:10]))
    if words:
        lines.append(f"前 {min(limit, len(words))} 个词条：{'、'.join(words[:limit])}")
        if len(words) > limit:
            lines.append(f"… 其余 {len(words) - limit} 个已省略")
    return "\n".join(lines)
