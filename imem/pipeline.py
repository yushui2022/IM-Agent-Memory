"""把「看到了什么」安全地落到记忆里——采集链路的最后一步。

为什么值得单独一个模块
----------------------
这一步太容易漏。抽取结果到手之后，库里其实要发生**三**件事：

    1. 新消息            → insert
    2. 模型又看了一遍旧消息 → confirm（跨帧一致性，走 ExtractResult.repeats）
    3. 端侧去重命中的旧消息 → confirm（走 ingest 的 stats['existing']）

只做第 1 件的后果是静默的：消息一条不少，但全部停在 observed，
`confirm` 形同虚设，而你从外面看一切正常。第 2 件尤其容易漏——
因为 prompt 明确要求模型"不要重复输出已记录的消息"，于是它照着做，
说"没有新消息"，而**那句话本身就是可靠性证据**。把它丢掉，等于
白送了一次交叉验证。

所以别在各处手写这两行，走 commit()。
"""
from __future__ import annotations

from . import extract


def repeated_ids(result: extract.ExtractResult) -> list[int]:
    """这一轮模型"又看了一遍"的旧消息 id。跨帧一致性的直接证据。"""
    out: list[int] = []
    for m in result.repeats:
        mid = m.get("message_id")
        if mid is not None:
            out.append(int(mid))
    return out


def commit(store, conversation_key: str, result: extract.ExtractResult, *,
           captured_at=None, observation_id: int | None = None) -> dict:
    """一次观测的完整落地。返回 ingest 的统计，另外多两个字段：

        confirmed  —— 本轮被升级为 confirmed 的条数
        payload    —— 本轮真正送去入库的草稿数
    """
    payload = extract.to_ingest_payload(result, conversation_key=conversation_key)
    stats = store.ingest(conversation_key, payload,
                         observation_id=observation_id, captured_at=captured_at)

    evidence: list[int] = list(stats.get("existing") or [])
    evidence.extend(repeated_ids(result))
    stats["confirmed"] = store.confirm_many(evidence)
    stats["payload"] = len(payload)
    return stats
