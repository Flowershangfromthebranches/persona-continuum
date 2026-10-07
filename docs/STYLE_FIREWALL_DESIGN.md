# Style Firewall 架构设计文档

## 1. 为什么纯 Prompt 防火墙会失效

在以往版本中，即使在 Prompt 中加入“Do not write a screenplay or parenthetical voiceover”，模型仍然会高频输出括号夹心排版（如 `（抱住你）早安。（揉眼睛）`）。其本质原因在于：
1. **Few-shot 上下文的强归纳偏差**：Recent Transcript 与 Recall 记忆中充斥着历史沉淀的剧本括号排版。LLM 对上下文既有示例的模仿偏好远强于指令遵循。
2. **记忆通道混淆了“事实”与“语调”**：系统将以前的对话原文作为 `digital_experience` 直接存入向量库与 FTS。当检索唤醒旧记忆时，检索到的不仅是“说了什么”，更是“剧本格式”。
3. **动作通道缺失**：Persona 没有独立的动作表达槽位，一旦产生肢体互动需求，只能借助自然语言括号作为妥协。

## 2. 双重防火墙体系：Memory 与 Voice 彻底解耦

Style Firewall 确立的核心原则：
> **Memory 负责记录“客观发生了什么”；只有受控的 Voice Exemplar 才负责告诉 Persona “应该怎样说话”。**

### 2.1 检索角色划分（retrieval_role）
系统定义了明确的五类记忆检索角色：
- `fact`: 客观知识、背景事实（不携带口吻）。
- `event`: 客观历史事件资料（包含行为主体与事实动作，采用非口语化的中立事件陈述）。
- `relationship`: 人际关系变化与互动轨迹。
- `voice_exemplar`: 经过认证的第一人称纯净语调范例。
- `raw_archive`: 包含原始排版的历史原始记录，**仅用于只读审计，从一切默认检索与 FTS 索引中硬性屏蔽**。

### 2.2 语义经验转换（Semantic Experience Transformation）
原系统的 `digital_experience` 记录方式：
```text
User asked: {user_message}
Persona answered: {persona_response}
```
这种格式将双方的所有括号、长动作、编剧调度直接写死进长期记忆。

新系统在提交轮次时，生成剥离文体的 `semantic_experience`：
1. 将用户与 Persona 提取出的结构化事件记录为客观事实列表（如主体、动作类型、目标）。
2. 将双方真实表达的口语要点清洗为陈述句（如 `user表述的内容：我醒了；Persona表述的内容：早安。`）。
3. 带有 `retrieval_role="event"` 标识，并附注“本次交流事件资料”，彻底切断括号与镜头描写的传播链路。

## 3. 上下文构建防火墙（PromptComposer Normalization）

在组装下一轮 Prompt 时：
1. **Recent Transcript 过滤**：调用 `normalize_turn_for_prompt()`，仅输出参与者的 `spoken_text`。动作与场景变更作为独立的结构化事实输出到 `## Scene Facts (not voice examples)`。
2. **Host-owned Scene State 注入**：
   明确告知模型：
   ```markdown
   ## Current Scene State
   This is host-owned factual state. Do not repeat it as narration. Speak from the current moment.
   {"current_scene_time": "2026-09-19T08:00:00+08:00", ...}
   ```
   Persona 只能以此为当前立足点发言，严禁生成“八小时过去了，阳光照进窗户……”等全知叙事。
3. **结构化 Action 协议与原生回退**：
   - 当底层模型支持 Schema 输出时，定义 `SceneOutput` 包含 `speech`、`actions` 和 `scene_updates`。
   - 当模型产生非结构化输出时，宿主 Deterministic Post-parser（`TurnNormalizer`）自动剥离括号并提取动作；如果模型产生了包含说明性前缀的非法混合输出（如 `核对契约...{"speech":...}`），解析器精确提取 JSON 并将非语音文本沉降至审计元数据，严禁流入对话通道。
   - 当检测到 Persona 仍然输出非法括号时，自动在房间元数据中标记 `voice_reset_participants`，在下次发言前重置底层会话上下文游标，防止受污染历史在物理会话中恶性循环。
