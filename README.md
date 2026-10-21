# IM-Agent-Memory

给 agent 用的 **IM 存储 + 检索**层。

> 📋 **要解决什么问题、做到什么程度算成功、接下来做什么** → [ROADMAP.md](ROADMAP.md)
> 本文件只讲**接口与用法**。

```
agent（自己做感知：截图 / UI 树 / OCR）
    │  写入：(昵称, 正文, 时间戳) + 会话 + 观测出处
    ▼
IM-Agent-Memory   ← 本仓库：清洗、去重、消歧、索引、检索
    │  读出：原文片段 + 出处 + 置信度 + 覆盖盲区
    ▼
agent（自己决定要不要信、要不要复核）
```

## 1. 边界：它不管什么

| 不负责 | 原因 |
|---|---|
| 看屏幕 / OCR / UI 树解析 | 感知是另一个方向，由 agent 自己完成 |
| 语义抽取（"谁承诺了什么"） | 第二阶段；v1 只保证"能准确检索到原始消息 + 能说出出处" |
| 社交画像、跨 app 自动合并身份 | 同上，且需要人工确认回路 |
| 摘要生成 | `digest()` 只返回**原始素材**，摘要由 agent 自己调 LLM |

**核心假设：输入可能是错的。** 所以：`sender_entity_key` 允许 NULL，时间戳允许 `inferred`，消息允许 `vanished`，检索结果带出处和置信度。

## 2. 快速开始

```python
from imem import Store

with Store("imem.db") as s:
    conv = s.ensure_conversation("wechat", "产品群", kind="group",
                                 app_chat_key="123@chatroom")
    obs  = s.add_observation("wechat", captured_at="2026-10-21T14:33",
                             window_title="产品群", image_ref="shot-0042.png")

    s.ingest(conv, [
        {"sender": "老王-产品", "text": "方案明天再看", "ts": "2026-10-21T14:32",
         "client_ref": "shot-0042:0"},
        {"sender": "小李", "text": "收到", "ts": "2026-10-21T14:32",
         "client_ref": "shot-0042:1"},
    ], observation_id=obs, captured_at="2026-10-21T14:33")

    s.bind_alias("person:老王", "老王-产品", scope_key=conv)   # 消歧是渐进的人工回路

    for hit in s.search("方案", scope=conv):
        print(hit["ts"], hit["sender"], hit["text"], hit["observation_id"])
```

跑测试：

```bash
python tests/test_imem.py       # 21 个行为测试
```

零第三方依赖，只用标准库（sqlite3 / hashlib / re / unicodedata）。

## 3. 写入契约

### 会话

```python
conv_key = s.ensure_conversation(app, title, kind="group",
                                 app_chat_key=None, member_count=None)
```

`conv_key` 是 **app-agnostic** 的（`wechat:123@chatroom` / `feishu:oc_xxx`）——加新 IM 不用改表。

### 消息

| 字段 | 必填 | 说明 |
|---|---|---|
| `sender` | ✅ | **界面上的昵称原文**。不要自己清洗，原样传 |
| `text` | ✅ | 正文。图片/表情传真占位（如 `[图片]`） |
| `ts` | 建议 | ISO / epoch。**不给则退化为观测时刻，并标记 `ts_conf='inferred'`** |
| `msg_type` | — | `text` / `image` / `file` / `system` / `sticker` / `quote` / `redpacket` / `other` |
| `client_ref` | — | **幂等键，强烈建议给**。形如 `shot-0042:7`（观测 id + 窗口内序号） |
| `reply_to_text` | — | 被引用内容 |
| `new` | — | `True` = agent 确认这是新消息，绕过去重（见 §5） |

### 观测（用于溯源）

```python
obs_id = s.add_observation(app, captured_at=..., window_title=..., image_ref=...)
```

每条消息都能追到「来自哪次观测、哪张图」，这是"可复核"的前提。

## 4. 检索契约

| 方法 | 用途 | 典型耗时（2 万条实测） |
|---|---|---|
| `search(q, scope=..., since=..., until=..., speaker=..., k=10)` | 关键词检索。**`scope` 是硬过滤** | 1.6 ms |
| `recent(scope, n=30)` | 最近消息（占 90% 调用量） | <0.1 ms |
| `who(name, scope_key=...)` | 昵称 → 实体，返回**置信度**而不是抛异常 | — |
| `digest(scope, since=...)` | 拉一段原始素材（不摘要） | — |
| `unresolved()` | **「我不确定」的一等出口** | — |
| `coverage()` | 覆盖盲区：多久没看过哪个群 | 3.2 ms |
| `stats()` | 计数 | — |

排序 = `0.75 × 名次分 + 0.25 × 新近分`（30 天半衰）。可解释、可调。

### 两个设计上刻意的选择

**① `scope` 是硬过滤，不做跨会话漂移。**
把 A 群的话拿到 B 群说，是比"检索不到"严重得多的错误。

