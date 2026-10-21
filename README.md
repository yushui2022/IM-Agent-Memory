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
  │  ╲                 │                        │
  │   ╲auto_approve    └──cancel()──► cancelled └──fail()──► failed
  │    ╲（策略自动放行，人不在场）
  └──cancel()──► cancelled

escalate() ──► escalated ──resolve_escalation()──► cancelled
              （不生成回复，等人工）
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

## 10. 决策层（默认自治）

`imem/drafter.py`：读到新消息 → 判断 → **自动回 / 沉默 / 升级给人**。

目标不是"帮人起草"，是**代替人处理**。所以默认路径是直接处理掉，
人只在真正该由人拍板的时候被惊动。

### 三条出口

| 出口 | 什么时候 | 结果 |
|---|---|---|
| **reply** | 该回、也回得来 | 起草 + 按策略自动放行（`state='approved'`、`auto_approved=1`），交给执行器 |
| **silent** | 确认语、纯附件、不关我事、不在白名单 | 什么都不做。**这是大多数消息的正确答案** |
| **escalate** | 涉及钱/合同/投诉、日内频次超限 | 不生成回复，转人工（`state='escalated'`） |

### 默认策略（`Policy`）

| 参数 | 默认 | 含义 |
|---|---|---|
| `auto_approve` | **True** | 起草后自动放行，不需要人批 |
| `scope_allowlist` | `None` | `None`=私聊全放开 / `[]`=全禁用 / `[conv…]`=仅这些会话自治 |
| `skip_groups` | True | 群聊永久排除（群发是公开事故） |
| `max_auto_per_day` | 20 | 单会话每日自动回复上限，超了转人工（熔断） |
| `escalate_hints` | 合同/金额/投诉/律师… | 命中即升级，不做主张 |
| `only_unanswered` | True | 已被本人/agent 回过的跳过 |
| `max_age_s` / `min_gap_s` / `max_per_run` | 3600 / 30 / 5 | 时效、防刷、单轮上限 |

### 沉默比回复更重要

一个什么消息都回「收到」的助手，比不说话的更烦人。`should_reply()` **默认返回 False**：

- 纯确认语（收到 / 好的 / 嗯 / 👌）→ 不回
- 纯附件（`[图片]` `[语音]`）→ 不回
- 没有疑问或请求信号的陈述句 → 不回

只有出现问号、疑问词，或明确请求（"帮我 / 麻烦 / 发一下 / 确认"）才进入起草。

### 升级：唯一该惊动人的情况

命中 `合同 / 金额 / 报价 / 付款 / 投诉 / 律师 / 急 / 领导说` 这类词时，agent **不做主张**，
直接 `escalate()` 并附理由。这种事替人拍板，比不回更糟。

### 幂等：同一条消息只处理一次

`outbox.source_message_id` 记录来源消息。两道防线：`find_candidates()` 过滤 + `enqueue_draft()` 二次拦截。

### Composer 可插拔

```python
from imem import drafter

def my_composer(ctx):
    # ctx = {conversation, message, recent, related}
    return call_llm(build_prompt(ctx))     # 返回 None = 这条不值得回

drafter.run_once(store, my_composer)
```

内置 `template_composer` 只是**占位实现**，用来验证链路，回复质量不代表真实能力。

### 一个必须知道的能力边界

`related`（composer 拿到的相关历史）走**子串检索**：找得到「同一句话的重复出现」，
**找不到「换个说法问同一件事」**。真实语义召回要靠 v1.2 的向量 / RRF 融合。

### 跑一遍

```bash
python tools/auto_loop_demo.py     # 自治回路：读 → 判断 → 回/沉默/升级 → 推送
```

## 11. 动向推送（接到手机）

> **本仓库不做手机端。** 手机端就是飞书 / Telegram / 邮件的 App 本身——
> 我们只给机器人发一条消息，推送走它们自家的通道。

常态是「✅ 刚替你回了 N 条」，看过就行；只有 agent 拿不准时才出现「🚨 需要你介入」：

```
🚨 需要你介入 1 条

#1 [王五] 涉及「合同」，应由本人判断
   收到：合同你签了吗

──────────

✅ 刚替你回了 1 条

· [张三] 评审改到几点了？ → 收到，我确认一下再回你。
```

| 通道 | 手机端 | 需要什么 |
|---|---|---|
| **飞书群机器人 webhook** | 飞书 App | 建群 → 群设置 → 群机器人 → 添加 → 复制 URL。**最简单** |
| Telegram Bot | Telegram App | @BotFather 拿 token |
| SMTP | 系统邮件 | 邮箱账号 |

### 命令

```bash
export IMEM_FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxx'

python tools/push_drafts.py --db imem.db            # dry-run，只打印
python tools/push_drafts.py --db imem.db --send     # 推到你手机
python tools/push_drafts.py --db imem.db --resolve 1  # 处理完第 1 条升级项
```

编号按 `outbox.id` 升序固定，避免每次顺序不同导致点错条目。

`--approve` / `--cancel` 仍保留，用于 `auto_approve=False`（人工审稿模式）下的手写草稿。

