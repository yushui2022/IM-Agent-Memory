"""草稿推送测试。

全部走注入的 fake transport，不联网、不需要真实 webhook。

运行： python tests/test_notifier.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from imem import Store, text as T
from imem import drafter, notifier


# ------------------------------------------------------------------ 夹具
class FakeTransport:
    def __init__(self, status=200, resp='{"code":0,"msg":"ok"}'):
        self.status = status
        self.resp = resp
        self.calls: list[tuple[str, bytes, dict]] = []

    def __call__(self, url, body, headers, timeout=15):
        self.calls.append((url, body, headers))
        return self.status, self.resp


def make_pending(n: int = 2):
    """造 n 条待审草稿，返回 (store, pending)。"""
    s = Store(":memory:")
    s.set_self_label("wechat", "我")
    name = ["张三", "李四", "王五"]
    for i in range(n):
        conv = s.ensure_conversation("wechat", name[i % 3], kind="dm",
                                     app_chat_key=f"dm_{i}")
        s.ingest(conv, [{"sender": name[i % 3], "text": f"第{i}个问题，你看下",
                         "ts": (datetime.now(T.TZ) - timedelta(seconds=10)).isoformat(timespec="seconds")}])
    drafter.run_once(s, policy=drafter.Policy(min_gap_s=0))
    return s, s.outbox_pending()


# ------------------------------------------------------------------ 渲染
def test_render_empty():
    assert "没有待审" in notifier.render_drafts([])


def test_render_numbering_and_titles():
    s, pending = make_pending(2)
    titles = {"wechat:dm_0": "张三", "wechat:dm_1": "李四"}
    incoming = {}
    for r in pending:
        row = s.conn.execute("SELECT text FROM message WHERE id=?",
                             (r["source_message_id"],)).fetchone()
        incoming[r["source_message_id"]] = row["text"]
    txt = notifier.render_drafts(pending, titles, incoming=incoming)
    assert "待审草稿 2 条" in txt, txt
    assert "#1 [张三]" in txt, txt
    assert "#2 [李四]" in txt, txt
    assert "收到：" in txt, txt          # 手机上必须能看到「在回什么」
    assert "拟回：" in txt, txt
    assert "通过也不等于发送" in txt, txt


def test_render_truncates_long_text():
    rows = [{"id": 1, "conversation_key": "c", "text": "长" * 200}]
    txt = notifier.render_drafts(rows)
    assert "…" in txt
    assert "长" * 200 not in txt


def test_render_overflow_notice():
    rows = [{"id": i, "conversation_key": "c", "text": f"t{i}"} for i in range(1, 6)]
    txt = notifier.render_drafts(rows, max_items=2)
    assert "还有 3 条未显示" in txt, txt


# ------------------------------------------------------------------ 通道
def test_feishu_webhook_ok():
    fake = FakeTransport(200, '{"code": 0, "msg": "success"}')
    r = notifier.Notifier(fake).feishu_webhook("https://example/hook", "hi")
    assert r["ok"] is True, r
    url, body, headers = fake.calls[0]
    assert url == "https://example/hook"
    assert "hi" in body.decode("utf-8")
    assert headers["Content-Type"] == "application/json"


def test_feishu_webhook_detects_api_error():
    fake = FakeTransport(200, '{"code": 19001, "msg": "param invalid"}')
    r = notifier.Notifier(fake).feishu_webhook("https://example/hook", "hi")
    assert r["ok"] is False, r


def test_telegram_ok_and_fail():
    ok = notifier.Notifier(FakeTransport(200, '{"ok": true}')).telegram("tok", "42", "hi")
    assert ok["ok"] is True, ok
    bad = notifier.Notifier(FakeTransport(400, '{"ok": false}')).telegram("tok", "42", "hi")
    assert bad["ok"] is False, bad


def test_push_fanout_one_channel_fails():
    """单个通道失败不能拖垮其他通道。"""
    class Mixed:
        def __init__(self):
            self.n = 0

        def __call__(self, url, body, headers, timeout=15):
            self.n += 1
            if "feishu" in url:
                return 500, "boom"
            return 200, '{"ok": true}'

    res = notifier.push(notifier.Notifier(Mixed()), "hi",
                        feishu_url="https://open.feishu.cn/x",
                        tg_token="tok", tg_chat="1")
    by = {r["channel"]: r for r in res}
    assert by["feishu_webhook"]["ok"] is False
    assert by["telegram"]["ok"] is True, res


def test_push_without_config_reports_error():
    res = notifier.push(notifier.Notifier(FakeTransport()), "hi")
    assert len(res) == 1 and res[0]["ok"] is False
    assert "没有任何通道" in res[0]["error"]


def test_smtp_failure_is_caught():
    """连接失败要返回 ok=False，而不是抛异常把整个推送打挂。"""
    class Boom:
        def __init__(self, *a, **kw):
            raise OSError("no route to host")

    orig = notifier.smtplib.SMTP_SSL
    notifier.smtplib.SMTP_SSL = Boom
    try:
        r = notifier.Notifier(FakeTransport()).smtp(
            host="127.0.0.1", port=465, user="", password="", to="a@b.c", text="hi")
        assert r["ok"] is False and "OSError" in r["error"], r
    finally:
        notifier.smtplib.SMTP_SSL = orig


# ------------------------------------------------------------------ 红线
def test_push_does_not_touch_outbox_state():
    """推送只是读，绝不改变草稿状态，更不会发送。"""
    s, pending = make_pending(2)
    before = [(r["id"], r["state"]) for r in s.outbox_pending()]
    notifier.push(notifier.Notifier(FakeTransport(200, '{"code":0}')), "hi",
                  feishu_url="https://example/hook")
    after = [(r["id"], r["state"]) for r in s.outbox_pending()]
    assert before == after, (before, after)
    assert all(st == "draft" for _, st in after)
    assert s.conn.execute(
        "SELECT COUNT(*) FROM message WHERE sent_by='agent'").fetchone()[0] == 0


# ------------------------------------------------------------------ CLI
def test_cli_dry_run_and_approve():
    """端到端跑一次脚本：dry-run 只打印，--approve 只改状态不发消息。"""
    tmp = tempfile.mkdtemp(prefix="imem-cli-")
    db = os.path.join(tmp, "imem.db")

    s = Store(db)
    s.set_self_label("wechat", "我")
    conv = s.ensure_conversation("wechat", "张三", kind="dm", app_chat_key="dm_zs")
    s.ingest(conv, [{"sender": "张三", "text": "明天开会吗？",
                     "ts": (datetime.now(T.TZ) - timedelta(seconds=5)).isoformat(timespec="seconds")}])
    drafter.run_once(s)
    s.close()

    script = os.path.join(ROOT, "tools", "push_drafts.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    dry = subprocess.run([sys.executable, script, "--db", db],
                         capture_output=True, text=True, encoding="utf-8", env=env)
    assert dry.returncode == 0, dry.stderr
    assert "待审草稿 1 条" in dry.stdout, dry.stdout
    assert "dry-run" in dry.stdout, dry.stdout

    ap = subprocess.run([sys.executable, script, "--db", db, "--approve", "1"],
                        capture_output=True, text=True, encoding="utf-8", env=env)
    assert ap.returncode == 0, ap.stderr
    assert "approved" in ap.stdout, ap.stdout

    s2 = Store(db)
    states = [r["state"] for r in s2.conn.execute("SELECT state FROM outbox").fetchall()]
    assert states == ["approved"], states
    # 仍然没有发送
    assert s2.conn.execute(
        "SELECT COUNT(*) FROM message WHERE sent_by='agent'").fetchone()[0] == 0
    s2.close()


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
