"""草稿推送：把待审草稿渲染成一条人能看懂的消息，发到你的手机上。

**不做手机端。** 手机端是飞书 / Telegram / 邮件客户端本身——
我们只负责给机器人发一条消息，推送走它们自家的通道。

零第三方依赖：HTTP 用 urllib，邮件用 smtplib。transport 可注入，测试不联网。

支持三种通道：
  feishu_webhook   飞书群自定义机器人（最简单：建群 → 加机器人 → 拿 URL）
  telegram         BotFather 拿 token
  smtp             邮箱
"""
from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Callable

# url, body(bytes), headers -> (status, text)
Transport = Callable[[str, bytes, dict, float], tuple[int, str]]


def default_transport(url: str, body: bytes, headers: dict, timeout: float = 15) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ 渲染
def render_drafts(rows: list[dict], titles: dict[str, str] | None = None,
                  max_items: int = 8, approve_hint: str = "",
                  incoming: dict[int, str] | None = None) -> str:
    """把 outbox 待审项渲染成一条消息。纯文本，手机上好读。

    `incoming` 是 {source_message_id: 收到的原话}——没有它，你在手机上
    只看到"我打算回什么"，却不知道在回什么，没法做判断。
    """
    if not rows:
        return "📭 没有待审草稿。"

    titles = titles or {}
    incoming = incoming or {}
    shown = rows[:max_items]
    lines = [f"📝 待审草稿 {len(rows)} 条", ""]
    for i, r in enumerate(shown, 1):
        conv = titles.get(r["conversation_key"], r["conversation_key"])
        lines.append(f"#{i} [{conv}]")
        src = incoming.get(r.get("source_message_id") or -1, "")
        if src:
            src = src.replace("\n", " ")
            lines.append("   收到：" + (src[:60] + "…" if len(src) > 60 else src))
        text = (r["text"] or "").replace("\n", " ")
        if len(text) > 80:
            text = text[:80] + "…"
        lines.append(f"   拟回：{text}")
    if len(rows) > len(shown):
        lines.append(f"… 还有 {len(rows) - len(shown)} 条未显示")
    lines.append("")
    lines.append(approve_hint or
                 "（本机执行 `python tools/push_drafts.py --approve 1` 确认通过）")
    lines.append("注：通过也不等于发送，发送仍需单独执行。")
    return "\n".join(lines)


# ------------------------------------------------------------------ 发送
class Notifier:
    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or default_transport

    # ---- 飞书群自定义机器人：建群 → 设置 → 群机器人 → 添加 → 复制 Webhook 地址
    def feishu_webhook(self, url: str, text: str) -> dict:
        body = json.dumps({"msg_type": "text", "content": {"text": text}},
                          ensure_ascii=False).encode("utf-8")
        status, resp = self.transport(url, body, {"Content-Type": "application/json"}, 15)
        ok = status == 200 and '"code":0' in resp.replace(" ", "")
        return {"channel": "feishu_webhook", "ok": ok, "status": status, "resp": resp[:300]}

    # ---- Telegram：@BotFather 建 bot，给 bot 发条消息后用 getUpdates 查 chat_id
    def telegram(self, token: str, chat_id: str, text: str) -> dict:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        status, resp = self.transport(
            url, body, {"Content-Type": "application/x-www-form-urlencoded"}, 15)
        ok = status == 200 and '"ok":true' in resp.replace(" ", "")
        return {"channel": "telegram", "ok": ok, "status": status, "resp": resp[:300]}

    # ---- SMTP：把消息当邮件正文
    def smtp(self, host: str, port: int, user: str, password: str, to: str,
             text: str, use_ssl: bool = True) -> dict:
        msg = EmailMessage()
        msg["Subject"] = "IM-Agent-Memory 待审草稿"
        msg["From"] = user or "imem@localhost"
        msg["To"] = to
        msg.set_content(text)
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls()
            if user:
                server.login(user, password)
            server.send_message(msg)
            server.quit()
            return {"channel": "smtp", "ok": True}
        except (OSError, smtplib.SMTPException) as exc:
            return {"channel": "smtp", "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def render_escalations(rows: list[dict], titles: dict[str, str] | None = None,
                       incoming: dict[int, str] | None = None,
                       max_items: int = 8) -> str:
    """升级通知：agent 拿不准，需要人拍板。**这才是唯一该惊动你的消息。**"""
    if not rows:
        return ""
    titles = titles or {}
    incoming = incoming or {}
    shown = rows[:max_items]
    lines = [f"🚨 需要你介入 {len(rows)} 条", ""]
    for i, r in enumerate(shown, 1):
        conv = titles.get(r["conversation_key"], r["conversation_key"])
        lines.append(f"#{i} [{conv}] {r.get('escalate_reason') or '需要人工判断'}")
        src = incoming.get(r.get("source_message_id") or -1, "")
        if src:
            src = src.replace("\n", " ")
            lines.append("   收到：" + (src[:60] + "…" if len(src) > 60 else src))
    if len(rows) > len(shown):
        lines.append(f"… 还有 {len(rows) - len(shown)} 条")
    return "\n".join(lines)


def render_actions(rows: list[dict], titles: dict[str, str] | None = None,
                   incoming: dict[int, str] | None = None,
                   max_items: int = 10) -> str:
    """事后通报：已经替你处理了。**只告知，不需要你做什么。**"""
    if not rows:
        return ""
    titles = titles or {}
    incoming = incoming or {}
    shown = rows[:max_items]
    lines = [f"✅ 刚替你回了 {len(rows)} 条", ""]
    for r in shown:
        conv = titles.get(r["conversation_key"], r["conversation_key"])
        src = incoming.get(r.get("source_message_id") or -1, "")
        src = src.replace("\n", " ")
        if len(src) > 40:
            src = src[:40] + "…"
        body = (r.get("text") or "").replace("\n", " ")
        if len(body) > 40:
            body = body[:40] + "…"
        lines.append(f"· [{conv}] {src} → {body}" if src else f"· [{conv}] {body}")
    if len(rows) > len(shown):
        lines.append(f"… 还有 {len(rows) - len(shown)} 条")
    return "\n".join(lines)


def render_digest(escalations: list[dict], actions: list[dict],
                  titles: dict[str, str] | None = None,
                  incoming: dict[int, str] | None = None) -> str:
    """一条消息同时给出「需要你介入」和「已替你处理」。都为空则返回空串。"""
    parts = [p for p in (render_escalations(escalations, titles, incoming),
                         render_actions(actions, titles, incoming)) if p]
    if not parts:
        return ""
    return "\n\n──────────\n\n".join(parts)


def push(notifier: Notifier, text: str, feishu_url: str = "", tg_token: str = "",
         tg_chat: str = "", smtp_cfg: dict | None = None) -> list[dict]:
    """按配置往所有可用通道发一遍。单个通道失败不影响其他。"""
    results: list[dict] = []
    if feishu_url:
        results.append(notifier.feishu_webhook(feishu_url, text))
    if tg_token and tg_chat:
        results.append(notifier.telegram(tg_token, tg_chat, text))
    if smtp_cfg:
        results.append(notifier.smtp(text=text, **smtp_cfg))
    if not results:
        results.append({"channel": None, "ok": False,
                        "error": "没有任何通道配置，消息只在本地打印"})
    return results
