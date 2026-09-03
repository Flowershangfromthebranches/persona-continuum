# 太卜阁示例 / Taibuge Demo

[中文](#中文) · [English](#english)

## 中文

此目录只发布用户明确指定的六个可导入 Persona 包，以及一次经过选择的“太卜阁”房间公开演示。包使用 `public_compiled` 导出模式：保留运行所需的编译人格组件，但不包含原始资料、证据/声明、记忆、本地对话、房间记录、关系状态、运行时状态、凭据或本机路径。

> **风险提示：** 本模板及演示中的术数/算命演算仅供娱乐与传统文化研究，请勿过度迷信；不构成投资、医疗、法律或其他专业建议。投资有风险，请依据可靠信息独立判断，必要时咨询持牌专业人士。

### 快速使用

1. 安装项目并执行 `uv run persona-continuum init`。
2. 导入 `personas/` 下的六个包：

   ```bash
   uv run persona-continuum import examples/taibuge/personas/xuanheng-xiansheng.persona.zip
   uv run persona-continuum import examples/taibuge/personas/ziping-xiansheng.persona.zip
   uv run persona-continuum import examples/taibuge/personas/ziwei-xiansheng.persona.zip
   uv run persona-continuum import examples/taibuge/personas/yigua-xiansheng.persona.zip
   uv run persona-continuum import examples/taibuge/personas/sanshi-xiansheng.persona.zip
   uv run persona-continuum import examples/taibuge/personas/western-divination-consultant.persona.zip
   ```

3. 启动 Web UI，新建房间，选择内置模板“太卜阁 · 术数综合会诊”。模板会按名称匹配六位已导入人物。
4. 为每个席位选择本机可用的 Agent Host、模型与 Reasoning Effort，核对后启动房间。

新建房间默认是空白的：不预填标题、协议、讨论方式、发言方式或参与者。只有选择模板或手动添加席位后，才会写入配置。

本版本不提供平行世界或叙事创作的用户演示与模板。相关核心功能和测试仍保留。

演示记录见 [`demo-conversation.md`](demo-conversation.md)。演示按本地房间的公开消息顺序抄录；模型输出可能存在矛盾、遗漏或事实错误，不应作为投资依据。

## English

This directory publishes only the six explicitly selected, importable Persona packages and one selected Taibuge Room demonstration. The archives use the `public_compiled` export mode: runtime-ready compiled persona components are included, while raw source material, evidence/claims, memories, local conversations, Room records, relationship state, runtime state, credentials, and local paths are excluded.

> **Risk notice:** Divination and fortune-telling outputs in this template and demo are for entertainment and traditional-culture research only. Do not rely on them as investment, medical, legal, or other professional advice. Investing involves risk; make independent decisions using reliable information and consult licensed professionals when appropriate.

### Quick start

1. Install the project and run `uv run persona-continuum init`.
2. Import all six archives from `personas/` with `uv run persona-continuum import <archive>`.
3. Start the Web UI, create a Room, and select the built-in `太卜阁 · 术数综合会诊` template. The template matches the imported Personas by name.
4. Select a locally available Agent Host, model, and Reasoning Effort for each seat, review the bindings, and start the Room.

A new Room now opens blank: no title, protocol, discussion/speaker mode, or participants are prefilled. Configuration appears only after choosing a template or adding seats manually.

No end-user demo or template is provided for Parallel World or Narrative Studio in this release. Their core implementation and tests remain available.

See [`demo-conversation.md`](demo-conversation.md). It preserves the order of the selected Room's public messages. Model output may be inconsistent, incomplete, or factually wrong and must not be used as investment guidance.