### 推送 ≠ 发送

推送只是告诉你情况。真正的发送由**执行器**在 UI 上做，然后回读验证（见 §9）。
**这个脚本永远不会替你发消息**——有测试守着这条红线。

### 人的介入方式的三档演进

| 档 | 你要做什么 | 需要什么 | 状态 |
|---|---|---|---|
| **L0 单向推送** | 看「已替你回了」，偶尔处理「需要你介入」 | 一个 webhook URL | ✅ 已实现 |
| L1 回复关键词 | 在飞书回 `ok 2` 处理升级项 | 飞书长连接（不需要公网 IP） | ⬜ |
| L2 卡片按钮 | 点消息里的按钮 | 自建应用 + 卡片回调 | ⬜ |

自治模式下，人的常态动作是**什么都不用做**——L1/L2 只是把偶尔的升级处理挪到手机上。

## 12. 采集：把屏幕上的画面变成记忆

记忆层本身不看屏幕（§1）。但「agent 用 computer use 操作 IM」这条路要走通，
中间必须有一层把像素变成 `ingest()` 吃得下的 `{sender, text, ts}`。

拆成三块，各自可测：

```
vision.py    帧差分 → 「这一帧值不值得花钱」   纯标准库，零模型调用
extract.py   多模态抽取 → 候选消息草稿         厂商无关，只负责 prompt 与解析
pipeline.py  草稿 → insert / confirm          一步落地，防止漏掉跨帧证据
```

### 为什么必须先过一道帧闸门

全屏帧连续送视觉模型是最贵的做法：1280×800 一帧上千 token，1fps 常驻
一天几十亿 token。真实可用的形态只能是：

    本地廉价判断「画面变没变」 → 只有变了才送模型

`Gate` 用格平均亮度差分做这件事，并且**分区判断**：

| 区域 | 变化意味着 | 是否触发 |
|---|---|---|
| 标题栏 | 换会话了 | ✅ 必须重读 |
| 消息正文 | 有新内容 | ✅ 变化格数 ≥ `min_tiles` |
| 会话列表 | 别处来了消息 | ✅ |
| 输入框 | 自己在打字 | ❌ 否则自己把自己刷醒 |

```python
gate = vision.Gate(tile=4, min_tiles=6)
v = gate.push(gray_bytes, w, h)
if v.trigger:                 # v.reason 会告诉你是哪一类变化
    ...
```

### 模型说的话不能直接进库

`extract.py` 的全部职责就是**不信任模型**：

- 时间只在界面明确显示时才填，**绝不根据消息顺序推算**（写死在 prompt 里）
- 发言人按界面原文保留，消歧交给 `who()`
- 消息类型、置信度、长度全部过一遍校验
- 解析宽松（剥 markdown、捞 JSON），校验严格

### 跨帧一致性：`observed → confirmed`

同一条消息被**两次独立抽取**都读到，才升为 `confirmed`。证据有两个来源：

1. 模型按提示去重后，把它放进 `result.repeats`
2. 端侧 `client_ref` 撞上，`ingest()` 回传 `stats['existing']`

两条都要认。`pipeline.commit()` 把它们一起交给 `confirm_many()`。
这是跑 demo 踩出来的坑：只认第一条的话，**模型越听话，`confirmed` 永远是 0**。

### 已知边界（说清楚，不粉饰）

- **没有事件驱动，只有轮询。** 秒级做不到，分钟级可以。
- **同一时刻只看得到一个会话。** 要覆盖全部得主动「扫未读 → 切过去 → 抓屏」。
- **没有时间戳的消息**，去重锚会随抓取时刻漂移，所以用
  `sha1(会话|发言人|正文)` 做跨帧稳定的 `client_ref`。
  代价是同一人两句一模一样的「好的」会折叠成一条。
- 送模型前裁剪掉会话列表和桌面背景：省 token，也避免模型把别的会话当成当前对话。

这些不是待修的 bug，是「以人的视角读屏幕」这条路的固有代价。

### 跑一遍（不需要真微信、不需要真模型）

```bash
python tools/collect_demo.py
```

输出会逐帧显示：闸门为什么判、哪几次真的调了模型、哪条消息因为被第二次看到而升为 `confirmed`。

### 会话切换调度（v1.0.6）

前一节只解决"画面变了就读"。但它有个前提问题：**同一时刻你只看得到一个会话。**
别的群来了消息，消息区根本没打开，读不到。

会话列表是唯一能同时看到所有会话的地方，所以要"扫未读 → 切过去 → 读 → 回来"。

**但切换不是中性的读取。在微信里点进一个会话，就等于把它标成已读。**
一个只顾刷全量的 agent 会让你的会话列表永远是空的——信息全进了库，
你却再也分不清哪些是自己看过的。

所以策略层先回答"能不能动手"，再回答"动哪个"：

