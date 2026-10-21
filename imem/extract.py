"""把「屏幕上的画面」变成「可入库的消息」——多模态抽取层。

为什么单独一层
--------------
采集器要做的事其实是两件完全不同的事：

    看（视觉）  —— 帧差分、裁剪、抽帧、送模型      → vision.py + 本模块
    信（存储）  —— 去重、消歧、时间置信、溯源      → store.py

中间这条缝最容易糊在一起，于是变成「模型直接写数据库」。那样就没救了：
模型输出的错字、错人、编出来的时间会直接变成"记忆"，事后无从分辨。

所以本模块只生产**候选消息草稿**，规矩是：

    - 模型说的一切都可以错，字段都要过一遍校验才允许往下走
    - 拿不到时间就留空，**绝不替模型补一个看起来合理的值**
    - 发言人一律按界面原文保留，消歧交给 store.who()
    - 单帧看到 = 候选；重复看到 = 更可信（见 ExtractResult.repeats）

不绑定任何模型厂商
------------------
`CallableExtractor` 收一个 `fn(images: list[bytes], prompt: str) -> str`，
你用什么 SDK 都行。本模块只负责 prompt 和解析。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

PROMPT_VERSION = "im-extract-v1"

# 单条正文的长度上限。超过基本意味着模型把整屏吐成了一条。
MAX_TEXT_CHARS = 4000

MSG_TYPES = ("text", "image", "file", "system", "sticker", "quote", "redpacket", "other")

# 模型爱填的"空"占位符，一律当没有
_NULLISH = {"", "null", "none", "nil", "n/a", "na", "unknown", "未知", "无", "不确定", "-", "—"}


# ================================================================ 数据契约


@dataclass
class ExtractRequest:
    """一次抽取请求。

    frames 按时间升序，最后一张是**当前帧**。给多帧而不是单帧，是因为
    模型对"这几条是不是真的"需要跨帧佐证——同一行文字在两帧里都清晰，
    才更可能是真消息，而不是动画/加载态的残影。
    """

    app: str
    window_title: str
    frames: list[bytes]
    captured_at: str
    previous: list[dict] = field(default_factory=list)
    hint: str = ""


@dataclass
class ExtractResult:
    """抽取结果。messages 是新看到的，repeats 是又看了一遍的旧消息。"""

    messages: list[dict] = field(default_factory=list)
    repeats: list[dict] = field(default_factory=list)
    notes: str = ""
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    conversation: str | None = None
    raw: str = ""

    def __bool__(self) -> bool:
        return bool(self.messages or self.repeats)


class Extractor(Protocol):
    def extract(self, req: ExtractRequest) -> ExtractResult:  # pragma: no cover
        ...


# ================================================================ Prompt

_SYSTEM_RULES = """你是一个屏幕读取器。你看到的是一组按时间先后排列的 IM 客户端截图。
你的唯一任务是：把聊天区里**可见的消息**转录成结构化数据。

铁律：
1. 只转录你确实看得见的文字。看不清就写 null，不要猜、不要补全、不要润色。
2. 保留界面原文，包括错别字、表情符号、@ 前缀。不要翻译，不要改写。
3. 时间只在界面上明确显示时才填。界面没有时间就填 null。
   绝对不要根据"消息顺序"推算时间。
4. 发言人按界面显示的昵称原样填写。分不清是谁就填 "unknown"。
5. 不要把界面元素当成消息：搜索框、时间分隔线、系统提示、
   "查看更多消息"、输入框里的草稿，都不是消息。
6. 同一条消息在多张截图里出现，只输出一次。
"""

_JSON_SCHEMA = """只输出一个 JSON 对象，不要有任何额外文字、不要用 markdown 代码块：

{
  "conversation": "聊天区顶部标题栏显示的会话名，看不清就 null",
  "messages": [
    {
      "sender": "界面上显示的昵称",
      "text": "消息正文原文",
      "ts": "界面上显示的时间，没有就 null",
      "msg_type": "text|image|file|system|sticker|quote|redpacket|other",
      "confidence": 0.0
    }
  ],
  "notes": "一句话说明你看到了什么、哪里不确定"
}

confidence 是你对这条"昵称+正文"都读对的把握，0 到 1 的小数。
图片/表情/文件这类没有文字的，text 写一句客观描述，msg_type 标对，confidence 调低。

