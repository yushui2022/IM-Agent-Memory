"""多模态抽取层的行为测试。

这些测试守的是另一条线：**模型说什么都不能直接进库。**
模型一定会夹带 markdown、编时间、漏字段、把整屏吐成一条。
所以这里测的全部是"模型输出不听话时，这一层还站得住吗"。

运行： python tests/test_extract.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, extract, pipeline


# ------------------------------------------------------------------ 夹具
def req(**kw) -> extract.ExtractRequest:
    d = {
        "app": "wechat",
        "window_title": "产品群",
        "frames": [b"\x89PNG-fake"],
        "captured_at": "2026-10-21T14:35:00",
    }
    d.update(kw)
    return extract.ExtractRequest(**d)


def payload(*msgs, notes="") -> str:
    return json.dumps({"messages": list(msgs), "notes": notes}, ensure_ascii=False)


def one(sender="老王", text="方案明天再看", ts=None, msg_type="text", confidence=0.9) -> dict:
    return {"sender": sender, "text": text, "ts": ts,
            "msg_type": msg_type, "confidence": confidence}


# ------------------------------------------------------------------ 解析
def test_parse_clean_json():
    r = extract.parse_model_output(payload(one()))
    assert len(r.messages) == 1
    assert r.messages[0]["sender"] == "老王"
    assert r.messages[0]["text"] == "方案明天再看"
    assert r.messages[0]["ts"] is None
    assert r.confidence == 0.9
    assert not r.warnings


def test_parse_markdown_fence():
    raw = "```json\n" + payload(one()) + "\n```"
    r = extract.parse_model_output(raw)
    assert len(r.messages) == 1


def test_parse_with_chatter_around():
    raw = "好的，我看到的消息如下：\n" + payload(one()) + "\n需要我再读一遍吗？"
    r = extract.parse_model_output(raw)
    assert len(r.messages) == 1


def test_parse_accepts_bare_list():
    r = extract.parse_model_output(json.dumps([one()], ensure_ascii=False))
    assert len(r.messages) == 1


def test_empty_output_raises():
    for bad in ("", "   "):
        try:
            extract.parse_model_output(bad)
        except extract.ExtractError:
            continue
        raise AssertionError(f"{bad!r} 应当抛 ExtractError")


def test_no_json_raises():
    try:
        extract.parse_model_output("模型挂了，返回了纯文本")
    except extract.ExtractError:
        return
    raise AssertionError("没有 JSON 时应当抛 ExtractError")


def test_top_level_scalar_raises():
    try:
        extract.parse_model_output('"就是一个字符串"')
    except extract.ExtractError:
        return
    raise AssertionError("顶层是标量时应当抛 ExtractError")


# ------------------------------------------------------------------ 字段校验
def test_drop_entry_without_text():
    r = extract.parse_model_output(payload(one(text=""), one(text="   ")))
    assert r.messages == []
    assert any("没有正文" in w for w in r.warnings), r.warnings


def test_drop_non_object_entry():
    r = extract.parse_model_output(payload("我是字符串", one()))
    assert len(r.messages) == 1
    assert any("不是对象" in w for w in r.warnings), r.warnings


def test_sender_missing_becomes_unknown():
    """宁可标 unknown，也不要猜是谁。"""
    r = extract.parse_model_output(payload(one(sender=None)))
    assert r.messages[0]["sender"] == "unknown"


def test_unknown_msg_type_falls_back_to_text():
    r = extract.parse_model_output(payload(one(msg_type="语音通话")))
    assert r.messages[0]["msg_type"] == "text"


def test_nullish_ts_becomes_none():
    for bad in ("", "null", "未知", "N/A", "-"):
        r = extract.parse_model_output(payload(one(ts=bad)))
        assert r.messages[0]["ts"] is None, bad


def test_real_ts_is_kept_verbatim():
    r = extract.parse_model_output(payload(one(ts="2026-10-21T14:32")))
    assert r.messages[0]["ts"] == "2026-10-21T14:32"


def test_confidence_is_clamped():
    r = extract.parse_model_output(payload(one(confidence=7), one(text="另一条", confidence=-3)))
    assert [m["confidence"] for m in r.messages] == [1.0, 0.0]


def test_confidence_averaged():
    r = extract.parse_model_output(payload(one(confidence=0.8), one(text="第二条", confidence=0.4)))
    assert r.confidence == 0.6, r.confidence


def test_long_text_is_truncated():
    r = extract.parse_model_output(payload(one(text="啊" * 9000)))
    assert len(r.messages[0]["text"]) == extract.MAX_TEXT_CHARS
    assert any("截断" in w for w in r.warnings)


def test_notes_survive():
    r = extract.parse_model_output(payload(one(), notes="右下角有个图片，读不出内容"))
    assert "图片" in r.notes


# ------------------------------------------------------------------ Prompt
def test_prompt_forbids_guessing_time():
    p = extract.build_prompt(req())
    assert "绝对不要根据" in p
    assert "看不清就写 null" in p


def test_prompt_lists_previous_messages():
    p = extract.build_prompt(req(previous=[
        {"sender": "老王", "text": "方案明天再看", "message_id": 1},
    ]))
    assert "已经记录过" in p
    assert "方案明天再看" in p


def test_prompt_carries_app_and_title():
    p = extract.build_prompt(req(app="feishu", window_title="需求评审"))
    assert "feishu" in p and "需求评审" in p


# ------------------------------------------------------------------ 跨帧重复
def test_repeats_carry_message_id():
    previous = [{"message_id": 7, "sender": "老王", "text": "方案明天再看"}]
    r = extract.parse_model_output(payload(one()), req(previous=previous))
    assert r.messages == []
    assert len(r.repeats) == 1
    assert r.repeats[0]["message_id"] == 7


def test_repeats_ignore_whitespace_differences():
    """模型多敲一个空格不该让同一条消息变成两条。"""
    previous = [{"message_id": 9, "sender": "老王", "text": "方案 明天再看"}]
    r = extract.parse_model_output(payload(one(text="方案明天再看")), req(previous=previous))
    assert r.repeats and r.repeats[0]["message_id"] == 9


def test_new_message_is_not_swallowed_as_repeat():
    previous = [{"message_id": 7, "sender": "老王", "text": "方案明天再看"}]
    r = extract.parse_model_output(payload(one(text="那就周三")), req(previous=previous))
    assert len(r.messages) == 1
    assert r.repeats == []


def test_two_identical_outputs_match_two_previous_ids():
    previous = [
        {"message_id": 1, "sender": "老王", "text": "好的"},
        {"message_id": 2, "sender": "老王", "text": "好的"},
    ]
    r = extract.parse_model_output(payload(one(text="好的"), one(text="好的")), req(previous=previous))
    assert sorted(x["message_id"] for x in r.repeats) == [1, 2]
    assert r.messages == []


# ------------------------------------------------------------------ 适配器
def test_callable_extractor_passes_frames_and_prompt():
    seen = {}

    def call(images, prompt):
        seen["images"] = images
        seen["prompt"] = prompt
        return payload(one())

    ex = extract.CallableExtractor(call)
    r = ex.extract(req(frames=[b"a", b"b"]))
    assert seen["images"] == [b"a", b"b"]
    assert "屏幕读取器" in seen["prompt"]
    assert len(r.messages) == 1


def test_callable_extractor_rejects_empty_frames():
    ex = extract.CallableExtractor(lambda images, prompt: payload(one()))
    try:
        ex.extract(req(frames=[]))
    except extract.ExtractError:
        return
    raise AssertionError("没有帧时应当抛 ExtractError")


def test_static_extractor_records_calls():
    ex = extract.StaticExtractor(payload(one()))
    ex.extract(req())
    ex.extract(req(window_title="另一个群"))
    assert len(ex.calls) == 2
    assert ex.calls[1].window_title == "另一个群"


# ------------------------------------------------------------------ 落地
def test_no_ts_rows_get_stable_client_ref():
    r = extract.parse_model_output(payload(one()))
    rows = extract.to_ingest_payload(r, conversation_key="wechat:g1")
    assert rows[0]["ts_conf"] == "inferred"
    assert rows[0]["client_ref"].startswith("scr:")


def test_stable_client_ref_is_deterministic():
    a = extract.stable_client_ref("wechat:g1", "老王", "方案明天再看")
    b = extract.stable_client_ref("wechat:g1", "老王", "方案 明天再看")
    c = extract.stable_client_ref("wechat:g2", "老王", "方案明天再看")
    assert a == b, "空白差异不该产生新 ref"
    assert a != c, "不同会话必须分开"


def test_rows_with_ts_do_not_need_client_ref():
    """有时间戳的消息，锚点本身就稳定，不需要额外去重键。"""
    r = extract.parse_model_output(payload(one(ts="2026-10-21T14:32")))
    rows = extract.to_ingest_payload(r, conversation_key="wechat:g1")
    assert "client_ref" not in rows[0]
    assert rows[0]["ts"] == "2026-10-21T14:32"


# ------------------------------------------------------------------ 端到端
MODEL_OUTPUT = payload(one(text="方案明天再看，周三之前给答复"))


def test_end_to_end_extract_ingest_search():
    """抽取 → 入库 → 检索。这是整条链路唯一真正要成立的部分。"""
    s = Store(":memory:")
    conv = s.ensure_conversation("wechat", "产品群", kind="group")

    r = extract.StaticExtractor(MODEL_OUTPUT).extract(req())
    stats = s.ingest(conv, extract.to_ingest_payload(r, conversation_key=conv),
                     captured_at="2026-10-21T14:35:00")
    assert stats["inserted"] == 1, stats

    hits = s.search("周三", scope=conv)
    assert len(hits) == 1
    assert hits[0]["text"] == "方案明天再看，周三之前给答复"
    assert hits[0]["state"] == "observed"
    assert hits[0]["ts_conf"] == "inferred"


def test_reobservation_dedupes_and_confirms():
    """同一行字被抓了两轮：不能变成两条，而且该升到 confirmed。"""
    s = Store(":memory:")
    conv = s.ensure_conversation("wechat", "产品群", kind="group")
    ex = extract.StaticExtractor(MODEL_OUTPUT)

    first = s.ingest(conv, extract.to_ingest_payload(ex.extract(req()), conversation_key=conv),
                     captured_at="2026-10-21T14:35:00")
    mid = first["messages"][0]

    # 第二轮：换了个抓取时刻，但内容一样 —— 稳定 client_ref 必须挡住
    second = s.ingest(conv, extract.to_ingest_payload(ex.extract(req()), conversation_key=conv),
                      captured_at="2026-10-21T14:41:00")
    assert second["inserted"] == 0, second
    assert second["existing"] == [mid], second

    assert s.confirm_many(second["existing"]) == 1
    assert s.recent(conv, n=5)[0]["state"] == "confirmed"
    assert s.stats()["messages"] == 1, "重复观测不能增加消息条数"


def test_second_wave_adds_only_the_new_message():
    """滚动窗口：第二轮多了一条新的，只该插入那一条。"""
    s = Store(":memory:")
    conv = s.ensure_conversation("wechat", "产品群", kind="group")

    wave1 = payload(one(text="方案明天再看"))
    wave2 = payload(one(text="方案明天再看"), one(sender="小李", text="收到"))

    a = s.ingest(conv, extract.to_ingest_payload(
        extract.StaticExtractor(wave1).extract(req()), conversation_key=conv))
    assert a["inserted"] == 1

    b = s.ingest(conv, extract.to_ingest_payload(
        extract.StaticExtractor(wave2).extract(req()), conversation_key=conv))
    assert b["inserted"] == 1, b
    assert len(b["existing"]) == 1, b
    assert s.stats()["messages"] == 2


# ------------------------------------------------------------------ pipeline
def test_commit_promotes_duplicate_observation():
    """端侧去重命中的旧消息，必须在这一步升 confirmed。"""
    s = Store(":memory:")
    conv = s.ensure_conversation("wechat", "产品群", kind="group")
    ex = extract.StaticExtractor(MODEL_OUTPUT)

    a = pipeline.commit(s, conv, ex.extract(req()), captured_at="2026-10-21T14:35:00")
    assert a["inserted"] == 1 and a["confirmed"] == 0
    assert a["payload"] == 1

    b = pipeline.commit(s, conv, ex.extract(req()), captured_at="2026-10-21T14:41:00")
    assert b["inserted"] == 0, b
    assert b["confirmed"] == 1, b
    assert s.recent(conv, n=5)[0]["state"] == "confirmed"


def test_commit_promotes_repeats_from_model():
    """模型按提示"不重复输出"，那条 repeats 就是交叉验证证据，不能白扔。

    这是 demo 跑出来的真 bug：只认 stats['existing'] 的话，
    模型越听话，confirm 越永远为 0。
    """
    s = Store(":memory:")
    conv = s.ensure_conversation("wechat", "产品群", kind="group")
    ex = extract.StaticExtractor(MODEL_OUTPUT)

    a = pipeline.commit(s, conv, ex.extract(req()), captured_at="2026-10-21T14:35:00")
    mid = a["messages"][0]

    prev = [{"message_id": mid, "sender": "老王",
             "text": "方案明天再看，周三之前给答复"}]
    r = ex.extract(req(previous=prev))
    assert r.messages == [] and len(r.repeats) == 1, r

    b = pipeline.commit(s, conv, r, captured_at="2026-10-21T14:41:00")
    assert b["inserted"] == 0
    assert b["confirmed"] == 1
    assert s.recent(conv, n=5)[0]["state"] == "confirmed"


def test_repeated_ids_ignores_missing_ids():
    res = extract.ExtractResult(repeats=[{"sender": "老王", "text": "方案明天再看"}])
    assert pipeline.repeated_ids(res) == []


def test_repeated_ids_collects_all():
    res = extract.ExtractResult(repeats=[
        {"message_id": 3, "sender": "a", "text": "x"},
        {"message_id": 4, "sender": "b", "text": "y"},
    ])
    assert pipeline.repeated_ids(res) == [3, 4]
