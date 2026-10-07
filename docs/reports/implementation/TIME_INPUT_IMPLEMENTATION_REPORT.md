# 「⏱ 时间」一等输入通道实现报告

## 1. 修改文件列表

- `src/persona_continuum/domain/scene.py`：完善 `SceneEvent`, `RoomSceneState`, `TurnChannels`, `SpatialSceneState`。
- `src/persona_continuum/runtime/temporal_parser.py`：新增确定性时间解析器，支持相对时长（8小时、30分钟）、语义时间点（第二天早上、明天 09:30）与绝对时间（2026-09-20 14:00），严格防范时间倒退与非法输入。
- `src/persona_continuum/runtime/turn_normalizer.py`：支持三模式（speech / action / time）与 mixed turn 分解；修复了动作描述与目标人物重复拼贴的渲染缺陷。
- `src/persona_continuum/runtime/scene_runtime.py`：维护场景时间时钟推进，物理空间姿态与距离状态。
- `src/persona_continuum/room/orchestrator.py`：在 `inject_message` 与 `_inject_message_locked` 中支持 `input_mode="time"`。时间推进模式下更新世界时间、驱动情感衰减（Affect Decay）与需求回弹（Need Homeostasis），不生成 Dialogue Turn，绝不调用模型。
- `src/persona_continuum/web/api.py`：`inject_room_message` 接收并透传 `input_mode`。
- `src/persona_continuum/web/static/index.html`：输入框模式下拉菜单扩展为三种模式：💬 对话、⚡ 动作、⏱ 时间。
- `src/persona_continuum/web/static/app.js`：
  - 动态切换模式 placeholder 与发送按钮文案（“发送” / “行动” / “推进”）。
  - 对话流中将时间推进渲染为独立的分隔线事件（`⏱ 时间推进 ...`），绝不渲染为聊天气泡。
  - 修复动作胶囊（`msg-action`）在说话气泡下重复附加目标角色名（如“回头去看他... ��� 菲比”）的体验缺陷。
- `tests/integration/test_time_input_mode.py`：包含相对时间、语义时间、绝对时间、失败关闭、禁止时间倒退、以及时间推进不调用模型的全套自动化回归测试。

---

## 2. 完整时间数据流与架构

```
用户在 UI 选择 [⏱ 时间] 输入 "+8小时"
          ↓
前端识别 input_mode="time"
POST /api/rooms/{id}/inject { content: "8小时", input_mode: "time" }
          ↓
RoomOrchestrator._inject_message_locked(..., input_mode="time")
          ↓
parse_temporal_input("8小时", reference_time=scene_state.scene_time)
解析得到: kind="relative", seconds=28800, target_scene_time=次日07:30
          ↓
RoomSceneState:
  scene_time += 8小时
  elapsed_since_last_interaction = 28800s
  last_interaction_scene_time = scene_time
  last_interaction_wall_time = now(UTC)
          ↓
AffectEngine & MotivationEngine:
  使用新的 scene_time 计算情感指数衰减与内在需求回弹稳态
          ↓
SceneEvent(type="time_advance", payload={...}) 生成并记录
          ↓
Transcript 记录:
  spoken_text: ""
  input_mode: "time"
  content: "时间推进 8小时 (8小时)"
  (绝无用户说出的台词，不进入 Dialogue Few-shot / Voice Exemplar)
          ↓
不调用 Persona Model！直接返回，无模型生成开销与文体污染
          ↓
UI 接收 WebSocket 广播 / Transcript:
  呈现为居中的 ──── ⏱ 时间推进 8小时 ──── 场景分割线
          ↓
用户后续发送第一条语音 [💬 对话] "老婆，早啊"
          ↓
Persona 接收到的 Prompt:
  ## Current Scene State
  {"current_scene_time": "2026-09-19T07:30:00+08:00", "elapsed_since_last_interaction_seconds": 28800.0}
  Recent Room Dialogue 中只有纯净对话，Persona 自然从清晨开始交互
```

---

## 3. 动作结束附带人物名称的缺陷修复

### 现象原因
在前端 `src/persona_continuum/web/static/app.js` 中：
```javascript
return verb ? `<div class="msg-action">${esc(verb)}${tgt && tgt !== "unspecified" ? ` ��� ${esc(tgt)}` : ""}</div>` : "";
```
当模型生成结构化动作且 `target_id` 为具体人物（如 `菲比`）时，前端渲染无论动作内容本身有多长（如“回头去看他，伸手抓住他扣在自己腰上的手”），都会在末尾无条件追加 ` ��� 菲比`，导致对话气泡下方出现异常后缀。

### 修复方案
在消息气泡下方的动作胶囊中移除冗余的 ` ��� ${esc(tgt)}` 后缀，直接展示自然动作描述，彻底消除了“…… ��� 菲比”的重复杂音。
