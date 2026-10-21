"""IM-Agent-Memory 行为测试。

每个测试对应一个"真实会踩到的坑"，不是覆盖率凑数。
运行： python tests/test_imem.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from imem import Store, text as T


# ------------------------------------------------------------------ 夹具
def fresh() -> tuple[Store, str]:
    s = Store(":memory:")
    conv = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="111@chatroom")
    return s, conv


def msg(sender, body, ts="2026-10-21T14:32:00", **kw):
    d = {"sender": sender, "text": body, "ts": ts}
    d.update(kw)
    return d


# ------------------------------------------------------------------ 1 基本写入
def test_basic_write_and_read():
    s, conv = fresh()
    r = s.ingest(conv, [msg("老王", "方案明天再看"), msg("小李", "收到")])
    assert r["inserted"] == 2, r
    assert r["skipped"] == 0

    got = s.recent(conv, n=10)
    assert len(got) == 2
    assert got[0]["text"] == "收到"          # recent 是倒序
    assert got[1]["sender"] == "老王"
    assert s.stats()["messages"] == 2


# ------------------------------------------------- 2 三条相同的「好的」要存成三条
def test_identical_messages_in_one_batch():
    s, conv = fresh()
    batch = [msg("小王", "好的"), msg("小王", "好的"), msg("小王", "好的")]
    r = s.ingest(conv, batch)
    assert r["inserted"] == 3, r
    rows = s.recent(conv, n=10)
    assert len(rows) == 3
    assert len({x["id"] for x in rows}) == 3


# ------------------------------------------------- 3 重看同一窗口不重复
def test_rescroll_same_window_is_deduped():
    s, conv = fresh()
    batch = [msg("小王", "好的"), msg("小王", "好的"), msg("小王", "好的")]
    s.ingest(conv, batch)
    r2 = s.ingest(conv, batch)          # 滚动回去又看了一遍
    assert r2["inserted"] == 0, r2
    assert r2["skipped"] == 3
    assert s.stats()["messages"] == 3   # 没有膨胀


# ------------------------------------------------- 4 滑窗重叠：只追加新的尾部
def test_overlapping_window_appends_only_tail():
    s, conv = fresh()
    s.ingest(conv, [msg("A", "一"), msg("B", "二")])
    r = s.ingest(conv, [msg("B", "二"), msg("C", "三")])   # 与上一批重叠一条
    assert r["inserted"] == 1, r
    assert r["skipped"] == 1
    assert s.stats()["messages"] == 3


# ------------------------------------------------- 5 client_ref 幂等
def test_client_ref_idempotent():
    s, conv = fresh()
    m = msg("A", "同一条", client_ref="cap7:0")
    s.ingest(conv, [m])
    r = s.ingest(conv, [m])
    assert r["inserted"] == 0 and r["skipped"] == 1, r
    assert s.stats()["messages"] == 1


# ------------------------------------------------- 6 第四条相同消息用 new=True 救回来
def test_new_flag_for_genuine_repeat():
    s, conv = fresh()
    for _ in range(3):
        s.ingest(conv, [msg("小王", "好的")])     # 三次独立观测，被锚去重压成一条
    assert s.stats()["messages"] == 1
    r = s.ingest(conv, [msg("小王", "好的", new=True)])   # agent 确认这是新的
    assert r["inserted"] == 1
    assert s.stats()["messages"] == 2


# ------------------------------------------------- 7 中文 2 字检索
def test_chinese_two_char_search():
    s, conv = fresh()
    s.ingest(conv, [msg("A", "今天的会议改到下午三点"), msg("B", "方案明天再看")])
    hits = s.search("会议", scope=conv)
    assert len(hits) == 1 and hits[0]["text"].startswith("今天的会议"), hits
    assert s.search("改到", scope=conv)
    assert s.search("方案", scope=conv)


# ------------------------------------------------- 8 4 字子串 + 单字检索
def test_substring_and_single_char_search():
    s, conv = fresh()
    s.ingest(conv, [msg("A", "今天的会议改到下午三点")])
    assert s.search("下午三点", scope=conv)
    assert s.search("午三点", scope=conv)      # 子串
    assert s.search("天", scope=conv)          # 单字走 uni 列
    assert not s.search("改会", scope=conv)    # 跨词不应命中


# ------------------------------------------------- 9 scope 硬过滤（最重要的一条）
def test_scope_is_hard_filter():
    s = Store(":memory:")
    a = s.ensure_conversation("wechat", "A群", app_chat_key="a")
    b = s.ensure_conversation("wechat", "B群", app_chat_key="b")
    s.ingest(a, [msg("张三", "预算一百万")])
    s.ingest(b, [msg("张三", "预算五十万")])

    hits_a = s.search("预算", scope=a)
    assert len(hits_a) == 1 and hits_a[0]["scope"] == a, hits_a
    assert hits_a[0]["text"] == "预算一百万"
    assert len(s.search("预算", scope=b)) == 1
    assert len(s.search("预算")) == 2          # 不传 scope 才跨会话


# ------------------------------------------------- 10 昵称消歧 + 回填
def test_alias_binding_backfills():
    s, conv = fresh()
    s.ingest(conv, [msg("老王-产品", "第一句"), msg("老王-产品", "第二句")])

    assert s.stats()["unresolved_messages"] == 2
    assert s.who("老王-产品", scope_key=conv)["entity"] is None

    s.bind_alias("person:老王", "老王-产品", scope_key=conv)

    assert s.stats()["unresolved_messages"] == 0        # 历史被回填
    r = s.who("老王-产品", scope_key=conv)
    assert r["entity"] and r["entity"]["key"] == "person:老王", r
    assert r["confidence"] == 1.0


# ------------------------------------------------- 11 群昵称是 per-scope 的
def test_group_nickname_is_scope_scoped():
    s = Store(":memory:")
    a = s.ensure_conversation("wechat", "公司群", app_chat_key="a")
    b = s.ensure_conversation("wechat", "牌友群", app_chat_key="b")

    s.bind_alias("person:老王", "王经理", scope_key=a)    # 公司群里叫王经理
    s.bind_alias("person:老王", "老王", scope_key=b)      # 牌友群里叫老王

    assert s.who("王经理", scope_key=a)["entity"]["key"] == "person:老王"
    assert s.who("老王", scope_key=b)["entity"]["key"] == "person:老王"
    # 在 A 群里问「老王」不应该解析成功（那是 B 群的昵称）
    assert s.who("老王", scope_key=a)["entity"] is None


# ------------------------------------------------- 12 同一个人在写消息时自动消歧
def test_auto_resolve_on_ingest():
    s, conv = fresh()
    s.bind_alias("person:老王", "老王-产品", scope_key=conv)
    s.ingest(conv, [msg("老王-产品", "这是消歧后的消息")])
    row = s.recent(conv, n=1)[0]
    assert row["sender_entity"] == "person:老王", row


# ------------------------------------------------- 13 撤回
def test_vanished_hidden_by_default():
    s, conv = fresh()
    s.ingest(conv, [msg("A", "我说错话了")])
    mid = s.recent(conv, n=1)[0]["id"]
    assert s.mark_vanished(mid) == 1

    assert s.recent(conv, n=10) == []
    assert len(s.recent(conv, n=10, include_vanished=True)) == 1
    assert s.search("说错", scope=conv) == []
    assert len(s.search("说错", scope=conv, include_vanished=True)) == 1
    assert s.unresolved()["vanished"][0]["id"] == mid


# ------------------------------------------------- 14 时间推断
def test_missing_ts_degrades_to_inferred():
    s, conv = fresh()
    s.ingest(conv, [{"sender": "A", "text": "没给时间"}], captured_at="2026-10-20T09:00:00")
    row = s.recent(conv, n=1)[0]
    assert row["ts_conf"] == "inferred", row
    assert row["ts"].startswith("2026-10-20T09:00")
    assert s.unresolved()["inferred_ts_count"] == 1


# ------------------------------------------------- 15 时间过滤
def test_time_range_filter():
    s, conv = fresh()
    s.ingest(conv, [msg("A", "上周的决定", ts="2026-10-14T10:00:00")])
    s.ingest(conv, [msg("A", "今天的决定", ts="2026-10-21T10:00:00")])
    recent = s.search("决定", scope=conv, since="2026-10-20T00:00:00")
    assert len(recent) == 1 and recent[0]["text"] == "今天的决定", recent
    old = s.search("决定", scope=conv, until="2026-10-15T00:00:00")
    assert len(old) == 1 and old[0]["text"] == "上周的决定"


# ------------------------------------------------- 16 按发言人过滤
def test_speaker_filter():
    s, conv = fresh()
    s.ingest(conv, [msg("A", "我同意这个方案"), msg("B", "我不同意这个方案")])
    hits = s.search("方案", scope=conv, speaker="B")
    assert len(hits) == 1 and hits[0]["sender"] == "B", hits


# ------------------------------------------------- 17 digest 与 coverage
def test_digest_and_coverage():
    s, conv = fresh()
    s.ensure_conversation("wechat", "另一个群", app_chat_key="z")
    s.ingest(conv, [msg("A", "一"), msg("B", "二"), msg("A", "三")])

    d = s.digest(conv)
    assert d["message_count"] == 3
    assert d["messages"][0]["text"] == "一"          # 正序
    names = {p["sender_label"]: p["n"] for p in d["participants"]}
    assert names == {"A": 2, "B": 1}, names

    cov = s.coverage()
    assert len(cov) == 2
    # 从没采过的群，stale_days 为 None → 这是"覆盖盲区"，不能被当成"没人说话"
    blind = [c for c in cov if c["n"] == 0]
    assert len(blind) == 1 and blind[0]["stale_days"] is None


# ------------------------------------------------- 18 观测溯源
def test_provenance():
    s, conv = fresh()
    obs = s.add_observation("wechat", captured_at="2026-10-21T14:30:00",
                            window_title="产品群", image_ref="shot-001.png")
    s.ingest(conv, [msg("A", "带出处的消息")], observation_id=obs)
    row = s.recent(conv, n=1)[0]
    assert row["observation_id"] == obs
    rec = s.conn.execute("SELECT image_ref FROM observation WHERE id=?", (obs,)).fetchone()
    assert rec["image_ref"] == "shot-001.png"


# ------------------------------------------------- 19 未知会话要报错而不是静默丢
def test_unknown_conversation_raises():
    s = Store(":memory:")
    try:
        s.ingest("wechat:不存在", [msg("A", "x")])
    except KeyError as e:
        assert "ensure_conversation" in str(e)
    else:
        raise AssertionError("应当抛 KeyError")


# ------------------------------------------------- 20 空消息容错
def test_empty_message_tolerated():
    s, conv = fresh()
    r = s.ingest(conv, [{"sender": "", "text": ""}, {"sender": "A", "text": "ok"}])
    assert r["inserted"] == 1
    assert any("空消息" in w for w in r["warnings"])


# ------------------------------------------------- 21 可选向量检索
def test_optional_vector_search():
    s, conv = fresh()
    # 极简确定性"嵌入"：字符计数向量，只为验证通路
    def fake_embed(texts):
        out = []
        for t in texts:
            v = [0.0] * 8
            for ch in t:
                v[ord(ch) % 8] += 1.0
            out.append(v)
        return out

    s.set_embedder(fake_embed, model="fake")
    s.ingest(conv, [msg("A", "今天的会议改到下午三点"), msg("B", "完全不同的话题")])
    assert s.embed_pending() == 2
    assert s.stats()["embedded"] == 2
    hits = s.vector_search("会议改到下午", scope=conv, k=1)
    assert hits and "会议" in hits[0]["text"], hits


# ------------------------------------------------------------------ 运行器
def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