**② `unresolved()` 是核心接口，不是附属功能。**
它返回未消歧的昵称、未确认的实体、撤回的消息、时间推断过的条数。agent 靠它知道"我可能读错了"，从而决定复核或追问。**没有这个，agent 会把读错的昵称当真、把没看到当成没发生。**

## 5. 去重语义（最容易踩坑的地方）

无消息 ID 的情况下，判重只能靠合成锚：

```
anchor = sha1(会话 + 昵称 + 正文 + 分钟)
唯一键 = (会话, anchor, 同批内序号 ordinal)
```

行为：

| 场景 | 结果 |
|---|---|
| 一个窗口里三条相同的「好的」 | 存成 **3 条**（同批 ordinal 递增）✅ |
| 滚动回去重看同一个窗口 | **0 新增**（锚+序号命中）✅ |
| 滑窗重叠（新旧窗口共享尾部） | 只追加**新的尾部** ✅ |
| 给了相同 `client_ref` | 跳过（幂等）✅ |
| 后来单独出现第 4 条相同的「好的」 | ⚠️ 会被判为重复而丢失 |

最后一行是**信息论上的固有限制**：一条消息看两次，和两条完全相同的消息，在没有任何 ID 时无法区分。两条出路：

1. **让 agent 传 `client_ref`**（它能区分，因为知道观测 id 和窗口内位置）——推荐
2. 传 `{"new": True}` 强制插入——agent 确认"这确实是新的一条"时用

系统不会静默出错：整批被去重时会返回 `warnings` 提示"可能是重复观测，或该用 new=True"。

## 6. 中文检索：为什么不用 FTS5 自带分词器

实测（SQLite 3.39）：

| 分词器 | `"会议"`（2 字） | 说明 |
|---|---|---|
| `unicode61` | ❌ 查不到 | 中文整段被当**一个词** |
| `trigram` | ❌ 查不到 | 只支持 ≥3 字符，2 字词全部失效 |
| **本方案（bigram + unigram 双列）** | ✅ 命中 | 见下 |

做法：CJK 切 bigram（`今天的会议` → `今天 天的 的会 会议`），另存一列 unigram 解决单字查询；拉丁/数字出整词。查询走 FTS5 **短语查询**，靠 token 相邻位置实现「子串命中」。

支持：`会议`（2字）、`下午三点`（4字）、`午三点`（子串）、`天`（单字）、`ten`（英文大小写不敏感）；`改会`（跨词）不会误命中。

## 7. 实测数据（2 万条，20 个群，Windows / Python 3.11 / SQLite 3.39）

| 指标 | 数值 |
|---|---|
| 写入 | **7,876 msg / s**（含去重判断） |
| 检索（带 scope） | 中位 **1.6 ms**，最大 10.2 ms |
| 检索（全库跨会话） | 中位 14.7 ms |
| 库体积 | **606 B / 条**（含 FTS 索引） |

个人 agent 场景（十万条以内）用 SQLite 单文件足够，**不需要向量库或 Postgres**。

## 8. 可选：向量检索

接口留好了，默认不启用：

```python
s.set_embedder(lambda texts: [...], model="bge-m3")
s.embed_pending()                       # 增量补算
s.vector_search("会议怎么定的", scope=conv)
```

存储用 `float32` BLOB，检索是**暴力余弦**。到十万条以内都够用；再往上就该换 ANN 索引了（届时把 `vector_search` 换成外部实现即可，schema 不用动）。

## 9. 出站（代发）能力

> ⚠️ 这是本仓库**唯一会对外产生副作用**的部分。它让云工能"以你的身份"在微信里说话，
> 所以默认全部关着，靠闸门逐层放开。

### 状态机

```
draft ──approve()──► approved ──mark_sent()──► sent ──回读验证──► verified
  │                    │                        │
  └──cancel()──► cancelled ◄──cancel()          └──fail()──► failed
```

**关键点：`sent` 不等于"发出去了"。** computer use 按下发送键后，你无法从任何返回值知道结果——
焦点错了、网络慢、被风控拦、消息被折叠，全都"看起来发了"。唯一的证据是**回读**。

### 回读验证：`ingest()` 兼任验证器

下一帧感知读到自己那条消息时，`ingest()` 会自动比对 `outbox` 里 `state='sent'` 的同内容条目：

```python
res = s.ingest(conv, [{"sender": "我", "text": "收到，晚点回你"}])
res["verified_outbox"]        # [3]  → outbox#3 置为 verified
```

并把该 message 标记为 `sent_by='agent'` + `outbox_id=3`。
**匹配前必须确认发送者是"我"**，否则别人说了同样的话会被误判成 agent 发的。

### 四道闸门（命中即 `risk='blocked'`，并写入 `block_reason` 留痕）