conversation 只从**标题栏**读，不要根据消息内容或联系人去猜。它是切会话之后
核对身份的唯一依据——读错比读不到更糟，所以看不清就老实填 null。
"""


def build_prompt(req: ExtractRequest) -> str:
    """拼 prompt。把上一轮已入库的消息一起给它，让它只输出新增部分。"""
    title = req.window_title or "（未知，请从标题栏读出来）"
    parts = [_SYSTEM_RULES, f"应用：{req.app}　　窗口标题：{title}"]

    if req.previous:
        known = "\n".join(
            f"- [{m.get('sender', '')}] {m.get('text', '')}"
            for m in req.previous[-40:]
        )
        parts.append(
            "以下消息**已经记录过**，不要重复输出：\n" + known
        )
    if req.hint:
        parts.append(f"额外提示：{req.hint}")
    parts.append(f"共 {len(req.frames)} 张截图，按时间升序。")
    parts.append(_JSON_SCHEMA)
    return "\n\n".join(parts)


# ================================================================ 解析


class ExtractError(ValueError):
    """模型输出完全没法用。调用者应当记一次失败、保持现状、下一轮再来。"""


def parse_model_output(raw: str, req: ExtractRequest | None = None) -> ExtractResult:
    """模型原始输出 → ExtractResult。

    这里假设模型**一定会**夹带 markdown 代码块、前后闲话、或者少个逗号。
    所以解析是宽松的，但校验是严格的：宽松只针对格式，不针对内容。
    """
    res = ExtractResult(raw=raw)
    if not raw or not raw.strip():
        raise ExtractError("模型返回空内容")

    payload = _rescue_json(raw)
    if payload is None:
        raise ExtractError(f"输出里找不到可解析的 JSON：{raw[:200]!r}")

    if isinstance(payload, list):
        items: Any = payload
        notes = ""
        conv: Any = None
    elif isinstance(payload, dict):
        items = payload.get("messages") or payload.get("msgs") or payload.get("data") or []
        notes = str(payload.get("notes") or "").strip()
        conv = payload.get("conversation") or payload.get("chat") or payload.get("title")
    else:
        raise ExtractError(f"JSON 顶层必须是对象或数组，实际是 {type(payload).__name__}")

    if not isinstance(items, list):
        raise ExtractError(f"messages 必须是数组，实际是 {type(items).__name__}")

    res.notes = notes
    res.conversation = _clean_str(conv) or None

    confs: list[float] = []
    for i, item in enumerate(items):
        msg, warn, conf = _coerce_message(item, i)
        if warn:
            res.warnings.append(warn)
        if msg is None:
            continue
        res.messages.append(msg)
        if conf is not None:
            confs.append(conf)

    if req is not None:
        res.messages, res.repeats = _split_repeats(res.messages, req.previous)

    res.confidence = round(sum(confs) / len(confs), 3) if confs else 0.0
    if not res.messages and not res.repeats:
        res.warnings.append("没有抽到任何消息（可能确实没有新消息，也可能读失败）")
    return res


def _rescue_json(raw: str) -> Any | None:
    """从一团文本里把 JSON 捞出来。先直解，再剥代码块，再从第一个括号试起。"""
    text = raw.strip()

    for candidate in (text, _strip_fence(text)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass

    body = _strip_fence(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = body.find(opener)
        end = body.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(body[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _strip_fence(text: str) -> str:
    text = re.sub(r"^\s*```[a-zA-Z]*\s*", "", text)
    return re.sub(r"\s*```\s*$", "", text).strip()


def _coerce_message(item: Any, idx: int) -> tuple[dict | None, str | None, float | None]:
    """单条草稿的校验与归一。返回 (草稿, 警告, 置信度)。"""
    if not isinstance(item, dict):
        return None, f"第 {idx} 条不是对象，已丢弃", None

    sender = _clean_str(item.get("sender"))
    text = _clean_str(item.get("text"))

    if not text:
        return None, f"第 {idx} 条没有正文，已丢弃", None
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        warn = f"第 {idx} 条正文超长，已截断到 {MAX_TEXT_CHARS} 字"
    else:
        warn = None

    msg_type = _clean_str(item.get("msg_type")).lower()
    if msg_type not in MSG_TYPES:
        msg_type = "text"

    conf = _coerce_conf(item.get("confidence"))
    draft = {
        "sender": sender or "unknown",
        "text": text,
        "ts": _clean_ts(item.get("ts")),
        "msg_type": msg_type,
        "confidence": conf,
    }
    return draft, warn, conf


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip().strip("\u200b")
    return "" if s.lower() in _NULLISH else s


def _clean_ts(v: Any) -> str | None:
    s = _clean_str(v)
    return s or None


def _coerce_conf(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(min(1.0, max(0.0, f)), 3)


def _split_repeats(messages: list[dict], previous: Sequence[dict]
                   ) -> tuple[list[dict], list[dict]]:
    """把模型又吐了一遍的旧消息挑出来，单独放 repeats。

    这不只是去重——`repeats` 是「跨帧一致性」的证据来源：
    同一条消息在两轮独立的抽取里都读到了，就值得从 observed 升到 confirmed。
    """
    index: dict[tuple[str, str], list[int]] = {}
    for p in previous or []:
        key = (_norm(p.get("sender")), _norm(p.get("text")))
        mid = p.get("message_id")
        if all(key) and mid is not None:
            index.setdefault(key, []).append(int(mid))

    fresh: list[dict] = []
    repeats: list[dict] = []
    seen_this_round: dict[tuple[str, str], int] = {}

    for m in messages:
        key = (_norm(m.get("sender")), _norm(m.get("text")))
        if key in index:
            ids = index[key]
            pos = seen_this_round.get(key, 0)
            if pos < len(ids):
                repeats.append({**m, "message_id": ids[pos]})
                seen_this_round[key] = pos + 1
                continue
        fresh.append(m)
    return fresh, repeats


def _norm(v: Any) -> str:
    """轻量归一：只压空白和全角空格，够用来比对"是不是同一句"。"""
    return re.sub(r"[\s\u3000]+", "", str(v or "")).lower()


# ================================================================ 适配器


class CallableExtractor:
    """把任意多模态调用包成 Extractor。

        extractor = CallableExtractor(lambda images, prompt: my_vlm(images, prompt))
        result = extractor.extract(ExtractRequest(...))

    `call` 收到的是**图像字节列表 + prompt**，返回模型原始文本。
    厂商无关是刻意的：这一层不该知道 Anthropic 还是 OpenAI 还是本地模型。
    """

    def __init__(self, call: Callable[[list[bytes], str], str], *,
                 media_type: str = "image/png"):
        self._call = call
        self.media_type = media_type

    def extract(self, req: ExtractRequest) -> ExtractResult:
        if not req.frames:
            raise ExtractError("没有可送的帧")
        prompt = build_prompt(req)
        raw = self._call(list(req.frames), prompt)
        return parse_model_output(raw, req)


class StaticExtractor:
    """测试与演示用：固定返回一段模型输出，不走网络。"""

    def __init__(self, output: str):
        self.output = output
        self.calls: list[ExtractRequest] = []

    def extract(self, req: ExtractRequest) -> ExtractResult:
        self.calls.append(req)
        return parse_model_output(self.output, req)


# ================================================================ 落地辅助


def to_ingest_payload(result: ExtractResult, *, conversation_key: str,
                      stable_ref: bool = True) -> list[dict]:
    """ExtractResult → store.ingest() 能直接吃的列表。

    低置信度的草稿照样入库——记忆层要留痕，`unresolved()` 才看得见它们。
    在这里静默丢掉才是最坏的选择：你会以为"那天没人说话"。

    关于 client_ref（重要）
    ----------------------
    store 的去重锚包含 ts_epoch。界面没显示时间时，它退化成"抓取时刻"，
    于是同一行字每抓一次就变一条新消息——轮询采集会迅速把库灌成垃圾。
    所以无 ts 的行必须自带一个**跨帧稳定**的 client_ref。

    代价要说清楚：同一个人的两句一模一样的「好的」会被折叠成一条。
    这是路线 A（读屏幕）的已知边界，不是 bug——拿不到时间就没有第二把尺子。
    需要保留重复条数时，调用者可以传 stable_ref=False，改用自己的策略。
    """
    payload: list[dict] = []
    for m in result.messages:
        row: dict = {
            "sender": m["sender"],
            "text": m["text"],
            "msg_type": m["msg_type"],
        }
        if m.get("ts"):
            row["ts"] = m["ts"]
        else:
            row["ts_conf"] = "inferred"
            if stable_ref:
                row["client_ref"] = stable_client_ref(
                    conversation_key, m["sender"], m["text"])
        payload.append(row)
    return payload


def stable_client_ref(conversation_key: str, sender: str, text: str) -> str:
    """跨帧稳定的去重键。同一会话 + 同一人 + 同一句正文 → 同一个值。"""
    raw = f"{conversation_key}|{_norm(sender)}|{_norm(text)}"
    return "scr:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def same_conversation(a: str | None, b: str | None) -> bool:
    """两个会话名是不是同一个。

    归一只压空白和大小写，不做同义词/别名消歧，而且**宁可判成不同**：
    判错的代价是把一个群的记录写进另一个群（不可逆），
    判严了只是多一次废读（可重来）。两边不对称，就按严的那边取。
    """
    na, nb = _norm(a), _norm(b)
    return bool(na) and na == nb
