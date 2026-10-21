"""文本处理：归一化、CJK 分词、时间解析。

为什么不用 FTS5 自带的分词器：
  - unicode61：中文整段当一个词，2 字查询查不到
  - trigram：只支持 ≥3 字符，中文两字词（"会议"/"改到"）全部失效（已实测）
所以自己切：CJK 出 bigram（配 unigram 列解决单字），拉丁/数字出整词。
查询时用 FTS5 的短语查询，靠 token 相邻位置实现"子串命中"。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone, timedelta

CJK_CLASS = "\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u30ff"
_TOKEN_RE = re.compile(f"[{CJK_CLASS}]+|[A-Za-z0-9_]+")
_CJK_RE = re.compile(f"[{CJK_CLASS}]")
_WS_RE = re.compile(r"\s+")

TZ = timezone(timedelta(hours=8))  # 与目标环境一致；可改成读 TZ 环境变量


# ------------------------------------------------------------------ 归一化
def normalize(text: str) -> str:
    """用于匹配与去重的归一化形式（NFKC + 折叠空白 + 小写）。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    s = _WS_RE.sub(" ", s).strip()
    return s.lower()


def analyze(text: str) -> tuple[list[str], list[str]]:
    """返回 (bigram_tokens, unigram_tokens)。"""
    bi: list[str] = []
    uni: list[str] = []
    for run in _TOKEN_RE.findall(unicodedata.normalize("NFKC", text or "")):
        if _CJK_RE.match(run):
            uni.extend(run)
            if len(run) > 1:
                bi.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            w = run.lower()
            bi.append(w)
            uni.append(w)
    return bi, uni


def build_fts(text: str) -> tuple[str, str]:
    """生成写入 message.fts_bi / fts_uni 的两个字符串。"""
    bi, uni = analyze(text)
    return " ".join(bi), " ".join(uni)


def fts_expr(query: str) -> str:
    """把用户查询变成 FTS5 MATCH 表达式。空字符串表示无法检索。"""
    bi, uni = analyze(query)
    if bi and (len(bi) > 1 or len(bi[0]) > 1):
        return 'fts_bi : "' + " ".join(bi) + '"'
    if uni:
        return 'fts_uni : "' + " ".join(uni) + '"'
    return ""


# ------------------------------------------------------------------ 时间
def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _to_epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return int(dt.timestamp())


def parse_ts(value, fallback_iso: str | None = None) -> tuple[str, int, str]:
    """解析时间。返回 (iso, epoch, confidence)。

    支持：datetime / ISO 字符串 / epoch 秒 / None。
    拿不到精确时间时退回观测时刻，并把置信度降为 inferred。
    """
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds"), _to_epoch(value), "exact"
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), TZ)
        return dt.isoformat(timespec="seconds"), int(float(value)), "exact"
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        for fmt_parser in (
            lambda s: datetime.fromisoformat(s),
            lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M"),
            lambda s: datetime.strptime(s, "%Y/%m/%d %H:%M"),
            lambda s: datetime.strptime(s, "%m-%d %H:%M").replace(
                year=datetime.now(TZ).year),
        ):
            try:
                dt = fmt_parser(raw)
                conf = "exact" if ":" in raw and len(raw) > 10 else "minute"
                return dt.isoformat(timespec="seconds"), _to_epoch(dt), conf
            except (ValueError, TypeError):
                continue
    fb = fallback_iso or now_iso()
    return fb, _to_epoch(datetime.fromisoformat(fb)), "inferred"


def minute_bucket(epoch: int) -> int:
    return epoch // 60


# ------------------------------------------------------------------ 去重锚
def anchor_hash(conversation_key: str, sender_label: str, text: str, ts_epoch: int) -> str:
    """去重锚：同一会话内，(sender, 正文, 分钟) 相同即视为同一批观测候选。

    注意：这是**不完美**的。同一分钟内三条完全相同、且相邻上下文也相同的消息，
    在没有任何 ID 的情况下信息论上无法区分——这时必须依赖 client_ref。
    """
    payload = "|".join([
        conversation_key,
        normalize(sender_label),
        normalize(text),
        str(minute_bucket(ts_epoch)),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def entity_key_for(label: str, kind: str = "person") -> str:
    return f"{kind}:{normalize(label)}"
