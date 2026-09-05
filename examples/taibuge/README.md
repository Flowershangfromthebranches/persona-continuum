# 太卜阁示例 / Taibuge Demo

[中文](#中文) · [English](#english)

## 中文

此目录发布六个可直接导入的示例 Persona 编译包（玄衡先生、子平先生、紫薇先生、易卦先生、三式先生、西学占测师）。包采用 `public_compiled` 导出模式：仅保留运行所需的编译人格组件，不包含任何原始资料、证据声明、历史记忆、本地对话记录、房间历史、运行时状态或本机私有凭据。

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

## English

This directory provides six importable sample Persona compiled packages (Xuanheng, Ziping, Ziwei, Yigua, Sanshi, and Western Divination Consultant). Archives use the `public_compiled` export mode: they include only runtime-ready compiled persona components, and exclude raw source materials, evidence/claims, memories, local conversation records, room histories, runtime states, and private credentials.

> **Risk notice:** Divination and fortune-telling outputs in this template and demo are for entertainment and traditional-culture research only. Do not rely on them as investment, medical, legal, or other professional advice. Investing involves risk; make independent decisions using reliable information and consult licensed professionals when appropriate.

### Quick start

1. Install the project and run `uv run persona-continuum init`.
2. Import all six archives from `personas/` with `uv run persona-continuum import <archive>`.
3. Start the Web UI, create a Room, and select the built-in `太卜阁 · 术数综合会诊` template. The template matches the imported Personas by name.
4. Select a locally available Agent Host, model, and Reasoning Effort for each seat, review the bindings, and start the Room.

A new Room now opens blank: no title, protocol, discussion/speaker mode, or participants are prefilled. Configuration appears only after choosing a template or adding seats manually.

No end-user demo or template is provided for Parallel World or Narrative Studio in this release. Their core implementation and tests remain available.
