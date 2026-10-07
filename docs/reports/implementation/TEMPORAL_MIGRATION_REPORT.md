# 场景时间与文体历史数据平滑迁移报告

## 1. 迁移目标与安全性准则

根据工程约束，历史迁移必须保证：
1. **只读性与无损性**：绝对禁止物理删除任何原始轮次（`room_transcripts` 与 `session_turns` 的 `content` / `persona_response` 保持完全不变）。
2. **状态不变性**：不得重置 Persona ID、分支结构、Relationship（亲密度/信任度）、Affect（情感参数）以及 Needs（生理与心理需求数值）。
3. **幂等性与可回滚**：迁移内置事务保存点与版本记录（`runtime_data_migrations`），重复执行直接安全跳过。

## 2. 数据库迁移实操与演练结果

### 2.1 数据库备份与指纹验证
在执行生产库迁移前，在 `/Users/leaf/.persona-continuum/backups/scene-runtime-20260918-125126/` 创建了冷备副本 `before.sqlite`，并由 `scripts/migrate_room_scene_history.py` 校验核心表的 SHA-256 签名。

受保护校验表：
- `raw_transcript`
- `session_turns`
- `relationships`
- `affect`
- `needs`
- `sessions`
- `personas`

### 2.2 演练与执行指标
- **数据库路径**: `/Users/leaf/.persona-continuum/persona_continuum.sqlite`
- **迁移版本号**: `room_scene_style_firewall_v1`
- **处理房间数 (Rooms)**: 28 间
- **处理对话轮次 (Turns)**: 727 轮
- **归档旧数字经验 (Archived Memories)**: 509 条（标记为 `retrieval_role='raw_archive'` 并移出 FTS）
- **构建语义经验记忆 (Semantic Memories)**: 498 条（新生成干净事件事实）
- **FTS5 全文索引重建**: 完成，仅索引有效且非归档的语义事实
- **受保护状态一致性 (protected_state_unchanged)**: `true`（迁移前后哈希完全一致）

## 3. 房间与转录元数据结构更新

对于每一个房间的历史转录记录：
- 在保持原始文本的前提下，分析并提取其中的通道数据，存入每条记录的 `metadata_json.channels`（包含 `spoken_text`, `actions`, `scene_events`, `scene_time`）。
- 确立结构化事件并写入新表 `room_scene_events`。
- 为每个房间构建初始 `scene_state`，推导最终场景时钟。
- 将旧的 `rolling_summary` 备份至 `raw_archive_rolling_summary_v0`，并基于无污染的通道事实重建 `rolling_summary`。
- 标记相关参与者在下次发言前刷新原生会话缓存，阻断旧 Native Thread 中的残余剧本历史。