| 闸门 | 默认值 | 说明 |
|---|---|---|
| **群聊禁止代发** | 开 | 群里发错是公开事故。确需放开显式传 `allow_group=True` |
| 超长 | 500 字 | 防止把一大段内容灌进输入框 |
| 限流 | 60s / 3 条 | 防止 agent 卡在循环里刷屏 |
| 重复内容 | 600s 内同文 | **死循环特征**：自己发的还没回读，又起草同一条 |

### 发送前预检

```python
s.preflight(conv, seen_title="张三")   # {"ok": True, ...}
s.preflight(conv, seen_title="产品群") # {"ok": False, "reason": "疑似切错窗口"}
```

UI 操作最常见的错误就是切错窗口还照发，所以发送前必须核对前台窗口标题。

### agent 每轮该看的三样

```python
s.outbox_pending(conv)        # draft/approved/sent —— 我欠着哪些没发完
s.outbox_unverified(120)      # sent 超 120 秒还没回读 = 大概率静默失败
s.unresolved()                # 读不准的部分（见 §4）
```

### 归因字段 `sent_by`

`other` / `human` / `agent` / `unknown`。需要先登记"我叫什么"：

```python
s.set_self_label("wechat", "我的昵称")
```

不登记的话所有消息都是 `unknown`，且回读验证无法工作。

## 10. 起草层（草稿模式）

`imem/drafter.py`：读到新消息 → 判断该不该回 → 起草 → `enqueue_draft()`。
**状态停在 `draft`，全程不发送。**

### 默认策略（`Policy`，偏保守）

| 参数 | 默认 | 含义 |
|---|---|---|
| `max_age_s` | 3600 | 超过 1 小时的消息不再起草 |
| `min_gap_s` | 30 | 同一会话两条草稿的最小间隔（`<=0` 表示不限） |
| `skip_groups` | True | 群聊不起草（群发本来就被闸门拦） |
| `only_unanswered` | True | 已经被本人/agent 回过的消息跳过 |
| `max_drafts_per_run` | 5 | 单轮上限 |

### 为什么单独一层

- 选哪些消息值得回、什么时候回，是**策略**问题，不该散落在 agent 主循环里
- 起草质量可以**单独评估**——你只看草稿，不承担发错的后果
- 幂等、限流、冷场判断集中在一处，好测

### 幂等：同一条消息只起草一次

`outbox.source_message_id` 记录"这条草稿是为哪条收到的消息起的"。两道防线：

1. `find_candidates()` 阶段过滤掉已有草稿的消息
2. `enqueue_draft()` 再拦一次，理由写 `这条消息已起草过（message#N）`

### Composer 可插拔

```python
from imem import drafter

def my_composer(ctx):
    # ctx = {conversation, message, recent, related}
    return call_llm(build_prompt(ctx))     # 返回 None = 这条不值得回

drafter.run_once(store, my_composer)
```

内置的 `template_composer` 只是**占位实现**，用来验证链路。它的回复质量不代表真实能力，别直接用。

### 一个必须知道的能力边界

`related`（composer 拿到的相关历史）走的是**子串检索**：
找得到「同一句话的重复出现」，**找不到「换个说法问同一件事」**。
真实语义召回要靠 v1.2 的向量 / RRF 融合，不要在这里假装有。

### 跑一遍

```bash
python tools/draft_demo.py
```

## 11. 尚未实现（按优先级）

1. `search()` 与 `vector_search()` 的结果融合（RRF）——现在两者独立
2. 跨 app 同一实体的**候选建议**（`unresolved()` 已给出素材）
3. 消息编辑/重发的关联（`reply_to_id` 字段已留，未使用）
4. 保留策略与遗忘（按会话 / 按人删除原始消息）
5. 增量导出给 LLM 做摘要的批处理封装

## 12. 目录

```
IM-Agent-Memory/
├── ROADMAP.md            目标、成功判据、版本规划
├── LICENSE               MIT
├── imem/
│   ├── schema.sql        表结构（observation/conversation/entity/alias/message/outbox）
│   ├── text.py           归一化 / CJK 分词 / 时间解析 / 去重锚
│   ├── db.py             连接、初始化、幂等迁移
│   ├── store.py          唯一对外 API（读写 + 出站闸门）
│   └── drafter.py        起草层：候选筛选 / 策略 / composer 插槽
├── tools/
│   ├── mock_demo.py      端到端演示：读 → 检索 → 起草 → 闸门 → 发送 → 回读
│   └── draft_demo.py     草稿模式演示：起草后停在 draft，绝不发送
└── tests/
    ├── run_all.py        统一入口
    ├── test_imem.py      21 个：存储与检索
    ├── test_outbox.py    14 个：出站闸门、归因、静默失败
    └── test_drafter.py   14 个：起草策略、幂等、绝不发送
```

## 13. 许可证

[MIT](LICENSE) © 2026 yushui2022