| 规则 | 行为 |
|---|---|
| 用户空闲 < `user_idle_s` | **一律不动**。抢焦点会打断正在打字的人 |
| 本轮已切 ≥ `max_per_cycle` | 剩下的排下一轮 |
| 本小时已切 ≥ `hourly_allowance` | 整轮停手 |
| 该会话在 `cooldown_min` 内切过 | 跳过 |
| 该会话已经打开 | 跳过（点了也没新信息，白吃一次已读） |
| 拿不到空闲时间 | 按无人值守处理（`xprintidle` 缺失时） |

```python
badges  = scan.find_red_badges(rgb, w, h)       # 免费，不调模型
signals = scan.RowTitles().apply(scan.to_signals(badges))
p       = plan.plan(signals, policy=..., user_idle_s=idle)
```

### 三重保险，防串会话

切换在屏幕上就是"点某个坐标"。点歪一格，后果不是读到旧内容，而是
**把 A 群的记录写进 B 群**——事后完全看不出来，因为消息内容本身正常。

| # | 检查 | 通过条件 |
|---|---|---|
| 1 | 点击前留帧 | — |
| 2 | 点击后**标题栏必须变** | 没变 = 没切成功，不采 |
| 3 | 模型从标题栏读回的会话名 | 与预期不符就**整批丢弃**（`pipeline.commit(expect_conversation=...)`） |

第 2 步只能证明"画面变了"，第 3 步才能证明"变到谁身上了"。两道都要。

首次接触某个会话时没有预期名字，此时以标题栏读到的为准——
这反而是自纠错的：不管点到了谁，都记在它真正的名字下。

### 没做的：切回去

准确恢复需要知道"原来那个会话在第几行"，而界面不会把行号画出来，没有可靠反查办法。
硬猜只会更乱，所以默认不恢复。代价是：一轮调度结束后，屏幕停在最后读的那个会话。

### 跑一遍

```bash
python tools/scan_demo.py
```

四轮演示：发现 3 个未读 → 用户正在打字按兵不动 → 冷却期内不重切 →
标题栏读回不一致时整批丢弃。

## 13. 尚未实现（按优先级）

1. `brief()`：按人聚合「他是谁 / 上次说到哪 / 你欠不欠回复」（原始素材已有，缺聚合）
2. 切换后恢复原会话（当前没有可靠的行号反查办法，见 §12）
3. 未读条数：角标里的数字要 OCR，现在只知道"有未读"
4. `search()` 与 `vector_search()` 的结果融合（RRF）——现在两者独立
5. 跨 app 同一实体的**候选建议**（`unresolved()` 已给出素材）
6. 消息编辑/重发的关联（`reply_to_id` 字段已留，未使用）
7. 保留策略与遗忘（按会话 / 按人删除原始消息）

## 14. 目录

```
IM-Agent-Memory/
├── ROADMAP.md            目标、成功判据、版本规划
├── LICENSE               MIT
├── imem/
│   ├── schema.sql        表结构（observation/conversation/entity/alias/message/outbox）
│   ├── text.py           归一化 / CJK 分词 / 时间解析 / 去重锚
│   ├── db.py             连接、初始化、幂等迁移
│   ├── store.py          唯一对外 API（读写 + 出站闸门）
│   ├── drafter.py        起草层：候选筛选 / 策略 / composer 插槽
│   ├── notifier.py       草稿推送：飞书 webhook / Telegram / SMTP
│   ├── vision.py         帧闸门：格差分 / 分区判断（纯标准库）
│   ├── extract.py        多模态抽取：prompt / 解析 / 校验（厂商无关）
│   ├── scan.py           会话列表未读角标 + 行号→会话名缓存
│   ├── plan.py           切换策略：用户活跃 / 冷却 / 预算
│   └── pipeline.py       草稿 → 入库 + 跨帧 confirm + 身份核对
├── tools/
│   ├── mock_demo.py      端到端演示：读 → 检索 → 起草 → 闸门 → 发送 → 回读
│   ├── draft_demo.py     人工审稿模式演示：停在 draft，绝不发送
│   ├── auto_loop_demo.py 自治回路演示：读 → 判断 → 回/沉默/升级 → 推送
│   ├── push_drafts.py    推送动向到手机 + 处理升级项
│   ├── collect_demo.py   采集演示：合成帧 → 闸门 → 抽取 → 入库（零依赖）
│   └── scan_demo.py      切换演示：角标 → 计划 → 切换 → 核对 → 入库（零依赖）
└── tests/
    ├── run_all.py        统一入口
    ├── test_imem.py      21 个：存储与检索
    ├── test_outbox.py    19 个：出站闸门、归因、静默失败
    ├── test_drafter.py   17 个：起草策略、幂等、绝不发送
    ├── test_notifier.py  15 个：渲染、通道、失败隔离、CLI
    ├── test_vision.py    22 个：格差分、分区触发、不该刷醒的场景
    ├── test_extract.py   47 个：解析容错、字段校验、跨帧重复、身份护栏
    ├── test_scan.py      29 个：角标检测、误报压制、行号缓存
    └── test_plan.py      17 个：该拒绝的场景一个都不许放行
```

## 15. 许可证

[MIT](LICENSE) © 2026 yushui2022
