"""IM-Agent-Memory：给 agent 用的 IM 存储与检索层。

设计前提（重要）：
  感知不在本层。它只负责「把已经看到的东西记准、记全、可溯源」。
  所以这里的一切都应假设输入**可能有错**：
    - sender 可能没消歧 → sender_entity_key 允许 NULL
    - ts 可能拿不到     → 退化为观测时刻并标 inferred
    - 同一条消息可能被写入多次 → anchor/client_ref 去重
    - 消息可能撤回       → state='vanished'

  想连"看屏幕"一起要的话，用这几个模块：
    vision.py   帧差分，判断这一帧值不值得送模型
    extract.py  多模态抽取，把画面变成候选消息草稿
    pipeline.py 草稿落地 + 跨帧 confirm + 会话身份核对
    scan.py     会话列表里的未读角标（切会话的触发器）
    plan.py     什么时候可以切、切哪几个

快速开始：
    from imem import Store
    with Store("imem.db") as s:
        conv = s.ensure_conversation("wechat", "产品群", kind="group", app_chat_key="123@chatroom")
        obs = s.add_observation("wechat", captured_at=None, window_title="产品群")
        s.ingest(conv, [
            {"sender": "老王-产品", "text": "方案明天再看", "ts": "2026-10-21T14:32",
             "client_ref": "cap42:0"},
        ], observation_id=obs, captured_at="2026-10-21T14:33")
        s.bind_alias("person:老王", "老王-产品", scope_key=conv)
        s.search("方案", scope=conv)
"""
from .store import Store
from . import extract, pipeline, plan, scan, text, vision

__all__ = ["Store", "text", "vision", "extract", "pipeline", "scan", "plan"]
__version__ = "1.0.6"
