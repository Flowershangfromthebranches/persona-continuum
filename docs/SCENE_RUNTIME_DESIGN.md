# Room Scene Runtime 设计文档

## 1. 核心问题与设计目标

在先前的 Persona Continuum 架构中，Room 存在以下系统性缺陷：
1. **缺失场景时间（Scene Time）概念**：会话完全依赖物理世界的调用瞬间（Wall Clock）。当用户表达“我去睡觉了”，系统在逻辑上无法感知时间跨度，内部情感与欲望稳态（Affect & Needs Homeostasis）无法基于真实流逝或叙事推进进行自然衰减与回弹。
2. **时间推进过度依赖括号旁白**：用户被迫充当“场记”，输入形如“（第二天早上）我醒了”来提示时间流逝。
3. **括号文体污染（Screenplay Contamination）**：模型受到含有括号的 Few-shot 样本与历史对话诱导，开始模仿剧本排版输出“（动作）台词（动作）”，甚至逐步从第一人称参与者退化为第三人称编剧或全知旁白。
4. **时间与真实写入混淆**：若强行修改写入时间戳，将破坏审计与幂等回溯。

本设计的目标是实现彻底的宿主托管场景状态（Host-owned Scene State），分离“现实写入时间（`created_at`）”与“世界发生时间（`scene_time` / `occurred_at`）”，将时间推移与离线事件确立为系统状态机而非模型撰写的作文。

## 2. 状态机模型：RoomSceneState 与 SceneEvent

### 2.1 状态结构定义
`RoomSessionState` 引入独立的宿主托管场景状态 `scene_state: RoomSceneState`：
- `scene_time`: 场景世界当前的发生时间（带时区）。
- `timezone`: 场景所在时区，默认 `Asia/Shanghai`。
- `time_mode`: 时间推进模式，支持：
  - `realtime`: 严格依据前后两次物理互动的实际时间差推进。
  - `narrative`: 仅响应用户明确的叙事意图推进时间。
  - `hybrid`（推荐默认）：物理流逝自然累加场景时间；同时明确的用户时间推进意图（如“第二天早上”、“推进3小时”）可进一步推进场景时间。
- `scene_id`: 当前逻辑场景唯一标识符。
- `locations`: 各参与者当前所在空间（如 `{"user": "宿舍", "persona": "宿舍"}`）。
- `activities`: 各参与者当前正在进行的活动（如 `{"user": "sleeping"}`）。
- `activity_started_at`: 活动开始的场景时间戳，用于计算真实活动持续时长。
- `presence`: 参与者在线/在场状态（`present` / `absent`）。
- `last_interaction_scene_time` / `last_interaction_wall_time`: 上一次交互的场景时间与物理墙上时间。
- `elapsed_since_last_interaction`: 距离上一次交互流逝的场景秒数。
- `recent_events`: 场景近期发生的确定性事件列表（最多保留最近30条）。

### 2.2 确定性事件类型与结构
每个确立的场景变动必须形成结构化的 `SceneEvent` 并持久化：
- `id`: 事件全局唯一 ID。
- `room_id`: 关联房间 ID。
- `scene_time`: 事件在场景发生的时间。
- `actor`: 动作主体（`user` 或对应 `persona_id`）。
- `type`:
  - `time_advance`: 场景时间跃迁（如 `{"intent": "next_morning"}` 或 `{"seconds": 28800}`）。
  - `activity_start`: 活动开始（如 `{"activity": "sleeping"}`）。
  - `activity_end`: 活动结束（附带 `duration_seconds`）。
  - `location_change`: 空间转移（如 `{"location": "图书馆"}`）。
  - `presence_change`: 离线或上线（`{"presence": "absent" | "present"}`）。
  - `physical_action`: 肢体动作或非语音互动（如 `{"action": "hug", "target": "user"}`）。
  - `scene_fact`: 宿主确认的客观场景事实。
- `payload`: 结构化参数。
- `source_turn_id`: 引发该事件的输入/输出轮次 ID。
- `confidence`: 置信度。
- `created_at`: 物理数据库写入时间。

## 3. 语义通道三元拆分（Three Semantic Channels）

系统对进入系统的每一轮输入与模型输出进行结构化三通道拆解：
1. `spoken_text`: 纯净的第一人称自然口语对话。
2. `actions`: 肢体动作与交互行为（如拥抱、挥手、坐起）。
3. `scene_events`: 影响环境状态的客观事实变更（如时间推进、睡醒状态、位置转移）。

同时：
- `raw_content`: 原始用户输入或模型生成文本，继续永久保留在数据库历史中以备审计与回溯。
- `non_voice_context`: 无法确认为合法事件的旁白或说明，保留在归一化元数据中，严禁作为语音示例（Voice Exemplar）回传给模型。

## 4. 睡眠行为与离线时间推进策略

当用户输入“我去睡觉了”：
1. 状态机记录 `activity_start(sleeping)`，但**绝不立即自动推进到早晨**。
2. 若用户在 10 秒后再次发送消息“对了，还有件事”，当前场景时间依然是当晚（仅流逝 10 秒），活动保持 `sleeping` 或视新输入重新评估，不产生时间跳跃。
3. 若下一次物理交互发生在现实 8 小时后（在 `hybrid` 模式下），场景时间伴随物理时间自然推进 8 小时，用户活动持续时长累积为 8 小时。
4. 若用户在输入中明确表达“第二天早上我醒了”或“（第二天早上）我醒了”：
   - 提取意图：`time_advance(next_morning)`、`activity_end(sleeping)`、`activity_start(awake)`。
   - 场景时间推进至次日早晨 08:00。
   - 口语文本标准化为“我醒了。”。
   - 提取完成后，**完全无需强迫用户使用括号**。

## 5. 零离线虚构原则（No Offscreen Fiction）

第一版 Scene Runtime 严格限定在物理与状态连续性层面：
- 场景时钟单调推进。
- 情感指数衰减（Affect Decay）。
- 心理与关系需求稳态回弹（Need Homeostasis）。
- 空间与在场状态流转。

严禁在时间过去 8 小时后未经专门的 Continuation Agent 批准自动脑补用户或 Persona “昨晚做了什么梦”、“半夜见了谁”或“离线经历了什么重大变故”。时间流逝是纯粹的客观环境状态，而非编造故事的许可。
