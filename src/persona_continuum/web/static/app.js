// Persona Continuum — Full-Featured Web Application Controller
// Styled with Apple-inspired Token System & Comprehensive Protocol Runtimes

(function () {
  const ROOM_UI_STATE = window.PersonaRoomUiState;
  if (!ROOM_UI_STATE) throw new Error("Room UI state contract failed to load");

  // ─── Bilingual Dictionary ────────────────────────────────────────────────
  const I = {
    "zh-CN": {
      navRooms: "房间", navWorlds: "平行世界", navNarrative: "叙事创作", navAgents: "智能体", navApi: "API", navPersonas: "人物",
      narrEyebrow: "Narrative Studio · 短剧创作工作台", narrTitle: "叙事创作",
      narrLead: "先设定故事，再生成圣经与大纲，然后按集创作、审核并提交正史。",
      narrNew: "新建作品", narrRefresh: "刷新", narrCreate: "创建", narrCharSubmit: "添加",
      narrSelectAll: "全选", narrDeleteSelected: "删除所选",
      narrNewHelp: "填写完整作品设定后再开始。默认微短剧 60 集、90–120 秒，可随时修改。",
      narrTabSettings: "作品设定", narrTabBible: "故事圣经", narrTabCast: "角色",
      narrTabOutline: "全剧大纲", narrTabEpisode: "单集创作", narrTabContinuity: "连续性",
      narrTabProduction: "制作",
      narrBible: "故事圣经", narrCast: "角色", narrEpisodes: "剧集",
      narrKnowledge: "信息差", narrClues: "伏笔与线索", narrProduction: "制作",
      narrBibleGen: "AI 重新生成", narrBibleEdit: "编辑", narrBibleSave: "保存新版本",
      narrBibleVersions: "版本历史", narrCharAdd: "添加角色", narrCharCreate: "创建轻量虚构 Persona",
      narrOutline: "AI 生成 / 重新生成", narrDraft: "生成剧本", narrAudit: "审核", narrCommit: "提交正史",
      narrEpisodesTitle: "单集创作", narrNoProjects: "还没有作品，点击「新建作品」开始。",
      narrBack: "返回作品列表", narrRuntimeOpen: "⚙ 创作模型", narrRuntimeTitle: "创作模型",
      narrRuntimeHelp: "默认只设置一个创作模型。AI Agent 失败将直接报错，不会静默使用规则模式。",
      narrRuntimeDefault: "默认创作模型", narrRuntimeOverrides: "高级：按阶段覆盖",
      narrRuntimeOverrideHelp: "每个阶段默认继承默认创作模型。打开覆盖后才单独选择。",
      narrGenModeHelp: "AI Agent失败将直接报错，不会静默使用规则模式。",
      narrAdvancedMode: "显示技术详情（Runtime / World / Branch / JSON）",
      narrSettingsHelp: "完整填写作品类型、集数与时长。这些设定会进入后续生成。",
      narrDangerTitle: "危险区", narrDangerHelp: "删除作品不可恢复。", narrDelete: "删除作品",
      narrBibleHelp: "核心故事约束。作者秘密默认折叠，不会注入角色知识。",
      narrCastHelp: "核心角色绑定已有完整 Persona；普通剧情角色可以不绑定。",
      narrCastAdvanced: "高级操作",
      narrLightPersonaHelp: "创建轻量虚构 Persona：用于次要角色快速模拟。这不会运行完整 Persona Creation。核心角色建议绑定已有完整 Persona。",
      narrOutlineHelp: "按集查看目标、钩子与悬念。点击一集进入单集创作。",
      narrContinuityHelp: "信息差、伏笔、角色弧与 Canon 状态。技术 ID 默认隐藏。",
      narrProductionHelp: "按集展开剧本、分镜、画面与成片提示。不要只看截断文本。",
      narrWriterAdvanced: "高级创作工具 · Writer Room",
      narrWriterHelp: "不会默认使用故事人物当编剧。请先在创作模型设置中配置 Writer Personas。",
      narrArcs: "角色弧 / Plot Threads", narrCanon: "Canon 状态",
      narrFlowSettings: "作品设定", narrFlowBible: "故事圣经", narrFlowCast: "绑定核心人物",
      narrFlowOutline: "全剧大纲", narrFlowEpisode: "单集创作",
      narrFlowAudit: "审核并提交正史", narrFlowProd: "制作包",
      localFirst: "本地优先运行时",
      roomsEyebrow: "Multi-Agent Room · Local-first",
      roomsTitle: "多人讨论，本地编排",
      roomsLead: "把已编译人物与本地 Agent 宿主解耦绑定，由 Director 推进每一回合。",
      newRoom: "新建房间", filterAll: "全部", filterActive: "进行中", filterPaused: "已暂停", filterStopped: "已结束",
      backRooms: "返回房间", lobbyTitle: "房间配置",
      lobbyLead: "人物身份与运行时、模型、思考强度分开绑定，启动时冻结快照。",
      randomize: "随机绑定", preview: "解析预览", startRoom: "启动房间",
      roomSettings: "房间设置", labelTitle: "标题", labelTopic: "议题", labelDirector: "Director 模式",
      slots: "参与者席位", addSlot: "添加席位", previewTitle: "绑定快照预览",
      previewHelp: "启动前检查将冻结的引擎组合。",
      nextTurn: "下一回合", speak: "指定发言", pause: "暂停", resume: "继续", resumeReady: "恢复为就绪", cancelTurn: "取消当前轮", stop: "结束房间",
      pauseRoom: "暂停该房间？", stopRoom: "结束该房间及其自动讨论？",
      emptyAgents: "没有匹配的智能体", emptyAgentsHint: "尝试重新扫描本地宿主，或切换筛选条件。",
      inject: "注入", injectPh: "向讨论注入一句主持提问…",
      inspState: "人物状态", inspRelation: "关系状态", inspRecall: "召回证据", inspBind: "引擎绑定",
      selectParticipant: "选择一位参与者查看其运行时状态。",
      stateBaseline: "基线", stateConfidence: "置信度", stateUpdated: "更新于", stateTrigger: "触发",
      viewAll: "查看全部", collapseAll: "收起",
      noState: "该人物尚无运行时状态记录。", noRelation: "该人物尚无关系状态记录。",
      stateLoading: "正在读取运行时状态…", stateError: "运行时状态读取失败。",
      stateDelta: "本回合状态变化", relationOf: "对象",
      agentsEyebrow: "Discovery", agentsTitle: "本地智能体发现",
      agentsLead: "探测 CLI、App Server、桌面宿主与协议能力。不调用云端密钥。",
      rescan: "重新扫描", ready: "就绪", authNeeded: "需要认证", statusDetected: "已检测未验证", statusBroken: "运行异常", statusDisabled: "未安装",
      apiEyebrow: "OpenAI-compatible", apiTitle: "API 提供方",
      apiLead: "所有模型密钥由 CredentialManager 加密保存并仅在请求期间解密。",
      addApi: "添加提供方", apiName: "名称", apiUrl: "Base URL", apiEnv: "API Key",
      apiModel: "默认模型（可选）", cancel: "取消", save: "保存", create: "构建并初始化", execute: "执行动作",
      personaEyebrow: "Agent / Profile Library", personaTitle: "人物 / 档案库",
      personaLead: "统一管理人物、组织、机构与群体 Agent 档案；Persona 仍由正式证据、记忆与编译链路提供权威内容。",
      searchPersona: "搜索档案",
      footerNote: "本地优先 · 真实协议与因果推演系统",
      emptyRooms: "还没有房间", emptyRoomsHint: "创建一个房间，开始多人讨论。",
      emptyWorlds: "还没有平行世界", emptyWorldsHint: "设定初始历史分歧条件，开启推演。",
      emptyApi: "尚未配置提供方", emptyApiHint: "可添加本地 Ollama 或其它兼容端点。",
      emptyPersona: "没有匹配的人物",
      openRoom: "打开", openWorld: "进入推演", delete: "删除", turns: "回合",
      participant: "席位", remove: "移除", persona: "人物", host: "宿主", model: "模型", effort: "思考强度",
      bestReady: "使用最就绪宿主", randomAgent: "随机宿主", defModel: "宿主默认", randomModel: "随机兼容模型",
      dirDirector: "Director（启发式）", dirNatural: "自然流动", dirRR: "轮转", dirManual: "手动点名",
      titleNeed: "请填写房间标题。", slotNeed: "至少保留一个席位。",
      resolving: "正在解析绑定…", frozen: "启动时冻结",
      deleteRoom: "删除房间及其转写？", deleteApi: "删除该 API 配置？", deletePersona: "删除该人物？",
      confirm: "确认", test: "探测",
      scanning: "正在重新扫描本地宿主…", scanned: "扫描完成",
      saved: "已保存", tested: "探测完成",
      deletedSuccess: "删除成功",
      deletedWorldSuccess: "平行世界已成功删除",
      deletedRoomSuccess: "房间已成功删除",
      deletedApiSuccess: "API 提供方已成功删除",
      deletedPersonaSuccess: "数字人物已成功删除",
      needNameUrl: "名称与 Base URL 为必填。",
      selectSpeaker: "选择发言人",
      recallNone: "本回合尚未召回记忆。",
      bindNone: "选择一位参与者查看冻结绑定。",
      affect: "情绪", needs: "动机与需求", rels: "关系",
      aliases: "别名", mode: "运行模式", compiled: "已编译",
      close: "关闭",
      topicPrefix: "议题", turnPrefix: "回合", speaking: "正在发言", userHost: "主持注入",
      bannerSelect: "Director 选定发言人", bannerRecall: "召回闸门：检索记忆与证据",
      bannerGen: "正在生成回复", bannerDone: "本回合完成",
      bannerHost: "主持人正在开场…", bannerWaiting: "正在等待模型回复…",
      modelSilent: "模型没有返回内容，请检查 API 提供方、模型名或密钥。",
      wsLost: "实时连接已断开，正在重连…",
      creating: "创建中", initializing: "初始化中", ready: "就绪", discussing: "讨论中", paused: "已暂停", completed: "已完成", error: "错误",
      protocols: "协议", models: "模型", version: "版本", noBinary: "无二进制路径", env: "环境变量",
      // Parallel World
      worldsEyebrow: "Counterfactual Simulation · Multi-Branch",
      worldsTitle: "平行世界，因果演化",
      worldsLead: "自适应时钟、时间知识防火墙、因果事件链与多分支推演评估。",
      newWorld: "创建平行世界", allWorlds: "全部平行世界", backWorlds: "返回平行世界",
      deleteWorld: "删除世界", deleteWorldConfirm: "确定要彻底删除该平行世界及其所有分支、时间线与快照吗？此操作无法撤销。",
      createWorldTitle: "设定平行世界",
      createWorldLead: "输入自然语言历史分歧设定，系统将自动构建 WorldSeed 种子（不提前预设结局）。",
      worldDescLabel: "世界分歧描述", worldBaselineLabel: "基准世界", worldStartDateLabel: "起始时间 (T0)",
      worldEndDateLabel: "推演终点", currentBranch: "分支", stepClock: "推进时钟 (Step)", forkBranch: "派生分支 (Fork)",
      injectAction: "注入动作", triggerScene: "场景会话", evalOutcomes: "情景评估", causalTimeline: "因果时间线 (Causal DAG Timeline)",
      inspProjects: "战略项目", inspOrgs: "组织与资本", inspTech: "技术与市场", inspActors: "世界 Actor",
      injectActionTitle: "注入 Actor 战略动作", actionActorLabel: "行动主体 (Actor)", actionTypeLabel: "动作类型",
      actionDescLabel: "动作描述", actionTargetLabel: "目标/标的", actionAmountLabel: "资金规模 ($B)",
      sceneTitle: "触发 Actor 面对面场景会话",
      sceneLead: "跨 Actor 对话将复用 Tavern Multi-Agent Room 运行，会后自动回写记忆与好感度。",
      sceneActorsLabel: "参与 Actor (选择 2 个或以上)", sceneTopicLabel: "会谈议题", startScene: "进入场景讨论",
      evalTitle: "多分支情景评估 (Scenario Distribution)",
      evalHelp: "定义评估问题并在多个独立分支中进行多维加权推演打分（不产生虚假真实概率）。",
      evalQuestionLabel: "推演评估命题", evalBranchesLabel: "模拟分支采样数 (N)", runEval: "运行多分支评估",
      clearConversation: "清空对话", clearConversationTitle: "清除全部对话历史",
      clearConversationConfirm: "确定要清空当前房间的全部对话内容吗？此操作不可撤销，人格状态与记忆不会丢失。",
      clearConversationDone: "对话已清空",
      liveWorking: "正在工作", liveDone: "已完成",
      expandTopic: "展开完整议题", collapseTopic: "收起议题",
      expandReview: "展开评审内容",
      synthTitle: "本轮会诊总结", synthSummary: "一句话结论",
      synthPositions: "各方核心观点", synthSingle: "专家判断",
      synthConsensus: "共同结论", synthConflicts: "主要分歧",
      synthJudgment: "主持裁定", synthNext: "下一步",
      skipToContent: "跳到内容", liveRoomTitle: "房间", liveWorldTitle: "世界",
      narrStudioEyebrow: "叙事创作工作台", actorEngineTitle: "Actor 补全引擎",
      labelAgentProvider: "智能体 / 提供方", labelAgent: "智能体", labelModel: "模型", labelReasoning: "思考强度",
      labelResearchPolicy: "研究策略", labelProvider: "提供方",
      btnInjectEvent: "注入事件", btnCausalGraph: "因果图", btnWorldState: "世界状态", btnDebug: "调试",
      btnEvalBranches: "分支评估", btnForkBranch: "创建分支",
      optCompiled: "已编译", optCompletedGaps: "已完成（有缺口）", optResearching: "研究中", optReady: "就绪", optDraft: "草稿", optError: "错误", optArchived: "已归档",
      optOpenAICompatible: "OpenAI 兼容", optCustomEndpoint: "自定义端点", optApiProvider: "API 提供方", optDeterministic: "确定性 / 离线",
      snapshotState: "快照状态", genMode: "生成模式",
      causalTitle: "因果图谱溯源 (Causal DAG Query)", causalLead: "输入任意目标节点或终局结果，逆向提取从根因分歧到最终局面的完整因果链条。",
      query: "回溯推导", replayTitle: "世界历史轨迹回放 (World Historical Scrub)",
      replayLead: "沿时间轴双向拖拽回放，审查历史关键分水岭与状态演进。",
      reportTitle: "平行世界推演分析报告 (Simulation Report)", copy: "复制报告",
      verifiedRecall: "已通过召回闸门验证后执行。",
      worldNoEvents: "暂无事件。请推进时间或注入 Actor 动作。", worldNoProjects: "暂无战略项目。",
      cashReserves: "资金储备", sampledBranches: "已采样 {n} 个反事实分支",
      noCausalChain: "未找到目标查询的因果链路径。", noReplaySnapshots: "暂无可用的回放快照。",
      baselineStable: "基准世界稳定推进一个时间步。",
      toastTurnCancelled: "已取消当前回合", toastBranchForked: "分支已派生", toastActionApplied: "动作已生效",
      toastSceneRunning: "正在 Tavern 房间中运行场景对话…", toastSceneCompleted: "场景已完成",
      toastEvaluating: "正在跨分支评估…", branchNamePrompt: "输入新分支名称：", sceneActorsNeed: "请至少选择 2 个 Actor 参与场景对话。",
      toastReportGenerating: "正在生成综合模拟报告…", toastReportCopied: "报告已复制到剪贴板",
      slotRoomRole: "房间角色", slotAuthority: "权威度",
      slotSpecialties: "专长（逗号分隔，留空自动推断）",
      slotToolPerms: "工具权限（逗号分隔，留空使用模板配置）",
      biblePremise: "故事前提", bibleQuestion: "核心问题", bibleTheme: "主题",
      narrOption: "方向", narrSelectedDir: "已选方向", narrEvalSummary: "叙事评估", narrHorizon: "推演集数",
      knowledgeDelta: "信息变化", narrShotsPrompts: "{s} 个分镜 · {v} 条视频提示",
      auditBlocking: "阻断", auditWarning: "警告", narrStageOverride: "覆盖默认",
      stageStoryArchitect: "故事架构师", stageOutlineWriter: "大纲编剧", stageForecastSimulator: "推演模拟器",
      stageSceneActor: "场景演员", stageScreenwriter: "编剧", stageReviewer: "审读", stageProductionPlanner: "制作规划",
      stageDirector: "叙事导演",
      directorOpen: "导演 Agent", directorTitle: "导演 Agent",
      directorModeDiscuss: "讨论", directorModeAdvise: "建议", directorModeAgent: "代理",
      directorInputPh: "用自然语言描述修改要求，例如：这个秘密暴露太早了，删掉。",
      directorSend: "发送", directorStop: "停止",
      directorEmptyHint: "向导演描述你的修改意图；它会读取项目状态并通过正式操作执行，不会替你提交正史。",
      directorRunning: "正在执行…", directorWaitingUser: "等待你的输入",
      directorWaitingCanon: "已到达人工审核点：连续性审核通过，Director 已停止自动执行。请查看稿件，并在第 6 步手动提交正史。",
      directorNeedsGuidance: "需要人工介入：自动修复未能清除 BLOCKING 问题。", directorFailed: "执行失败，可重试或继续对话。",
      directorPaused: "已暂停", directorCancelled: "已取消", directorCompleted: "已完成", directorActive: "就绪",
      directorConfirmPending: "高影响操作待确认：回复「确认」执行，或「取消」放弃。",
      directorRuntimeTitle: "导演模型（CLI 工具 / 模型 / 思考强度）", directorRuntimeSource: "运行来源",
      directorSaveRuntime: "保存为作品导演模型", directorRuntimeSaved: "导演模型已保存，后续回合生效",
      roleHeadWriter: "总编剧", roleCharacterEditor: "人物编辑", roleMysteryEditor: "悬疑编辑",
      roleContinuityEditor: "连续性编辑", roleCommercialEditor: "商业编辑",
      coverageGatePassed: "覆盖闸门通过；等待低增益确认",
      stageShootingAgent: "拍摄 Agent",
      shootWaitingCanon: "已到达人工审核点：拍摄 Agent 已停止自动执行，等待你的确认。",
      shootOpen: "拍摄 Agent", shootTitle: "拍摄 Agent",
      shootModeDiscuss: "讨论", shootModeAdvise: "建议", shootModeAgent: "代理",
      shootInputPh: "用自然语言描述拍摄方案调整，例如：拆分 Clip 7 为两个视频。",
      shootSend: "发送", shootStop: "停止",
      shootEmptyHint: "向拍摄 Agent 描述拆分 / 合并 / Prompt 修改意图；它只操作视频生成方案与参考素材，不会改动正史。",
      shootRunning: "正在执行…", shootWaitingUser: "等待你的输入",
      shootNeedsGuidance: "需要人工介入：自动处理未能完成。", shootFailed: "执行失败，可重试或继续对话。",
      shootPaused: "已暂停", shootCancelled: "已取消", shootCompleted: "已完成", shootActive: "就绪",
      shootRuntimeTitle: "拍摄模型（CLI 工具 / 模型 / 思考强度）", shootRuntimeSource: "运行来源",
      shootSaveRuntime: "保存为作品拍摄模型", shootRuntimeSaved: "拍摄模型已保存，后续回合生效",
      shootTargetModelTitle: "目标视频模型", shootTargetModelHelp: "目标视频模型 = 最终 Prompt 交给哪个视频生成模型。",
      shootAgentRuntimeHelp: "拍摄 Agent = 谁负责思考。",
      prodLayerMaster: "制作母版", prodLayerPlan: "视频生成方案",
      prodLayerMasterHelp: "按集展开剧本、分镜与音轨。CANON 表示由已提交正史生成。",
      prodLayerPlanHelp: "按目标视频模型编译可直接提交的 Clip Prompt。",
      prodCanonBadge: "CANON ✓", prodPreviewBadge: "PREVIEW · NON-CANON",
      prodPreviewHelp: "预览包：基于未提交草稿生成，不是正史版本。",
      prodStaleBanner: "STALE：正史已更新，此制作包已过期，请重新生成。",
      prodVideoPromptAdvanced: "高级 → 基础运动描述",
      prodVideoPromptDisclaimer: "这是模型无关的运动/画面意图，不是可直接提交给特定视频模型的最终Prompt。",
      prodPlanEmpty: "还没有方案。点击「＋ 创建视频生成方案」开始。",
      prodCreatePlan: "＋ 创建视频生成方案", prodPlanCancel: "收起表单",
      prodExportZip: "导出压缩包", prodExportZipHelp: "一键打包导出该集全部 Clip 的 Prompt 清单（zip）",
      prodTargetModel: "目标视频模型",
      prodGenMode: "生成方式", prodAspect: "画面比例", prodQuality: "质量策略",
      prodQualityQuality: "质量优先", prodQualityBalanced: "均衡", prodQualityFast: "成本优先",
      prodAudio: "声音策略", prodAudioAuto: "根据模型能力自动",
      prodAudioHintYes: "该模型已声明支持音频", prodAudioHintNo: "该模型已声明不支持音频",
      prodAudioHintUnknown: "音频能力未知，将按 auto 处理",
      prodContinuity: "连续性", prodContinuityAuto: "自动推荐",
      prodContinuityFirstLast: "首尾帧一致（若支持）", prodContinuityReference: "参考图约束（若支持）",
      prodPromptLang: "Prompt 语言", prodLangAuto: "自动",
      prodPlanSubmit: "生成 Clip Plan",
      prodCompilePrompts: "生成完整 Prompt",
      prodOneShotSubmit: "一键生成完整 Prompt（跳过审阅）",
      prodClipPlanReviewHint: "请审阅下方 Clip 计划（可拆分/合并/调整），确认后生成完整 Prompt。",
      ppStageQueued: "排队中...", ppStagePlanningClips: "正在规划 Clips...",
      ppStagePlanningAssets: "正在规划参考素材...", ppStageCompilingPrompts: "正在编译 Prompt...",
      ppStageValidating: "正在校验...",
      prodProfileUpdateBanner: "Profile已更新：当前Package基于旧Profile",
      prodStalePlanBanner: "方案已过期：制作母版或正史已更新。",
      prodRecompile: "重新编译",
      prodStatusClipPlanned: "Clip Plan 已生成", prodStatusCompiling: "编译中",
      prodStatusReady: "就绪", prodStatusFailed: "失败",
      prodProfileVersionLabel: "Profile 版本",
      clipSourceShots: "来源 Shots", clipPurpose: "用途", clipGenMode: "生成模式",
      clipRefs: "参考素材", clipRefsNone: "○ 尚未准备", clipContinuity: "连续性",
      clipDuration: "时长", clipEditDuration: "修改时长", clipEditMode: "切换生成模式",
      clipSplit: "拆分 Clip", clipMerge: "合并相邻 Clip",
      clipPrompt: "Prompt", clipAudioPrompt: "Audio Prompt", clipNegativePrompt: "Negative Prompt",
      clipRecommended: "推荐设置", clipCopyPrompt: "复制 Prompt", clipCopyAll: "复制全部",
      clipRecompile: "重新编译", clipAskAgent: "让拍摄Agent修改",
      toastCopied: "已复制到剪贴板", toastCopyFailed: "复制失败", toastClipSaved: "Clip 已更新",
      toastPlanReady: "视频生成方案已就绪", toastSelectTargetModel: "请先选择目标视频模型",
      prodJobRunningHint: "任务已开始，正在执行中...",
      toastNeedAssetName: "请填写素材名称", toastNeedAssetUri: "请填写素材来源 URI（不会伪造未准备的文件）",
      modeAuto: "自动推荐", modeTextToVideo: "文生视频", modeImageToVideo: "图生视频",
      modeFirstFrame: "首帧图", modeFirstLastFrame: "首尾帧", modeReferenceConditioned: "参考图",
      assetsTitle: "参考素材", assetBound: "✓ 已绑定", assetUnbound: "○ 尚未准备",
      assetAdd: "登记素材", assetName: "名称", assetType: "类型", assetUri: "来源 URI",
      assetTypeCharacter: "角色参考", assetTypeLocation: "场景参考", assetTypeProp: "道具参考",
      assetTypeStart: "首帧", assetTypeEnd: "尾帧", assetTypeStyle: "风格参考", assetTypeOther: "其他",
      toastAssetCreated: "素材已登记",
      shootSplitInstruction: "拆分 Clip {n} 为两个视频",
      shootMergeInstruction: "合并 Clip {a} 和 Clip {b}",
      shootRecompileInstruction: "重新编译 Clip {n} 的 Prompt：保持画面意图与连续性约束不变，优化镜头表达。",
      shootClipModifyInstruction: "请修改 Clip {n}（当前时长 {d} 秒，生成模式：{m}，用途：{p}）。修改要求：",
      prodLayerGuide: "完整制作手册",
      prodLayerGuideHelp: "把视频生成方案编译成可执行拍摄手册：素材图片 Prompt + 每条完整视频 Prompt，逐条复制即可开工。",
      prodGuideCreate: "生成完整制作手册", prodGuideRegenerate: "重新生成手册",
      prodGuideCreating: "正在生成手册...", prodGuideReady: "手册已就绪",
      prodGuideOpen: "打开完整手册", prodGuideCopyAll: "复制完整手册",
      prodGuideExportMd: "导出 Markdown",
      prodGuideCopyAssetPrompt: "复制图片Prompt", prodGuideCopyClipPrompt: "复制视频Prompt",
      prodGuideCopyHint: "每条 Prompt 独立复制",
      prodGuideAssetsNeeded: "需要准备",
      prodGuideAssetChar: "人物参考", prodGuideAssetLoc: "场景参考", prodGuideAssetProp: "道具参考",
      prodGuideAssetOther: "其他参考",
      prodGuideClips: "视频片段", prodGuideTotalDuration: "预计总时长",
      prodGuideStaleBanner: "手册已过期（需重新生成）",
      prodGuideClose: "关闭",
      prodGuideEmpty: "还没有手册。完成视频生成方案后，可一键生成完整制作手册。",
      prodGuideStageLoadingSource: "正在读取制作源...",
      prodGuideStageAnalyzingAssets: "正在分析参考素材...",
      prodGuideStageCompilingAssetPrompts: "正在编译素材图片 Prompt...",
      prodGuideStageCompilingClipPrompts: "正在编译完整视频 Prompt...",
      prodGuideStageRenderingGuide: "正在渲染制作手册..."
    },
    en: {
      navRooms: "Rooms", navWorlds: "Parallel World", navNarrative: "Narrative", navAgents: "Agents", navApi: "API", navPersonas: "Personas",
      narrEyebrow: "Narrative Studio · Short-drama workbench", narrTitle: "Narrative Studio",
      narrLead: "Set up the story, generate the bible and outline, then write, audit, and commit each episode.",
      narrNew: "New Project", narrRefresh: "Refresh", narrCreate: "Create", narrCharSubmit: "Add",
      narrSelectAll: "Select all", narrDeleteSelected: "Delete selected",
      narrNewHelp: "Fill the full project setup first. Default: micro-drama, 60 episodes, 90–120 seconds.",
      narrTabSettings: "Project Settings", narrTabBible: "Story Bible", narrTabCast: "Characters",
      narrTabOutline: "Series Outline", narrTabEpisode: "Episode Workbench", narrTabContinuity: "Continuity",
      narrTabProduction: "Production",
      narrBible: "Story Bible", narrCast: "Characters", narrEpisodes: "Episodes",
      narrKnowledge: "Information Gap", narrClues: "Clues & Foreshadowing", narrProduction: "Production",
      narrBibleGen: "Regenerate with AI", narrBibleEdit: "Edit", narrBibleSave: "Save new version",
      narrBibleVersions: "Version history", narrCharAdd: "Add character", narrCharCreate: "Create lightweight synthetic Persona",
      narrOutline: "Generate / regenerate", narrDraft: "Generate draft", narrAudit: "Audit", narrCommit: "Commit canon",
      narrEpisodesTitle: "Episode Workbench", narrNoProjects: "No projects yet — create one to start.",
      narrBack: "Back to projects", narrRuntimeOpen: "⚙ Creative model", narrRuntimeTitle: "Creative model",
      narrRuntimeHelp: "Set one default model. Agent failure reports an error; it never silently falls back to rules.",
      narrRuntimeDefault: "Default creative model", narrRuntimeOverrides: "Advanced: per-stage override",
      narrRuntimeOverrideHelp: "Each stage inherits the default model. Open override to pick a different Agent / Model / Reasoning.",
      narrGenModeHelp: "If the AI Agent fails, the error is shown. Rules mode is never used silently.",
      narrAdvancedMode: "Show technical details (runtime / world / branch / JSON)",
      narrSettingsHelp: "Format, episode count, and duration feed later generation stages.",
      narrDangerTitle: "Danger zone", narrDangerHelp: "Deleting a project cannot be undone.", narrDelete: "Delete project",
      narrBibleHelp: "Author constraints. Author-only secrets stay collapsed and never enter character knowledge.",
      narrCastHelp: "Bind complete Personas to core roles. Extra story characters can stay unbound.",
      narrCastAdvanced: "Advanced",
      narrLightPersonaHelp: "Lightweight synthetic Personas are for quick secondary-character rehearsal. This does not run full Persona Creation. Bind a complete Persona for core roles.",
      narrOutlineHelp: "Review goals, hooks, and cliffhangers. Click an episode to open the workbench.",
      narrContinuityHelp: "Information gaps, clues, arcs, and canon status. Technical IDs stay hidden by default.",
      narrProductionHelp: "Expand screenplay, shot list, image and video prompts per episode.",
      narrWriterAdvanced: "Advanced tool · Writer Room",
      narrWriterHelp: "Story characters are not used as writers. Configure Writer Personas first.",
      narrArcs: "Character arcs / Plot threads", narrCanon: "Canon status",
      narrFlowSettings: "Project settings", narrFlowBible: "Story bible", narrFlowCast: "Bind core cast",
      narrFlowOutline: "Series outline", narrFlowEpisode: "Episode workbench",
      narrFlowAudit: "Audit and commit", narrFlowProd: "Production package",
      localFirst: "Local-first runtime",
      roomsEyebrow: "Multi-Agent Room · Local-first",
      roomsTitle: "Rooms, directed locally",
      roomsLead: "Bind compiled personas to local agent hosts. The director advances one turn at a time.",
      newRoom: "New Room", filterAll: "All", filterActive: "Active", filterPaused: "Paused", filterStopped: "Stopped",
      backRooms: "Back to Rooms", lobbyTitle: "Room setup",
      lobbyLead: "Persona identity is decoupled from runtime, model, and reasoning effort. Bindings freeze at start.",
      randomize: "Randomize", preview: "Resolve preview", startRoom: "Start Room",
      roomSettings: "Room settings", labelTitle: "Title", labelTopic: "Topic", labelDirector: "Director mode",
      slots: "Participant slots", addSlot: "Add slot", previewTitle: "Binding snapshot",
      previewHelp: "Inspect the engine combination that will freeze at launch.",
      nextTurn: "Next Turn", speak: "Speak", pause: "Pause", resume: "Resume", resumeReady: "Reopen as Ready", cancelTurn: "Cancel Turn", stop: "Stop Room",
      pauseRoom: "Pause this room?", stopRoom: "Stop this room and its autonomous discussion?",
      emptyAgents: "No matching agents", emptyAgentsHint: "Rescan local hosts or change the filter.",
      inject: "Send", injectPh: "Inject a host question into the room…",
      inspState: "Persona state", inspRelation: "Relationships", inspRecall: "Recall evidence", inspBind: "Engine binding",
      selectParticipant: "Select a participant to inspect its runtime state.",
      stateBaseline: "Baseline", stateConfidence: "Confidence", stateUpdated: "Updated", stateTrigger: "Trigger",
      viewAll: "View all", collapseAll: "Collapse",
      noState: "No runtime state recorded for this persona yet.", noRelation: "No relationship state recorded for this persona yet.",
      stateLoading: "Reading runtime state…", stateError: "Failed to read runtime state.",
      stateDelta: "State change this turn", relationOf: "Counterpart",
      agentsEyebrow: "Discovery", agentsTitle: "Local agent discovery",
      agentsLead: "Probe CLI tools, app servers, desktop hosts, and protocols. No cloud keys are stored here.",
      rescan: "Rescan", ready: "Ready", authNeeded: "Auth required", statusDetected: "Detected, unverified", statusBroken: "Broken", statusDisabled: "Not installed",
      apiEyebrow: "OpenAI-compatible", apiTitle: "API providers",
      apiLead: "CredentialManager encrypts every provider key and decrypts it only for a request.",
      addApi: "Add provider", apiName: "Name", apiUrl: "Base URL", apiEnv: "API Key",
      apiModel: "Default model (optional)", cancel: "Cancel", save: "Save", create: "Build & Initialize", execute: "Execute Action",
      personaEyebrow: "Agent / Profile Library", personaTitle: "Agent / Profile Library",
      personaLead: "Manage Persona, Organization, Institution and Collective decision profiles while preserving the formal evidence and compilation pipeline.",
      searchPersona: "Search profiles",
      footerNote: "Local-first · Real protocols and counterfactual causal simulation",
      emptyRooms: "No rooms yet", emptyRoomsHint: "Create a room to start a multi-persona discussion.",
      emptyWorlds: "No parallel worlds yet", emptyWorldsHint: "Define counterfactual divergence conditions to begin.",
      emptyApi: "No providers configured", emptyApiHint: "Add local Ollama or another compatible endpoint.",
      emptyPersona: "No matching personas",
      openRoom: "Open", openWorld: "Enter Simulation", delete: "Delete", turns: "Turns",
      participant: "Slot", remove: "Remove", persona: "Persona", host: "Host", model: "Model", effort: "Reasoning",
      bestReady: "Best ready agent", randomAgent: "Random agent", defModel: "Agent default", randomModel: "Random compatible model",
      dirDirector: "Director (heuristics)", dirNatural: "Natural flow", dirRR: "Round robin", dirManual: "Manual select",
      titleNeed: "A room title is required.", slotNeed: "A room needs at least one slot.",
      resolving: "Resolving bindings…", frozen: "Frozen at start",
      deleteRoom: "Delete this room and its transcript?", deleteApi: "Delete this API profile?", deletePersona: "Delete this persona?",
      confirm: "Confirm", test: "Probe",
      scanning: "Rescanning local hosts…", scanned: "Scan complete",
      saved: "Saved", tested: "Probe complete",
      deletedSuccess: "Deleted successfully",
      deletedWorldSuccess: "Parallel world deleted successfully",
      deletedRoomSuccess: "Room deleted successfully",
      deletedApiSuccess: "API provider deleted successfully",
      deletedPersonaSuccess: "Persona deleted successfully",
      needNameUrl: "Name and Base URL are required.",
      selectSpeaker: "Choose speaker",
      recallNone: "No memories retrieved on this turn.",
      bindNone: "Select a participant to inspect the frozen binding.",
      affect: "Affect", needs: "Motivation & needs", rels: "Relationships",
      aliases: "Aliases", mode: "Run mode", compiled: "Compiled",
      close: "Close",
      topicPrefix: "Topic", turnPrefix: "Turn", speaking: "Speaking", userHost: "Host inject",
      bannerSelect: "Director selected speaker", bannerRecall: "Recall gate: searching memories and evidence",
      bannerGen: "Generating reply", bannerDone: "Turn complete",
      bannerHost: "Host is opening the discussion…", bannerWaiting: "Waiting for the model…",
      modelSilent: "The model returned no content. Check the API provider, model id, or key.",
      wsLost: "Live connection lost, reconnecting…",
      creating: "Creating", initializing: "Initializing", ready: "Ready", discussing: "Discussing", paused: "Paused", completed: "Completed", error: "Error",
      protocols: "Protocols", models: "Models", version: "Version", noBinary: "No binary path", env: "Env var",
      // Parallel World
      worldsEyebrow: "Counterfactual Simulation · Multi-Branch",
      worldsTitle: "Parallel Worlds & Causal Evolution",
      worldsLead: "Adaptive world clock, temporal knowledge firewall, DAG causal timeline, and scenario evaluations.",
      newWorld: "Create World", allWorlds: "All Parallel Worlds", backWorlds: "Back to Worlds",
      deleteWorld: "Delete World", deleteWorldConfirm: "Are you sure you want to permanently delete this parallel world, all branches, and history? This cannot be undone.",
      createWorldTitle: "Configure Parallel World",
      createWorldLead: "Enter natural language divergence conditions. System builds a structured WorldSeed without outcome predictions.",
      worldDescLabel: "Divergence Description", worldBaselineLabel: "Baseline World", worldStartDateLabel: "Start Date (T0)",
      worldEndDateLabel: "Simulation End", currentBranch: "Branch", stepClock: "Step Clock", forkBranch: "Fork Branch",
      injectAction: "Inject Action", triggerScene: "Scene Dialogue", evalOutcomes: "Evaluate Scenarios", causalTimeline: "Causal DAG Timeline",
      inspProjects: "Strategic Projects", inspOrgs: "Organizations & Capital", inspTech: "Tech & Markets", inspActors: "World Actors",
      injectActionTitle: "Inject Actor Strategic Action", actionActorLabel: "Actor", actionTypeLabel: "Action Type",
      actionDescLabel: "Description", actionTargetLabel: "Target", actionAmountLabel: "Capital Scale ($B)",
      sceneTitle: "Trigger Multi-Actor Scene Dialogue",
      sceneLead: "Direct actor conversations reuse the Tavern Multi-Agent Room, writing back memories and relationships.",
      sceneActorsLabel: "Participating Actors (select 2+)", sceneTopicLabel: "Meeting Topic", startScene: "Launch Scene",
      evalTitle: "Scenario Distribution Evaluation",
      evalHelp: "Defines evaluation questions and runs weighted scoring across independent branches (no fake real-world probabilities).",
      evalQuestionLabel: "Evaluation Question", evalBranchesLabel: "Branch Sample Count (N)", runEval: "Evaluate Across Branches",
      clearConversation: "Clear chat", clearConversationTitle: "Clear entire conversation history",
      clearConversationConfirm: "Clear every message in this room? This cannot be undone; persona state and memories will be kept.",
      clearConversationDone: "Conversation cleared",
      liveWorking: "Working", liveDone: "Done",
      expandTopic: "Show full topic", collapseTopic: "Collapse topic",
      expandReview: "Expand review",
      synthTitle: "Consultation Summary", synthSummary: "One-line conclusion",
      synthPositions: "Key positions", synthSingle: "Expert judgment",
      synthConsensus: "Shared conclusions", synthConflicts: "Key disagreements",
      synthJudgment: "Host ruling", synthNext: "Next steps",
      skipToContent: "Skip to content", liveRoomTitle: "Room", liveWorldTitle: "World",
      narrStudioEyebrow: "Narrative Studio", actorEngineTitle: "Actor Completion Engine",
      labelAgentProvider: "Agent / Provider", labelAgent: "Agent", labelModel: "Model", labelReasoning: "Reasoning",
      labelResearchPolicy: "Research Policy", labelProvider: "Provider",
      btnInjectEvent: "Inject event", btnCausalGraph: "Causal graph", btnWorldState: "World state", btnDebug: "Debug",
      btnEvalBranches: "Evaluate branches", btnForkBranch: "Create branch",
      optCompiled: "Compiled", optCompletedGaps: "Completed with gaps", optResearching: "Researching", optReady: "Ready", optDraft: "Draft", optError: "Error", optArchived: "Archived",
      optOpenAICompatible: "OpenAI Compatible", optCustomEndpoint: "Custom Endpoint", optApiProvider: "API Provider", optDeterministic: "Deterministic / Offline",
      snapshotState: "Snapshot State", genMode: "Generation Mode",
      causalTitle: "Causal DAG Query", causalLead: "Trace the full causal chain from root divergence to any target node or outcome.",
      query: "Trace", replayTitle: "World Historical Scrub",
      replayLead: "Scrub the timeline backwards and forwards to review key watersheds and state evolution.",
      reportTitle: "Simulation Report", copy: "Copy report",
      verifiedRecall: "Verified through Recall Gate before turn execution.",
      worldNoEvents: "No events yet. Step time or inject an actor action.", worldNoProjects: "No active strategic projects.",
      cashReserves: "Cash Reserves", sampledBranches: "Sampled {n} Counterfactual Branches",
      noCausalChain: "No causal chain paths found for target query.", noReplaySnapshots: "No replay snapshots available yet.",
      baselineStable: "Baseline stable timestep.",
      toastTurnCancelled: "Turn cancelled", toastBranchForked: "Branch forked", toastActionApplied: "Action applied",
      toastSceneRunning: "Running scene dialogue in Tavern room...", toastSceneCompleted: "Scene completed",
      toastEvaluating: "Evaluating across multiple branches...", branchNamePrompt: "Enter new branch name:", sceneActorsNeed: "Please select at least 2 actors for the dialogue scene.",
      toastReportGenerating: "Generating comprehensive simulation report...", toastReportCopied: "Report copied to clipboard",
      slotRoomRole: "Room Role", slotAuthority: "Authority",
      slotSpecialties: "Specialties (comma separated; inferred if empty)",
      slotToolPerms: "Tool Permissions (comma separated; template default if empty)",
      biblePremise: "Premise", bibleQuestion: "Core Question", bibleTheme: "Theme",
      narrOption: "Option", narrSelectedDir: "selected", narrEvalSummary: "Narrative Evaluation", narrHorizon: "Horizon",
      knowledgeDelta: "Knowledge changes", narrShotsPrompts: "{s} shots · {v} video prompts",
      auditBlocking: "BLOCKING", auditWarning: "WARNING", narrStageOverride: "override default",
      stageStoryArchitect: "Story Architect", stageOutlineWriter: "Outline Writer", stageForecastSimulator: "Forecast Simulator",
      stageSceneActor: "Scene Actor", stageScreenwriter: "Screenwriter", stageReviewer: "Reviewer", stageProductionPlanner: "Production Planner",
      stageDirector: "Director",
      directorOpen: "Director Agent", directorTitle: "Director Agent",
      directorModeDiscuss: "Discuss", directorModeAdvise: "Advise", directorModeAgent: "Agent",
      directorInputPh: "Describe the change in natural language, e.g. this secret is revealed too early, cut it.",
      directorSend: "Send", directorStop: "Stop",
      directorEmptyHint: "Describe your intent; the Director reads real project state and acts through formal actions. It never commits canon for you.",
      directorRunning: "Working…", directorWaitingUser: "Waiting for your input",
      directorWaitingCanon: "Human review point reached: audit passed and the Director stopped. Review the draft and commit canon manually at step 6.",
      directorNeedsGuidance: "Needs human guidance: automatic repair could not clear BLOCKING findings.", directorFailed: "Failed; retry or continue the conversation.",
      directorPaused: "Paused", directorCancelled: "Cancelled", directorCompleted: "Completed", directorActive: "Ready",
      directorConfirmPending: "High-impact action pending: reply “confirm” to run it, or “cancel” to drop it.",
      directorRuntimeTitle: "Director runtime (CLI tool / model / reasoning)", directorRuntimeSource: "Runtime source",
      directorSaveRuntime: "Save as project Director model", directorRuntimeSaved: "Director runtime saved; applies to following turns",
      roleHeadWriter: "Head Writer", roleCharacterEditor: "Character Editor", roleMysteryEditor: "Mystery Editor",
      roleContinuityEditor: "Continuity Editor", roleCommercialEditor: "Commercial Editor",
      coverageGatePassed: "Coverage gate passed; waiting for low-gain confirmation",
      stageShootingAgent: "Shooting Agent",
      shootWaitingCanon: "Reached a human review point: the Shooting Agent stopped auto execution and awaits your confirmation.",
      shootOpen: "Shooting Agent", shootTitle: "Shooting Agent",
      shootModeDiscuss: "Discuss", shootModeAdvise: "Advise", shootModeAgent: "Agent",
      shootInputPh: "Describe the shooting-plan change, e.g. split clip 7 into two videos.",
      shootSend: "Send", shootStop: "Stop",
      shootEmptyHint: "Describe split / merge / prompt edits; the Shooting Agent only touches video generation plans and reference assets, never canon.",
      shootRunning: "Working…", shootWaitingUser: "Waiting for your input",
      shootNeedsGuidance: "Needs human guidance: automatic handling did not finish.", shootFailed: "Failed; retry or continue the conversation.",
      shootPaused: "Paused", shootCancelled: "Cancelled", shootCompleted: "Completed", shootActive: "Ready",
      shootRuntimeTitle: "Shooting runtime (CLI tool / model / reasoning)", shootRuntimeSource: "Runtime source",
      shootSaveRuntime: "Save as project Shooting model", shootRuntimeSaved: "Shooting runtime saved; applies to following turns",
      shootTargetModelTitle: "Target video model", shootTargetModelHelp: "Target video model = which video model receives the final prompt.",
      shootAgentRuntimeHelp: "Shooting Agent = who does the thinking.",
      prodLayerMaster: "Production master", prodLayerPlan: "Video generation plan",
      prodLayerMasterHelp: "Screenplay, shots, and tracks per episode. CANON means derived from committed canon.",
      prodLayerPlanHelp: "Compile submission-ready clip prompts per target video model.",
      prodCanonBadge: "CANON ✓", prodPreviewBadge: "PREVIEW · NON-CANON",
      prodPreviewHelp: "Preview package: built from an uncommitted draft, not canon.",
      prodStaleBanner: "STALE: canon has moved on; regenerate this package.",
      prodVideoPromptAdvanced: "Advanced → Base motion description",
      prodVideoPromptDisclaimer: "This is model-agnostic motion/visual intent, not a final prompt ready to submit to a specific video model.",
      prodPlanEmpty: "No plan yet. Click “＋ Create video generation plan” to start.",
      prodCreatePlan: "＋ Create video generation plan", prodPlanCancel: "Collapse form",
      prodExportZip: "Export clips (.zip)", prodExportZipHelp: "Download all clips of this episode as a zip of prompt sheets",
      prodTargetModel: "Target video model",
      prodGenMode: "Generation mode", prodAspect: "Aspect ratio", prodQuality: "Quality priority",
      prodQualityQuality: "Quality first", prodQualityBalanced: "Balanced", prodQualityFast: "Cost first",
      prodAudio: "Audio strategy", prodAudioAuto: "Auto by model capability",
      prodAudioHintYes: "Model declares audio support", prodAudioHintNo: "Model declares no audio support",
      prodAudioHintUnknown: "Audio capability unknown; treated as auto",
      prodContinuity: "Continuity", prodContinuityAuto: "Auto",
      prodContinuityFirstLast: "First–last frame consistency (if supported)", prodContinuityReference: "Reference-conditioned (if supported)",
      prodPromptLang: "Prompt language", prodLangAuto: "Auto",
      prodPlanSubmit: "Generate Clip Plan",
      prodCompilePrompts: "Generate full prompts",
      prodOneShotSubmit: "One-shot full prompts (skip review)",
      prodClipPlanReviewHint: "Review the clip plan below (split/merge/adjust), then generate full prompts.",
      ppStageQueued: "Queued...", ppStagePlanningClips: "Planning clips...",
      ppStagePlanningAssets: "Planning reference assets...", ppStageCompilingPrompts: "Compiling prompts...",
      ppStageValidating: "Validating...",
      prodProfileUpdateBanner: "Profile updated: this package was built from an older profile",
      prodStalePlanBanner: "Stale plan: the production master or canon has changed.",
      prodRecompile: "Recompile",
      prodStatusClipPlanned: "Clip plan ready", prodStatusCompiling: "Compiling",
      prodStatusReady: "Ready", prodStatusFailed: "Failed",
      prodProfileVersionLabel: "Profile version",
      clipSourceShots: "Source shots", clipPurpose: "Purpose", clipGenMode: "Generation mode",
      clipRefs: "Reference assets", clipRefsNone: "○ Not prepared", clipContinuity: "Continuity",
      clipDuration: "Duration", clipEditDuration: "Change duration", clipEditMode: "Switch mode",
      clipSplit: "Split clip", clipMerge: "Merge with previous",
      clipPrompt: "Prompt", clipAudioPrompt: "Audio Prompt", clipNegativePrompt: "Negative Prompt",
      clipRecommended: "Recommended settings", clipCopyPrompt: "Copy prompt", clipCopyAll: "Copy all",
      clipRecompile: "Recompile", clipAskAgent: "Ask Shooting Agent",
      toastCopied: "Copied to clipboard", toastCopyFailed: "Copy failed", toastClipSaved: "Clip updated",
      toastPlanReady: "Video generation plan is ready", toastSelectTargetModel: "Select a target video model first",
      prodJobRunningHint: "Task is running, processing...",
      toastNeedAssetName: "Asset name is required", toastNeedAssetUri: "Asset source URI is required (never fake unprepared files)",
      modeAuto: "Auto", modeTextToVideo: "Text to video", modeImageToVideo: "Image to video",
      modeFirstFrame: "First frame", modeFirstLastFrame: "First & last frame", modeReferenceConditioned: "Reference conditioned",
      assetsTitle: "Reference assets", assetBound: "✓ Bound", assetUnbound: "○ Not prepared",
      assetAdd: "Register asset", assetName: "Name", assetType: "Type", assetUri: "Source URI",
      assetTypeCharacter: "Character reference", assetTypeLocation: "Location reference", assetTypeProp: "Prop reference",
      assetTypeStart: "Start frame", assetTypeEnd: "End frame", assetTypeStyle: "Style reference", assetTypeOther: "Other",
      toastAssetCreated: "Asset registered",
      shootSplitInstruction: "Split clip {n} into two videos",
      shootMergeInstruction: "Merge clip {a} and clip {b}",
      shootRecompileInstruction: "Recompile the prompt of clip {n}: keep visual intent and continuity constraints, improve the wording.",
      shootClipModifyInstruction: "Please modify clip {n} (duration {d}s, mode {m}, purpose {p}). Request: ",
      prodLayerGuide: "Full production guide",
      prodLayerGuideHelp: "Compile the video generation plan into an executable handbook: one image prompt per asset plus one full video prompt per clip, ready to copy one by one.",
      prodGuideCreate: "Generate full guide", prodGuideRegenerate: "Regenerate guide",
      prodGuideCreating: "Generating guide...", prodGuideReady: "Guide is ready",
      prodGuideOpen: "Open full guide", prodGuideCopyAll: "Copy full guide",
      prodGuideExportMd: "Export Markdown",
      prodGuideCopyAssetPrompt: "Copy image prompt", prodGuideCopyClipPrompt: "Copy video prompt",
      prodGuideCopyHint: "Copy each prompt independently",
      prodGuideAssetsNeeded: "Prepare",
      prodGuideAssetChar: "character refs", prodGuideAssetLoc: "location refs", prodGuideAssetProp: "prop refs",
      prodGuideAssetOther: "other refs",
      prodGuideClips: "Clips", prodGuideTotalDuration: "Estimated total duration",
      prodGuideStaleBanner: "Guide is stale (regenerate it)",
      prodGuideClose: "Close",
      prodGuideEmpty: "No guide yet. Once the video generation plan is ready, generate the full production guide in one click.",
      prodGuideStageLoadingSource: "Loading production source...",
      prodGuideStageAnalyzingAssets: "Analyzing reference assets...",
      prodGuideStageCompilingAssetPrompts: "Compiling asset image prompts...",
      prodGuideStageCompilingClipPrompts: "Compiling full video prompts...",
      prodGuideStageRenderingGuide: "Rendering production guide..."
    }
  };

  // ─── State Store ─────────────────────────────────────────────────────────
  const state = {
    lang: localStorage.getItem("pc-ui-lang") || "zh-CN",
    view: "rooms",
    roomSub: "list",
    worldSub: "list",
    roomFilter: "all",
    agentFilter: "all",
    personaQ: "",
    personas: [],
    profiles: [],
    profileType: "",
    profileStatus: "",
    profileSort: "updated_at",
    agents: [],
    agentsLoading: false,
    apiProfiles: [],
    apiEditingId: null,
    rooms: [],
    roomProtocols: [],
    roomTemplates: [],
    currentRoom: null,
    currentRoomWs: null,
    lobbySlots: [],
    inspectParticipantId: null,
    activeSpeakingSlotId: null,
    isTurnBusy: false,
    liveWatchToken: 0,
    // Parallel World
    worlds: [],
    currentWorld: null,
    currentBranchId: null,
    currentWorldState: null,
    currentWorldEvents: [],
    currentWorldActors: [],
    wsReconnectTimer: null,
    wsReconnectAttempts: 0,
    wsGeneration: 0,
    wsRoomId: null,
    wsHeartbeatTimer: null,
    personaCreationJob: null,
    personaCreationPoll: null,
    personaCreationExistingId: null,
    profileEnrichmentJob: null,
    backgroundJobs: [],
    backgroundJobPolls: new Map(),
    backgroundJobCounts: {},
    backgroundJobFilter: "all",
    backgroundTerminalPage: 1,
    backgroundTerminalPageSize: 20,
    // Real persona runtime state.  Keyed `personaId::branchId`; read-only
    // renders reuse it so re-painting the inspector never hits the network.
    runtimeCache: new Map(),
    runtimeInflight: new Map(),
    runtimeRefreshTimer: null,
    runtimePending: false,
    // Live topic clamp: expanded flag plus the last topic key so a topic
    // change resets the fold.
    topicKey: "",
    topicExpanded: false,
    topicOverflow: false,
    narrProjects: [],
    narrSelectedIds: [],
    narrReturnToCast: null,
    narrProject: null,
    narrBible: null,
    narrCast: [],
    narrEpisodes: [],
    narrKnowledge: null,
    narrClues: [],
    narrProduction: [],
    narrForecasts: [],
    narrForecast: null,
    narrDirections: [],
    narrCanon: [],
    narrThreads: [],
    narrArcs: [],
    narrTab: "settings",
    narrEpNumber: 1,
    narrJob: null,
    narrJobPoll: null,
    narrBibleEditing: false,
    narrAdvanced: localStorage.getItem("pc-narr-advanced") === "1",
    director: { sessionId: null, poll: null, mode: "agent", seen: new Set(), loading: false },
    shooting: { sessionId: null, poll: null, mode: "agent", seen: new Set(), loading: false },
    narrVideoProfiles: [],
    narrVideoProfilesLoaded: false,
    narrPromptPlans: {},
    narrPlanForms: {},
    narrPlanJobs: {},
    narrProductionAssets: [],
    narrGuides: {},
    narrGuideJobs: {},
    narrGuideDrawerPkg: null,
  };

  // ─── DOM Helpers ─────────────────────────────────────────────────────────
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const t = (k) => (I[state.lang] && I[state.lang][k]) || I.en[k] || k;
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const nowTime = () => new Date().toLocaleTimeString(state.lang === "zh-CN" ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit" });
  function onClick(sel, handler) {
    const el = $(sel);
    if (!el) return;
    el.addEventListener("click", handler);
  }

  // ─── Selectable Runtime Helpers ──────────────────────────────────────────
  function getSelectableLocalAgents() {
    return state.agents.filter(agent =>
      agent.status === "ready" &&
      agent.runtime_source === "local_cli"
    );
  }

  function getSelectableApiAgents() {
    return state.agents.filter(agent =>
      agent.status === "ready" &&
      agent.runtime_source === "api"
    );
  }

  function getSelectableAgentsForSource(source) {
    return source === "api" ? getSelectableApiAgents() : getSelectableLocalAgents();
  }

  // Shared runtime selector contract used by Room, Parallel World and
  // Persona Creation.  It only exposes discovery data reported by the
  // selected READY agent; reasoning values are never invented in the UI.
  function getRuntimeModels(agent) {
    return (agent && Array.isArray(agent.models))
      ? agent.models.filter(model => model && model.selectable !== false)
      : [];
  }

  function getReasoningCapability(model) {
    const raw = model && model.reasoning_capability;
    if (!raw || typeof raw !== "object") {
      return { mode: "unknown", supported_efforts: [], default_effort: null, verified: false };
    }
    const mode = String(raw.mode || "unknown").toLowerCase();
    return {
      mode,
      supported_efforts: Array.isArray(raw.supported_efforts) ? raw.supported_efforts.filter(Boolean) : [],
      default_effort: raw.default_effort || null,
      verified: raw.verified === true,
      binding_strategy: raw.binding_strategy || "unknown",
      verification_error: raw.verification_error || ""
    };
  }

  function getRuntimeReasoningOptions(agent, model) {
    const capability = getReasoningCapability(model);
    if (capability.mode === "native_effort" && capability.verified) {
      return capability.supported_efforts;
    }
    if (capability.mode === "manual_config") {
      return capability.supported_efforts;
    }
    if (capability.mode === "default_only") return ["default"];
    return [];
  }

  function reasoningCapabilityLabel(model) {
    const capability = getReasoningCapability(model);
    if (capability.mode === "native_effort") {
      return capability.verified
        ? `Reasoning：原生能力已验证 · ${capability.supported_efforts.join(", ") || "default"}`
        : `Reasoning：原生能力未验证 · ${capability.supported_efforts.join(", ") || "未报告"}`;
    }
    if (capability.mode === "manual_config") {
      return `Reasoning：手动配置 · ${capability.supported_efforts.join(", ") || "未配置"}（未验证）`;
    }
    if (capability.mode === "provider_specific") {
      return "Reasoning：Provider-specific binding 尚未验证";
    }
    if (capability.mode === "default_only") return "Reasoning：仅使用 Provider default";
    if (capability.mode === "unsupported") return "Reasoning：当前 Runtime 未绑定，不支持显式调节";
    return "Reasoning：尚未检测到能力，保持默认配置";
  }

  function reasoningOptionLabel(value) {
    return value === "default" ? "Provider default" : value;
  }

  function runtimeModelOptionLabel(agent, model) {
    const name = (model && (model.display_name || model.id)) || "未知模型";
    const capability = getReasoningCapability(model);
    if (!getRuntimeReasoningOptions(agent, model).length) return name;
    return capability.mode === "manual_config"
      ? `${name} · Reasoning（手动）`
      : `${name} · Reasoning ✓`;
  }

  function runtimeReasoningNotice(agent, model, source) {
    const selectedLabel = reasoningCapabilityLabel(model);
    if (source !== "api" || getReasoningCapability(model).mode !== "unknown") {
      return selectedLabel;
    }
    const models = getRuntimeModels(agent);
    const reportedCount = models.filter(item => getRuntimeReasoningOptions(agent, item).length).length;
    if (!reportedCount) return selectedLabel;
    return `${selectedLabel}；同一 Provider 有 ${reportedCount}/${models.length} 个模型已报告档位（模型名带 Reasoning ✓）`;
  }

  function setReasoningCapabilityNotice(element, agent, model, source) {
    if (!element) return;
    const capability = getReasoningCapability(model);
    element.textContent = runtimeReasoningNotice(agent, model, source);
    if (capability.mode !== "unknown" || source !== "api") return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm btn-text";
    button.style.marginLeft = "6px";
    button.textContent = "配置能力";
    button.title = "在 API Provider 表单中保存已确认的 Reasoning 能力";
    button.addEventListener("click", () => {
      const dialog = $("#dlg-api");
      const modelInput = $("#api-model");
      if (modelInput && model && model.id) modelInput.value = model.id;
      if (dialog) dialog.showModal();
    });
    element.appendChild(button);
  }

  function sharedRuntimeSelection(source, agentId, modelId) {
    const agents = getSelectableAgentsForSource(source);
    const agent = agents.find(item => item.id === agentId) || agents[0] || null;
    const models = getRuntimeModels(agent);
    const model = models.find(item => item.id === modelId) || models[0] || null;
    return { agents, agent, models, model, reasoning: getRuntimeReasoningOptions(agent, model) };
  }

  function runtimeSourceOf(cfg) {
    const value = cfg && (cfg.runtime_source || cfg.source);
    return value === "api" ? "api" : "local_cli";
  }

  function fillSharedRuntimeSelects({ source, agentSelect, modelSelect, reasoningSelect, noticeEl, saved }) {
    if (!agentSelect || !modelSelect || !reasoningSelect) return;
    const agents = getSelectableAgentsForSource(source);
    const readyLabel = source === "api" ? "Connected" : "READY";
    const savedCfg = saved || {};
    if (!agents.length) {
      const scanning = state.agentsLoading;
      agentSelect.innerHTML = `<option value="" selected>${scanning ? "正在扫描 Runtime…" : "没有 READY / Connected Runtime"}</option>`;
      modelSelect.innerHTML = `<option value="default" selected>${scanning ? "扫描结束后可选模型" : "Agent 默认模型"}</option>`;
      reasoningSelect.innerHTML = `<option value="none" selected>${scanning ? "扫描结束后可选 Reasoning" : "Provider default"}</option>`;
      reasoningSelect.disabled = true;
      if (noticeEl) noticeEl.textContent = scanning ? "正在扫描可用 Runtime…" : "当前来源没有可用 Runtime。";
      return;
    }
    const savedAgentId = savedCfg.agent_id || savedCfg.agent || "";
    const agent = agents.find(item => item.id === savedAgentId) || agents[0];
    agentSelect.innerHTML = agents.map(item =>
      `<option value="${esc(item.id)}" ${item.id === agent.id ? "selected" : ""}>${esc(item.name || item.id)} [${readyLabel}]</option>`
    ).join("");
    const models = getRuntimeModels(agent);
    const savedModelId = savedCfg.model_id || savedCfg.model || "";
    const hasExplicitDefault = agent && agent.capabilities && (agent.capabilities.model_selection === "unsupported" || agent.capabilities.agent_default_model);
    const model = models.find(item => item.id === savedModelId) || models[0] || null;
    if (!models.length || hasExplicitDefault && !models.length) {
      modelSelect.innerHTML = `<option value="default" selected>Agent 默认模型</option>`;
    } else {
      modelSelect.innerHTML = models.map(item =>
        `<option value="${esc(item.id)}" ${model && item.id === model.id ? "selected" : ""}>${esc(runtimeModelOptionLabel(agent, item))}</option>`
      ).join("");
    }
    const selectedModel = models.find(item => item.id === modelSelect.value) || model;
    const efforts = getRuntimeReasoningOptions(agent, selectedModel);
    const capability = getReasoningCapability(selectedModel);
    const savedReasoning = savedCfg.reasoning_effort || savedCfg.reasoning || "";
    if (capability.mode === "unsupported") {
      reasoningSelect.innerHTML = `<option value="none" selected>当前 Runtime 不支持单独设置 Reasoning</option>`;
      reasoningSelect.disabled = true;
    } else if (capability.mode === "default_only" || (efforts.length === 1 && efforts[0] === "default")) {
      reasoningSelect.disabled = false;
      reasoningSelect.innerHTML = `<option value="default" selected>Provider default</option>`;
    } else if (!efforts.length) {
      reasoningSelect.innerHTML = `<option value="none" selected>Provider default</option>`;
      reasoningSelect.disabled = true;
    } else {
      const selectedEffort = efforts.includes(savedReasoning)
        ? savedReasoning
        : ((selectedModel && selectedModel.default_reasoning_effort) || capability.default_effort || efforts[0]);
      reasoningSelect.disabled = false;
      reasoningSelect.innerHTML = efforts.map(effort =>
        `<option value="${esc(effort)}" ${effort === selectedEffort ? "selected" : ""}>${esc(reasoningOptionLabel(effort))}</option>`
      ).join("");
    }
    if (noticeEl) noticeEl.textContent = runtimeReasoningNotice(agent, selectedModel, source);
  }

  function bindSharedRuntimeSelector(prefix, saved) {
    const agentSelect = $(`#${prefix}-agent`);
    const modelSelect = $(`#${prefix}-model`);
    const reasoningSelect = $(`#${prefix}-reasoning`);
    const noticeEl = $(`#${prefix}-reasoning-notice`);
    const radios = $$(`input[name='${prefix}-source']`);
    const cfg = saved || {};
    const preferredSource = runtimeSourceOf(cfg);
    const currentSource = () => (($(`input[name='${prefix}-source']:checked`) || {}).value || preferredSource || "local_cli");
    const apply = (keepSaved) => fillSharedRuntimeSelects({
      source: currentSource(),
      agentSelect,
      modelSelect,
      reasoningSelect,
      noticeEl,
      saved: keepSaved ? {
        runtime_source: currentSource(),
        agent_id: agentSelect && agentSelect.value,
        model_id: modelSelect && modelSelect.value,
        reasoning_effort: reasoningSelect && reasoningSelect.value,
      } : cfg,
    });
    radios.forEach(radio => {
      radio.checked = radio.value === preferredSource;
      radio.onchange = () => fillSharedRuntimeSelects({
        source: radio.value, agentSelect, modelSelect, reasoningSelect, noticeEl, saved: {},
      });
    });
    fillSharedRuntimeSelects({
      source: preferredSource,
      agentSelect, modelSelect, reasoningSelect, noticeEl, saved: cfg,
    });
    if (agentSelect) agentSelect.onchange = () => apply(true);
    if (modelSelect) modelSelect.onchange = () => apply(true);
  }

  function readSharedRuntime(prefix) {
    const source = (($(`input[name='${prefix}-source']:checked`) || {}).value || "local_cli");
    const agent = (($(`#${prefix}-agent`) || {}).value || "").trim();
    const model = (($(`#${prefix}-model`) || {}).value || "").trim();
    const reasoning = (($(`#${prefix}-reasoning`) || {}).value || "").trim();
    if (!agent) return null;
    return {
      runtime_source: source === "api" ? "api" : "local_cli",
      agent_id: agent,
      model_id: model || "default",
      reasoning_effort: reasoning || "none",
    };
  }

  function liveRuntimeDraft(prefix) {
    const source = (($(`input[name='${prefix}-source']:checked`) || {}).value || "").trim();
    if (!source) return null;
    return {
      runtime_source: source,
      agent_id: (($(`#${prefix}-agent`) || {}).value || "").trim(),
      model_id: (($(`#${prefix}-model`) || {}).value || "").trim(),
      reasoning_effort: (($(`#${prefix}-reasoning`) || {}).value || "").trim(),
    };
  }

  function mergeRuntimeDraft(saved, live) {
    const baseline = saved || {};
    if (!live) return baseline;
    if (live.runtime_source && live.runtime_source !== runtimeSourceOf(baseline) && !live.agent_id) {
      return { runtime_source: live.runtime_source };
    }
    return {
      runtime_source: live.runtime_source || runtimeSourceOf(baseline),
      agent_id: live.agent_id || baseline.agent_id || baseline.agent || "",
      model_id: live.model_id || baseline.model_id || baseline.model || "",
      reasoning_effort: live.reasoning_effort || baseline.reasoning_effort || baseline.reasoning || "",
    };
  }

  const toast = (msg) => {
    const el = $("#toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("is-on");
    setTimeout(() => el.classList.remove("is-on"), 2400);
  };

  async function api(url, opts = {}) {
    try {
      const res = await fetch(url, opts);
      const ct = res.headers.get("content-type") || "";
      if (ct.includes("application/json")) {
        const data = await res.json();
        return data;
      }
      const txt = await res.text();
      return { ok: res.ok, error: txt || `HTTP_${res.status}`, status: res.status };
    } catch (err) {
      console.warn(`API fetch error for ${url}:`, err);
      return { ok: false, error: String(err) };
    }
  }

  // ─── Language & Navigation ───────────────────────────────────────────────
  function applyLang() {
    document.documentElement.lang = state.lang;
    localStorage.setItem("pc-ui-lang", state.lang);
    $$("[data-i]").forEach((el) => { el.textContent = t(el.getAttribute("data-i")); });
    $$("[data-i-placeholder]").forEach((el) => { el.placeholder = t(el.getAttribute("data-i-placeholder")); });
    $$(".lang-switch button").forEach((b) => b.classList.toggle("is-active", b.dataset.lang === state.lang));

    const dir = $("#lobby-director");
    if (dir) {
      const v = dir.value;
      dir.innerHTML = `
        <option value="">— ${state.lang === "zh-CN" ? "请选择 Director 模式" : "Select Director mode"} —</option>
        <option value="director">${t("dirDirector")}</option>
        <option value="natural">${t("dirNatural")}</option>
        <option value="round_robin">${t("dirRR")}</option>
        <option value="manual">${t("dirManual")}</option>
      `;
      dir.value = v;
    }
    renderAll();
  }

  function showView(name) {
    state.view = name;
    $$(".view").forEach((v) => v.classList.toggle("is-on", v.id === "view-" + name));
    $$(".nav-links button").forEach((b) => b.classList.toggle("is-active", b.dataset.nav === name));
    if (name === "rooms" && state.roomSub !== "live") showRoomSub("list");
    if (name === "worlds" && state.worldSub !== "live") showWorldSub("list");
    if (name === "narrative" && !state.narrProject) loadNarrativeProjects();
  }

  function showRoomSub(sub) {
    state.roomSub = sub;
    ["list", "lobby", "live"].forEach((s) => {
      const el = $("#sub-room-" + s);
      if (el) el.hidden = s !== sub;
    });
    if (sub === "live") requestAnimationFrame(syncTopicClamp);
  }

  function showWorldSub(sub) {
    state.worldSub = sub;
    ["list", "create", "live"].forEach((s) => {
      const el = $("#sub-world-" + s);
      if (el) el.hidden = s !== sub;
    });
  }

  function statusLabel(s) {
    const legacy = { active: "ready", lobby: "creating", stopped: "completed", waiting_for_speaker: "ready" };
    const key = legacy[s] || s;
    return t(key) || key || "";
  }

  // ─── Context capability formatting ────────────────────────────────────────
  // An unresolved context window is Unknown, never the historical 32K
  // fallback.  The planning fallback may only be shown when it is explicitly
  // labelled as a planning policy.
  function formatTokens(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return "—";
    if (n >= 1_048_576 && n % 1_048_576 === 0) return `${n / 1_048_576}M`;
    if (n >= 1024) return `${Math.round(n / 1024)}K`;
    return String(n);
  }

  function formatContextTag(capabilities) {
    const caps = capabilities || {};
    if (caps.effective_context_window == null) return "Context: Unknown ⚠";
    return `Context: ${formatTokens(caps.effective_context_window)} ✓`;
  }

  function formatContextDetail(capabilities) {
    const caps = capabilities || {};
    const lines = [
      `Native: ${formatTokens(caps.native_context_window)}`,
      `Requested: ${caps.requested_context_window == null ? "Auto" : formatTokens(caps.requested_context_window)}`,
      `Adapter limit: ${formatTokens(caps.adapter_context_limit)}`,
      `Effective: ${caps.effective_context_window == null ? "Unknown ⚠" : formatTokens(caps.effective_context_window)}`,
      `Usable: ${caps.usable_context_budget == null ? "—" : formatTokens(caps.usable_context_budget)}`,
      `Preferred working: ${caps.preferred_working_context == null ? "—" : formatTokens(caps.preferred_working_context)}`,
      `Mode: ${caps.context_window_mode || "unknown"}`,
      `Source: ${caps.context_capability_source || caps.context_window_source || "unknown"}`,
      `Verified: ${caps.context_verified ? "yes" : "no"}`,
    ];
    if (caps.effective_context_window == null && caps.planning_context_window != null) {
      lines.push(`Planning fallback: ${formatTokens(caps.planning_context_window)} (fallback_policy)`);
    }
    return lines.join("\n");
  }

  // ─── 1. Multi-Agent Room Subsystem ───────────────────────────────────────
  async function loadRooms() {
    const res = await api("/api/rooms");
    if (res && res.ok) {
      state.rooms = res.data || [];
      renderRooms();
    }
  }

  function renderRooms() {
    const grid = $("#rooms-grid");
    if (!grid) return;
    const list = state.rooms.filter((r) => {
      if (state.roomFilter === "all") return true;
      if (state.roomFilter === "active") return ["ready", "discussing"].includes(r.status);
      if (state.roomFilter === "stopped") return r.status === "completed";
      return r.status === state.roomFilter;
    });
    const countEl = $("#room-count");
    if (countEl) countEl.textContent = String(list.length);

    if (!list.length) {
      grid.innerHTML = `
        <div class="empty" style="grid-column:1/-1">
          <h3>${t("emptyRooms")}</h3>
          <p>${t("emptyRoomsHint")}</p>
          <button class="btn btn-primary" id="empty-new-room">${t("newRoom")}</button>
        </div>`;
      const b = $("#empty-new-room");
      if (b) b.onclick = openRoomLobby;
      return;
    }

    grid.innerHTML = list.map((r) => `
      <article class="card interactive room-card" data-room-id="${r.id}">
        <div class="row-between" style="align-items:flex-start;">
          <h3>${esc(r.title || "Room " + r.id)}</h3>
          <span class="tag ${["ready", "discussing"].includes(r.status) ? "ok" : ""}">${esc(statusLabel(r.status))}</span>
        </div>
        <p class="kicker">${esc(r.topic || "General Discussion")}</p>
        <div class="row" style="flex-wrap:wrap;gap:6px;margin-bottom:16px;">
          ${(r.participants || []).map((p) => `<span class="tag">${esc(p.display_name || p.persona_id)}</span>`).join("")}
        </div>
        <div class="row-between">
          <span class="meta num">${t("turns")} ${r.turn_index || 0}</span>
          <div class="row" style="gap:6px;">
            <button class="btn btn-sm btn-secondary" data-open-room="${r.id}">${t("openRoom")}</button>
            <button class="btn btn-sm btn-ghost" data-del-room="${r.id}">${t("delete")}</button>
          </div>
        </div>
      </article>`).join("");

    grid.querySelectorAll("[data-open-room]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        openLiveRoom(el.getAttribute("data-open-room"));
      });
    });

    grid.querySelectorAll("[data-del-room]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const rid = el.getAttribute("data-del-room");
        if (!rid) return;
        confirmDlg(t("deleteRoom"), t("delete"), async () => {
          await api(`/api/rooms/${rid}`, { method: "DELETE" });
          state.rooms = state.rooms.filter((r) => r.id !== rid);
          renderRooms();
          toast(t("deletedRoomSuccess"));
        }, true);
      });
    });
  }

  function openRoomLobby() {
    $("#lobby-title").value = "";
    $("#lobby-topic").value = "";
    $("#lobby-template").value = "";
    $("#lobby-director").value = "";
    $("#lobby-mode").value = "";
    $("#lobby-speaker-selection").value = "";
    $("#lobby-description").value = "";
    $("#lobby-protocol").value = "";
    $("#room-shared-background").value = "";
    $("#room-shared-rules").value = "";
    $("#room-custom-instructions").value = "";
    const err = $("#title-error");
    if (err) err.hidden = true;
    const errBox = $("#room-init-error");
    if (errBox) errBox.style.display = "none";
    showRoomSub("lobby");
    applyLobbyMode(state.lobbyAdvanced ? "advanced" : "simple");
    state.lobbySlots = [];
    buildLobbyDefaults();
    renderProtocolSettings();
  }

  // ─── Lobby: simple vs advanced mode ─────────────────────────────────────
  // Simple mode hides every orchestration knob the Room Protocol already
  // decides for the user; advanced mode restores the full control surface.
  const ROLE_AUTHORITY = { host: 100, chair: 100, judge: 90, expert: 70, critic: 60, member: 50, observer: 0 };
  const ROLE_LABELS = {
    host: "主持人", chair: "主席", judge: "裁判", expert: "专家",
    critic: "评审", member: "成员", pro: "正方", con: "反方", observer: "观察员"
  };

  function authorityForRole(role) {
    return ROLE_AUTHORITY[role] != null ? ROLE_AUTHORITY[role] : 50;
  }

  const SUMMARY_SPECIALTY_PATTERNS = [
    [/(人工智能|机器学习|深度学习|\bAI\b|machine learning)/i, "人工智能"],
    [/(软件工程|编程|程序设计|计算机|software|programming|computer science)/i, "软件与计算"],
    [/(产品设计|用户体验|交互设计|product design|\bUX\b|\bUI\b)/i, "产品与设计"],
    [/(经济学|金融|投资|商业|economics|finance|investment|business)/i, "经济与商业"],
    [/(心理学|认知|行为科学|psychology|cognition|behavioral science)/i, "心理与认知"],
    [/(哲学|伦理|逻辑|philosophy|ethics|logic)/i, "哲学与伦理"],
    [/(历史|政治|国际关系|history|politics|geopolitics)/i, "历史与政治"],
    [/(医学|健康|生物|medicine|health|biology)/i, "医学与生命科学"],
    [/(法律|法学|合规|law|legal|compliance)/i, "法律与合规"],
    [/(写作|文学|叙事|媒体|writing|literature|narrative|media)/i, "写作与叙事"],
    [/(八字|命理|四柱|bazi)/i, "八字"],
    [/(紫微|ziwei)/i, "紫微斗数"],
    [/(易经|易卦|六爻|梅花易数|yijing|liuyao)/i, "易学"],
    [/(奇门|六壬|太乙|qimen|daliuren|taiyi)/i, "三式"],
    [/(占星|塔罗|astrology|tarot)/i, "西方术数"],
  ];

  function inferSpecialtiesFromSummary(persona) {
    const metadata = (persona && persona.metadata) || {};
    const text = [
      persona && persona.summary,
      persona && persona.description,
      persona && persona.system_prompt,
      metadata.summary,
      metadata.description,
      metadata.capability_summary,
    ].filter(Boolean).join("\n");
    if (!text) return [];
    return SUMMARY_SPECIALTY_PATTERNS
      .filter(([pattern]) => pattern.test(text))
      .map(([, label]) => label)
      .slice(0, 6);
  }

  function personaSpecialties(persona) {
    if (!persona) return [];
    const metadata = persona.metadata || {};
    for (const candidate of [
      persona.specialties,
      persona.expertise,
      metadata.specialties,
      metadata.expertise,
      metadata.capabilities,
    ]) {
      if (Array.isArray(candidate) && candidate.length) return candidate.slice();
    }
    return inferSpecialtiesFromSummary(persona);
  }

  function selectedProtocol() {
    const select = $("#lobby-protocol");
    return select ? select.value : "";
  }

  function applyLobbyMode(mode) {
    state.lobbyAdvanced = mode === "advanced";
    document.querySelectorAll("#lobby-mode-toggle [data-lobby-mode]").forEach(btn => {
      btn.classList.toggle("is-active", btn.dataset.lobbyMode === mode);
    });
    const protocol = selectedProtocol();
    document.querySelectorAll("#sub-room-lobby [data-advanced]").forEach(el => { el.hidden = !state.lobbyAdvanced; });
    document.querySelectorAll("#sub-room-lobby [data-fd-only]").forEach(el => {
      el.hidden = protocol !== "free_discussion" || (el.hasAttribute("data-advanced") && !state.lobbyAdvanced);
    });
    renderSlots();
    renderBindingPreview();
  }

  const protocolMeta = {
    free_discussion: { label: "自由讨论", help: "保留现有自由聊天；支持智能、轮询或手动发言。" },
    host_moderated: { label: "主持人模式", help: "主持人点名成员、推进讨论并生成最终答复。" },
    expert_consultation: { label: "专家会诊", help: "只路由相关专家，先独立分析，再交叉评审和综合。" },
    debate: { label: "辩论", help: "正反阵营按阶段陈述、质询和反驳，由裁判评审。" },
    committee: { label: "委员会", help: "委员独立意见、交叉评审和结构化投票，由主席裁决。" },
    custom: { label: "自定义", help: "高级声明式协议；当前版本通过底层 schema/API 配置。" }
  };

  const PROTOCOL_ROLES = {
    free_discussion: ["member", "host", "critic", "observer"],
    host_moderated: ["host", "member", "critic", "observer"],
    expert_consultation: ["host", "expert", "critic", "observer"],
    debate: ["host", "pro", "con", "judge", "observer"],
    committee: ["chair", "member", "expert", "critic", "observer"],
    custom: ["host", "chair", "expert", "member", "pro", "con", "judge", "critic", "observer"]
  };

  function roleOptions(protocol, current) {
    const roles = PROTOCOL_ROLES[protocol] || ["member"];
    return roles.map(role => `<option value="${role}" ${role === current ? "selected" : ""}>${role}</option>`).join("");
  }

  function defaultRole(protocol, index) {
    if (protocol === "expert_consultation") return index === 0 ? "host" : "expert";
    if (protocol === "host_moderated") return index === 0 ? "host" : "member";
    if (protocol === "debate") return ["host", "pro", "con", "judge"][index] || "observer";
    if (protocol === "committee") return index === 0 ? "chair" : "member";
    return "member";
  }

  function renderProtocolSettings() {
    const protocol = selectedProtocol();
    const help = $("#protocol-help");
    if (help) help.textContent = (protocolMeta[protocol] || {}).help || "";
    const box = $("#protocol-settings");
    if (!box) return;
    if (!protocol) {
      box.innerHTML = "";
      renderSlots();
      return;
    }
    const common = `<div class="field"><label>最大轮数</label><input class="input" id="protocol-max-rounds" type="number" min="1" max="100" value="6" /></div>`;
    if (protocol === "expert_consultation") {
      box.innerHTML = `${common}<div class="field"><label>路由方式</label><select class="input" id="protocol-routing"><option value="host_decides">主持人决定</option><option value="rule_based">规则匹配</option><option value="all">全部专家</option><option value="manual">手动</option></select></div><div class="field"><label>最少专家数</label><input class="input" id="protocol-min-experts" type="number" min="1" value="1" /></div><div class="field"><label>最多专家数</label><input class="input" id="protocol-max-experts" type="number" min="1" value="3" /></div><label><input id="protocol-independent" type="checkbox" checked /> 独立首轮</label><label><input id="protocol-review" type="checkbox" checked /> 交叉评审</label><div class="field"><label>最大评审轮数</label><input class="input" id="protocol-review-rounds" type="number" min="0" max="20" value="1" /></div>`;
    } else if (protocol === "committee") {
      box.innerHTML = `${common}<label><input id="protocol-voting" type="checkbox" checked /> 启用投票</label><label><input id="protocol-anonymous" type="checkbox" /> 匿名投票</label><div class="field"><label>计票方式</label><select class="input" id="protocol-vote-method"><option value="simple_majority">简单多数</option><option value="weighted">按 authority 加权</option></select></div><label><input id="protocol-abstain" type="checkbox" checked /> 允许弃权</label>`;
    } else if (protocol === "debate") {
      box.innerHTML = `${common}<label><input id="protocol-cross-exam" type="checkbox" checked /> 交叉质询</label>`;
    } else if (protocol === "free_discussion") {
      box.innerHTML = `${common}`;
    } else {
      box.innerHTML = common;
    }
    state.lobbySlots.forEach((slot, index) => {
      if (!slot.role || !roleOptions(protocol, slot.role).includes(`value="${slot.role}"`)) slot.role = defaultRole(protocol, index);
      slot.authority = authorityForRole(slot.role);
    });
    renderSlots();
  }

  async function loadRoomTemplates() {
    const [protocols, templates] = await Promise.all([api("/api/room-protocols"), api("/api/room-templates")]);
    state.roomProtocols = (protocols && protocols.ok && protocols.data) || [];
    state.roomTemplates = (templates && templates.ok && templates.data) || [];
    const select = $("#lobby-template");
    if (select) select.innerHTML = `<option value="">不使用模板</option>` + state.roomTemplates.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    return true;
  }

  function updateTemplateHint(templateId) {
    const hint = $("#template-hint");
    if (!hint) return;
    const template = state.roomTemplates.find(item => item.id === templateId);
    const base = "模板会自动配置协作流程与角色，只需为每个角色槽位选择人物。";
    if (!template) {
      hint.textContent = base;
      return;
    }
    const label = (protocolMeta[template.protocol] || {}).label || template.protocol || "";
    const risk = template.id === "room_template_divination_consultation"
      ? " 本模板及演示中的术数/算命演算仅供娱乐与传统文化研究，请勿过度迷信；不构成投资、医疗、法律或其他专业建议。"
      : "";
    hint.textContent = `协作模式：${label} · ${base}${risk}`;
  }

  function normalizePersonaLabel(value) {
    return String(value || "")
      .replace(/[（(]\s*主持\s*[）)]/g, "")
      .replace(/[-—_]主持$/g, "")
      .replace(/\s+/g, "")
      .trim();
  }

  function applyRoomTemplate(templateId) {
    const template = state.roomTemplates.find(item => item.id === templateId);
    updateTemplateHint(template ? templateId : "");
    if (!template) return;
    $("#lobby-title").value = template.name || "";
    if (template.topic) $("#lobby-topic").value = template.topic;
    if (template.protocol) $("#lobby-protocol").value = template.protocol;
    const shared = template.shared_context || {};
    $("#room-shared-background").value = shared.background || "";
    $("#room-shared-rules").value = (shared.rules || []).join("\n");
    $("#room-custom-instructions").value = shared.custom_instructions || "";
    $("#lobby-description").value = template.description || "";
    if (!Array.isArray(template.participants) || !template.participants.length) {
      renderProtocolSettings();
      renderSlots();
      renderBindingPreview();
      return;
    }
    // Templates define role slots (role/label/specialties/authority/tool
    // permissions), never a fixed persona binding: the user maps each slot
    // to a real persona.  Slot label lives in display_name.
    state.lobbySlots = template.participants.map((participant, index) => {
      const role = participant.role || defaultRole(template.protocol, index);
      const fallbackBinding = state.defaultBinding || {};
      const recommendedLabel = normalizePersonaLabel(participant.display_name);
      const matchedPersona = state.personas.find((persona) =>
        [persona.id, persona.display_name].some((value) => normalizePersonaLabel(value) === recommendedLabel)
      );
      return {
        slot_label: participant.display_name || participant.slot_label || ROLE_LABELS[role] || `席位 ${index + 1}`,
        persona_id: participant.persona_id || (matchedPersona ? matchedPersona.id : ""),
        role,
        specialties: (participant.specialties && participant.specialties.length)
          ? participant.specialties.slice()
          : personaSpecialties(state.personas.find(p => p.id === participant.persona_id)),
        authority: participant.authority != null ? participant.authority : authorityForRole(role),
        tool_permissions: (participant.tool_permissions || []).slice(),
        runtime_source: fallbackBinding.source || "local_cli",
        runtime_selection: fallbackBinding.agent || "",
        model_selection: fallbackBinding.model || "default",
        reasoning_selection: fallbackBinding.reasoning || "none"
      };
    });
    renderProtocolSettings();
    const config = template.protocol_config || {};
    const setValue = (id, value) => { const el = $(id); if (el && value != null) el.value = value; };
    const setChecked = (id, value) => { const el = $(id); if (el && value != null) el.checked = Boolean(value); };
    setValue("#protocol-max-rounds", config.max_rounds);
    setValue("#protocol-routing", config.routing_mode);
    setValue("#protocol-min-experts", config.min_experts);
    setValue("#protocol-max-experts", config.max_experts);
    setValue("#protocol-review-rounds", config.max_review_rounds);
    setChecked("#protocol-independent", config.independent_first);
    setChecked("#protocol-review", config.cross_review);
    applyLobbyMode(state.lobbyAdvanced ? "advanced" : "simple");
    renderBindingPreview();
  }

  async function buildLobbyDefaults() {
    const errBox = $("#room-init-error");
    const [personasLoaded] = await Promise.all([loadPersonas(), loadAgents(false)]);
    await loadRoomTemplates();

    const readyLocalAgents = getSelectableLocalAgents();
    const readyApiAgents = getSelectableApiAgents();
    const defaultAgents = readyLocalAgents.length ? readyLocalAgents : readyApiAgents;
    const defaultRuntime = defaultAgents[0];
    const defaultSource = readyLocalAgents.length ? "local_cli" : "api";
    const defaultAgent = defaultRuntime ? defaultRuntime.id : "";
    const defaultModel = (defaultRuntime && defaultRuntime.models && defaultRuntime.models[0]) ? defaultRuntime.models[0].id : "default";
    state.defaultBinding = { source: defaultSource, agent: defaultAgent, model: defaultModel, reasoning: "none" };

    if (!personasLoaded || !state.personas.length) {
      state.lobbySlots = [];
      if (errBox) {
        errBox.style.display = "block";
        const details = $("#room-error-details");
        if (details) details.textContent = "人物列表加载失败或当前没有可用人物。请先创建并编译人物，再启动房间。";
      }
      renderSlots();
      renderBindingPreview();
      return;
    }

    // A fresh Room is intentionally blank. Personas and runtime bindings are
    // only populated after the user chooses a template or adds a slot.
    state.lobbySlots = [];
    renderSlots();
    renderBindingPreview();
  }

  function slotSelects(slot) {
    const pOpts = state.personas.map((p) => `<option value="${p.id}" ${p.id === slot.persona_id ? "selected" : ""}>${esc(p.display_name || p.id)}</option>`).join("") || `<option value="" disabled selected>当前没有可用人物</option>`;
    const source = slot.runtime_source || "local_cli";
    const availableAgents = getSelectableAgentsForSource(source);

    let aOpts = "";
    if (!availableAgents.length) {
      const msg = state.agentsLoading
        ? "正在扫描 Runtime…"
        : (source === "local_cli" ? "当前没有已连接的本地 CLI Agent" : "当前没有已连接的 API Provider");
      aOpts = `<option value="" disabled selected>${msg}</option>`;
    } else {
      aOpts = availableAgents.map((a) => `<option value="${a.id}" ${a.id === slot.runtime_selection ? "selected" : ""}>${esc(a.name)} [READY]</option>`).join("");
    }

    const selectedAgent = availableAgents.find(a => a.id === slot.runtime_selection) || availableAgents[0];
    const models = (selectedAgent && selectedAgent.models) || [];
    const hasExplicitDefault = selectedAgent && selectedAgent.capabilities && (selectedAgent.capabilities.model_selection === "unsupported" || selectedAgent.capabilities.agent_default_model);

    let mOpts = "";
    if (!models.length) {
      if (hasExplicitDefault) {
        mOpts = `<option value="default" selected>Agent 默认模型</option>`;
      } else {
        mOpts = `<option value="" disabled selected>该 CLI 已连接，但未发现可用模型</option>`;
      }
    } else {
      mOpts = models.map((m) => `<option value="${m.id}" ${m.id === slot.model_selection ? "selected" : ""}>${esc(runtimeModelOptionLabel(selectedAgent, m))}</option>`).join("");
    }

    const selectedModel = models.find(m => m.id === slot.model_selection) || models[0];
    const supportedEfforts = getRuntimeReasoningOptions(selectedAgent, selectedModel);

    let rOpts = "";
    let rDisabled = false;
    if (supportedEfforts.length) {
      const capability = getReasoningCapability(selectedModel);
      const defaultEffort = supportedEfforts.includes(slot.reasoning_selection)
        ? slot.reasoning_selection
        : (capability.mode === "default_only" ? "default" : capability.default_effort || supportedEfforts[0]);
      rOpts = supportedEfforts.map(eff => `<option value="${esc(eff)}" ${eff === defaultEffort ? "selected" : ""}>${esc(reasoningOptionLabel(eff))}</option>`).join("");
    } else {
      rOpts = `<option value="none" selected disabled>未报告 Reasoning 能力</option>`;
      rDisabled = true;
    }

    const reasoningNotice = runtimeReasoningNotice(selectedAgent, selectedModel, source);
    return { pOpts, aOpts, mOpts, rOpts, rDisabled, source, selectedAgent, selectedModel, reasoningNotice };
  }

  function renderSlots() {
    const box = $("#slots");
    if (!box) return;
    const protocol = selectedProtocol();
    const advanced = !!state.lobbyAdvanced;
    box.innerHTML = state.lobbySlots.map((slot, idx) => {
      const s = slotSelects(slot);
      const role = slot.role || defaultRole(protocol, idx);
      const slotTitle = slot.slot_label || `${t("participant")} ${idx + 1}`;
      const personaOptions = (slot.persona_id && !state.personas.some(p => p.id === slot.persona_id)
        ? `<option value="${esc(slot.persona_id)}" selected>${esc(slot.persona_id)}（已失效）</option>`
        : "")
        + (slot.persona_id ? "" : `<option value="" disabled selected>— 选择人物 —</option>`)
        + state.personas.map((p) => `<option value="${p.id}" ${p.id === slot.persona_id ? "selected" : ""}>${esc(p.display_name || p.id)}</option>`).join("");
      const advancedFields = advanced ? `
            <div class="field"><label>${t("slotRoomRole")}</label><select class="input" data-k="role" data-idx="${idx}">${roleOptions(protocol, role)}</select></div>
            <div class="field"><label>${t("slotSpecialties")}</label><input class="input" data-k="specialties_text" data-idx="${idx}" value="${esc((slot.specialties || []).join(", "))}" /></div>
            <div class="field"><label>${t("slotAuthority")}</label><input class="input" data-k="authority" data-idx="${idx}" type="number" min="0" max="100" value="${Number(slot.authority ?? authorityForRole(role))}" /></div>
            <div class="field"><label>${t("slotToolPerms")}</label><input class="input" data-k="tool_permissions_text" data-idx="${idx}" value="${esc((slot.tool_permissions || []).join(", "))}" /></div>` : "";
      return `
        <div class="card slot-card" data-slot-idx="${idx}" style="margin-bottom:12px;">
          <div class="row-between" style="margin-bottom:12px;">
            <div class="row" style="gap:8px;align-items:center;">
              <strong>${esc(slotTitle)}</strong>
              <span class="tag">${esc(ROLE_LABELS[role] || role)}</span>
              ${slot.tool_permissions && slot.tool_permissions.length && !advanced ? `<span class="tag ok">工具权限：由模板自动配置</span>` : ""}
            </div>
            <button class="btn btn-sm btn-ghost" data-rm-slot="${idx}">${t("remove")}</button>
          </div>
          <div class="slot-grid">
            <div class="field"><label>${t("persona")}</label><select class="input" data-k="persona_id" data-idx="${idx}">${personaOptions}</select></div>
            ${advancedFields}
            <div class="field">
              <label>运行来源</label>
              <div class="row" style="gap:10px;margin-top:6px;">
                <label style="cursor:pointer;display:inline-flex;align-items:center;gap:4px;font-size:12px;">
                  <input type="radio" name="slot-src-${idx}" value="local_cli" ${s.source === "local_cli" ? "checked" : ""} data-src-idx="${idx}" /> 本地 CLI
                </label>
                <label style="cursor:pointer;display:inline-flex;align-items:center;gap:4px;font-size:12px;">
                  <input type="radio" name="slot-src-${idx}" value="api" ${s.source === "api" ? "checked" : ""} data-src-idx="${idx}" /> API 提供方
                </label>
              </div>
            </div>
            <div class="field"><label>${t("host")}</label><select class="input" data-k="runtime_selection" data-idx="${idx}">${s.aOpts}</select></div>
            <div class="field"><label>${t("model")}</label><select class="input" data-k="model_selection" data-idx="${idx}">${s.mOpts}</select></div>
            <div class="field"><label>${t("effort")}</label><select class="input" data-k="reasoning_selection" data-idx="${idx}" ${s.rDisabled ? "disabled" : ""}>${s.rOpts}</select><p class="meta" style="margin-top:6px;">${esc(s.reasoningNotice)}</p></div>
          </div>
        </div>`;
    }).join("");

    box.querySelectorAll("input[type='radio']").forEach((radio) => {
      radio.addEventListener("change", () => {
        const idx = +radio.dataset.srcIdx;
        const newSrc = radio.value;
        state.lobbySlots[idx].runtime_source = newSrc;
        const available = getSelectableAgentsForSource(newSrc);
        if (available.length) {
          state.lobbySlots[idx].runtime_selection = available[0].id;
          state.lobbySlots[idx].model_selection = (available[0].models && available[0].models.length) ? available[0].models[0].id : "default";
        } else {
          state.lobbySlots[idx].runtime_selection = "";
          state.lobbySlots[idx].model_selection = "default";
        }
        state.lobbySlots[idx].reasoning_selection = "none";
        renderSlots();
        renderBindingPreview();
      });
    });

    box.querySelectorAll("select, input[data-k]").forEach((sel) => {
      const idx = +sel.dataset.idx;
      if (!sel.dataset.k.endsWith("_text")) sel.value = state.lobbySlots[idx][sel.dataset.k] ?? sel.value;
      sel.addEventListener("change", () => {
        if (sel.dataset.k === "specialties_text") state.lobbySlots[idx].specialties = sel.value.split(",").map(x => x.trim()).filter(Boolean);
        else if (sel.dataset.k === "tool_permissions_text") state.lobbySlots[idx].tool_permissions = sel.value.split(",").map(x => x.trim()).filter(Boolean);
        else if (sel.dataset.k === "authority") state.lobbySlots[idx].authority = Number(sel.value || 50);
        else state.lobbySlots[idx][sel.dataset.k] = sel.value;
        if (sel.dataset.k === "persona_id") {
          // Specialties prefer template → persona metadata; never force the
          // user to retype them for every room.
          const slot = state.lobbySlots[idx];
          if (!(slot.specialties || []).length) {
            slot.specialties = personaSpecialties(state.personas.find(p => p.id === slot.persona_id));
            if (state.lobbyAdvanced) renderSlots();
          }
        } else if (sel.dataset.k === "role") {
          state.lobbySlots[idx].authority = authorityForRole(sel.value);
          renderSlots();
        }
        if (sel.dataset.k === "runtime_selection") {
          const source = state.lobbySlots[idx].runtime_source || "local_cli";
          const available = getSelectableAgentsForSource(source);
          const ag = available.find(a => a.id === sel.value);
          if (ag && ag.models && ag.models.length) {
            state.lobbySlots[idx].model_selection = ag.models[0].id;
          } else {
            state.lobbySlots[idx].model_selection = "default";
          }
          state.lobbySlots[idx].reasoning_selection = "none";
          renderSlots();
        } else if (sel.dataset.k === "model_selection") {
          renderSlots();
        }
        renderBindingPreview();
      });
    });

    box.querySelectorAll("[data-rm-slot]").forEach((b) => {
      b.addEventListener("click", () => {
        if (state.lobbySlots.length <= 1) {
          toast(t("slotNeed"));
          return;
        }
        state.lobbySlots.splice(+b.dataset.rmSlot, 1);
        renderSlots();
        renderBindingPreview();
      });
    });
  }

  function resolveLocalSlot(slot) {
    const source = slot.runtime_source || "local_cli";
    const availableAgents = getSelectableAgentsForSource(source);
    let agent = availableAgents.find(a => a.id === slot.runtime_selection) || availableAgents[0];
    if (!agent) {
      agent = { id: slot.runtime_selection || "none", name: slot.runtime_selection || "未连接", models: [{ id: "default", display_name: "default" }] };
    }
    const models = agent.models || [];
    const model = models.find(m => m.id === slot.model_selection) || models[0] || { id: "default", display_name: "default" };
    const p = state.personas.find(x => x.id === slot.persona_id) || { display_name: slot.persona_id, id: slot.persona_id };
    return {
      persona: p,
      agent: agent.name || agent.id,
      model: model.display_name || model.id,
      effort: slot.reasoning_selection || "none"
    };
  }

  function renderBindingPreview() {
    const box = $("#preview-box");
    if (!box) return;
    box.innerHTML = state.lobbySlots.map((slot) => {
      const r = resolveLocalSlot(slot);
      return `
        <div class="bind-row" style="margin-bottom:8px;padding:8px;background:var(--surface);border-radius:var(--radius-sm);">
          <div class="row-between"><strong>${esc(r.persona.display_name || r.persona.id)}</strong><span class="tag">${t("frozen")}</span></div>
          <p class="meta" style="margin-top:4px;">${esc(r.agent)} · ${esc(r.model)} · ${esc(r.effort)}</p>
        </div>`;
    }).join("");
  }

  function collectProtocolConfig() {
    const value = (id, fallback) => $(id) ? $(id).value : fallback;
    const checked = (id, fallback) => $(id) ? $(id).checked : fallback;
    return {
      max_rounds: Number(value("#protocol-max-rounds", 6)),
      speaker_selection: value("#lobby-speaker-selection", "intelligent"),
      routing_mode: value("#protocol-routing", "host_decides"),
      min_experts: Number(value("#protocol-min-experts", 1)),
      max_experts: Number(value("#protocol-max-experts", 3)),
      independent_first: checked("#protocol-independent", true),
      cross_review: checked("#protocol-review", true),
      max_review_rounds: Number(value("#protocol-review-rounds", 1)),
      voting_enabled: checked("#protocol-voting", true),
      anonymous_voting: checked("#protocol-anonymous", false),
      vote_method: value("#protocol-vote-method", "simple_majority"),
      allow_abstain: checked("#protocol-abstain", true),
      cross_examination: checked("#protocol-cross-exam", true),
      continue_on_member_failure: true
    };
  }

  async function startRoomFromLobby() {
    const title = $("#lobby-title").value.trim();
    const errBox = $("#room-init-error");
    const errDetails = $("#room-error-details");
    if (errBox) errBox.style.display = "none";

    if (!title) {
      const err = $("#title-error");
      if (err) {
        err.hidden = false;
        err.textContent = t("titleNeed");
      }
      return;
    }

    if (!state.lobbySlots.length) {
      if (errBox && errDetails) {
        errBox.style.display = "block";
        errDetails.textContent = t("slotNeed") || "至少保留一个席位。";
      }
      toast(t("slotNeed") || "至少保留一个席位。", "error");
      return;
    }

    // Explicit protocol: a selected template always wins over a drifted
    // select, and an empty selection must abort loudly instead of silently
    // creating a free_discussion room.
    const protocolSelect = $("#lobby-protocol");
    const templateId = $("#lobby-template").value || null;
    let protocol = (protocolSelect && protocolSelect.value) ? protocolSelect.value : "";
    if (templateId) {
      const selectedTemplate = state.roomTemplates.find(item => item.id === templateId);
      if (selectedTemplate && selectedTemplate.protocol && selectedTemplate.protocol !== protocol) {
        if (protocolSelect) protocolSelect.value = selectedTemplate.protocol;
        protocol = selectedTemplate.protocol;
        renderProtocolSettings();
      }
    }
    if (!protocol) {
      if (errBox && errDetails) {
        errBox.style.display = "block";
        errDetails.textContent = "请选择房间协作模式。";
      }
      toast("请选择房间协作模式。", "error");
      return;
    }
    if (protocol === "free_discussion" && (!$("#lobby-mode").value || !$("#lobby-speaker-selection").value)) {
      if (errBox && errDetails) {
        errBox.style.display = "block";
        errDetails.textContent = "自由讨论需要明确选择讨论方式与发言方式。";
      }
      toast("请选择讨论方式与发言方式。", "error");
      return;
    }
    const validRoles = PROTOCOL_ROLES[protocol] || [];

    // Validation: runtime exists, status == ready, source matches, model valid, reasoning valid
    for (let i = 0; i < state.lobbySlots.length; i++) {
      const s = state.lobbySlots[i];
      if (!s.persona_id) {
        if (errBox && errDetails) {
          errBox.style.display = "block";
          errDetails.textContent = `席位 ${i + 1} 尚未选择人物。模板角色槽位需要映射到一个真实人物后才能启动。`;
        }
        toast(`席位 ${i + 1} 尚未选择人物`, "error");
        return;
      }
      const persona = state.personas.find(p => p.id === s.persona_id);
      if (!persona) {
        if (errBox && errDetails) {
          errBox.style.display = "block";
          errDetails.textContent = `席位 ${i + 1} 所选人物 "${s.persona_id || '未选择'}" 不存在或已经被删除。请重新选择人物。`;
        }
        toast(`席位 ${i + 1} 人物无效`, "error");
        return;
      }
      const source = s.runtime_source || "local_cli";
      const available = getSelectableAgentsForSource(source);
      const agent = available.find(a => a.id === s.runtime_selection);
      if (!agent) {
        if (errBox && errDetails) {
          errBox.style.display = "block";
          errDetails.textContent = `席位 ${i + 1} 所选运行时 "${s.runtime_selection || '未选择'}" 不可用或未就绪。请选择已连接的 ${source === 'local_cli' ? '本地 CLI' : 'API Provider'}。`;
        }
        toast(`席位 ${i + 1} 运行时不可用`, "error");
        return;
      }
      if (agent.runtime_source !== source) {
        if (errBox && errDetails) {
          errBox.style.display = "block";
          errDetails.textContent = `席位 ${i + 1} 运行时类型与来源不匹配。`;
        }
        toast(`席位 ${i + 1} 运行时类型不匹配`, "error");
        return;
      }
      const models = agent.models || [];
      const hasExplicitDefault = agent.capabilities && (agent.capabilities.model_selection === "unsupported" || agent.capabilities.agent_default_model);
      if (!models.length && !hasExplicitDefault) {
        if (errBox && errDetails) {
          errBox.style.display = "block";
          errDetails.textContent = `席位 ${i + 1} 的 Agent "${agent.name}" 未发现可用模型。`;
        }
        toast(`席位 ${i + 1} Agent 未发现可用模型`, "error");
        return;
      }
      const validModel = models.some(m => m.id === s.model_selection) || (s.model_selection === "default" && hasExplicitDefault);
      if (!validModel && models.length > 0) {
        if (errBox && errDetails) {
          errBox.style.display = "block";
          errDetails.textContent = `席位 ${i + 1} 所选模型 "${s.model_selection}" 不在 Agent 可用模型列表中。`;
        }
        toast(`席位 ${i + 1} 模型无效`, "error");
        return;
      }
    }

    const startButton = $("#btn-start");
    const progress = $("#room-init-progress");
    startButton.disabled = true;
    startButton.textContent = "初始化房间...";
    if (progress) progress.textContent = "创建房间 (5%)...";
    const requestId = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : `room-request-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const payload = {
      title,
      description: $("#lobby-description").value.trim(),
      topic: $("#lobby-topic").value.trim(),
      template_id: templateId,
      protocol: protocol,
      protocol_config: collectProtocolConfig(),
      shared_context: {
        description: $("#lobby-description").value.trim(),
        background: $("#room-shared-background").value.trim(),
        rules: $("#room-shared-rules").value.split("\n").map(x => x.trim()).filter(Boolean),
        custom_instructions: $("#room-custom-instructions").value.trim()
      },
      mode: protocol === "free_discussion" ? $("#lobby-mode").value : "autonomous",
      request_id: requestId,
      initialize_async: true,
      director_config: $("#lobby-director").value ? { mode: $("#lobby-director").value } : null,
      host_participant_id: (state.lobbySlots.find((s) => ["host", "chair"].includes(s.role)) || {}).participant_id || null,
      participants: state.lobbySlots.map((s, i) => {
        // No silent role fallback: a role invalid for the chosen protocol is
        // re-derived from the protocol's default role sequence.
        const roleKept = validRoles.includes(s.role);
        const finalRole = roleKept ? s.role : defaultRole(protocol, i);
        return {
          participant_id: `slot_${i}`,
          persona_id: s.persona_id,
          display_name: "",
          runtime_selection: s.runtime_selection,
          model_selection: s.model_selection,
          reasoning_selection: s.reasoning_selection,
          role: finalRole,
          specialties: (s.specialties && s.specialties.length) ? s.specialties : personaSpecialties(state.personas.find(p => p.id === s.persona_id)),
          authority: Number(roleKept && s.authority != null ? s.authority : authorityForRole(finalRole)),
          tool_permissions: s.tool_permissions || []
        };
      })
    };
    const hostIndex = state.lobbySlots.findIndex((s) => ["host", "chair"].includes(s.role));
    payload.host_participant_id = hostIndex >= 0 ? `slot_${hostIndex}` : null;

    try {
      const res = await api("/api/rooms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (!res || !res.ok) {
        throw new Error((res && res.error) || "房间创建失败");
      }

      const room = res.data;
      const stages = {
        creating: "创建房间...",
        scanning_runtimes: "扫描可用运行时...",
        validating_bindings: "验证运行时绑定...",
        loading_personas: "加载人格...",
        loading_memories: "加载记忆与上下文...",
        connecting_agents: "连接并启动 Agent Adapter...",
        validating_sessions: "校验会话可用性...",
        ready: "准备就绪。"
      };

      for (let attempt = 0; attempt < 150; attempt += 1) {
        const latest = await api(`/api/rooms/${room.id}`);
        if (!latest || !latest.ok) break;
        const current = latest.data;
        const label = stages[current.initialization_stage] || current.initialization_stage || "初始化中...";
        startButton.textContent = label;
        if (progress) progress.textContent = `${label} ${current.initialization_progress || 0}%`;
        if (current.status === "ready") {
          toast(t("saved"));
          await openLiveRoom(room.id);
          return;
        }
        if (current.status === "error") {
          throw new Error(current.last_error || "房间初始化失败");
        }
        await new Promise(resolve => setTimeout(resolve, 300));
      }
      throw new Error("房间初始化超时，请检查所选 Agent / CLI / API 是否可正常连接");
    } catch (err) {
      if (errBox && errDetails) {
        errBox.style.display = "block";
        errDetails.textContent = err.message || String(err);
      }
      toast("初始化失败: " + (err.message || String(err)));
    } finally {
      startButton.disabled = false;
      startButton.textContent = t("startRoom");
      if (progress) progress.textContent = "";
    }
  }

  function renderLiveError() {
    const room = state.currentRoom;
    const banner = $("#live-error-banner");
    const text = $("#live-error-text");
    const retry = $("#btn-retry-room");
    if (!banner || !text) return;
    if (room && (room.status === "error" || room.last_error)) {
      banner.style.display = "block";
      text.textContent = room.last_error || "房间运行中断 (Unknown error)";
      if (retry) retry.hidden = room.status !== "error";
    } else {
      banner.style.display = "none";
      text.textContent = "";
      if (retry) retry.hidden = true;
    }
  }

  async function retryRoomInitialization() {
    const room = state.currentRoom;
    const retry = $("#btn-retry-room");
    const text = $("#live-error-text");
    if (!room || room.status !== "error" || !retry) return;
    retry.disabled = true;
    retry.textContent = "正在重新初始化...";
    if (text) text.textContent = "正在重新加载人物、记忆并连接 Agent...";
    const res = await api(`/api/rooms/${room.id}/start`, { method: "POST" });
    if (res && res.ok) {
      state.currentRoom = res.data;
      await openLiveRoom(room.id);
      toast("房间已恢复，自动讨论正在启动");
    } else {
      room.status = "error";
      room.last_error = (res && res.error) || "房间重新初始化失败";
      renderLiveError();
      toast(room.last_error);
    }
    retry.disabled = false;
    retry.textContent = "重新初始化房间";
  }

  // ─── Protocol stage presentation ─────────────────────────────────────────
  // Users see phase descriptions, never internal enum values.
  const PROTOCOL_STAGE_LABELS = {
    waiting_user: "等待提问",
    user_input: "等待提问",
    question: "等待议题",
    host_analysis: "主持人正在分析问题",
    expert_routing: "正在选择本轮专家",
    host_selects_speaker: "主持人正在选择发言人…",
    expert_task_creation: "正在创建专家任务",
    independent_analysis: "专家正在独立分析",
    independent_opinions: "委员正在发表独立意见…",
    result_collection: "正在收集分析结果",
    cross_review: "正在交叉评审",
    cross_examination: "正在进行交叉质询…",
    optional_rebuttal: "可选申辩",
    rebuttal: "正在进行反驳…",
    optional_discussion: "委员会讨论中…",
    vote: "正在投票…",
    host_synthesis: "主持人正在总结",
    chair_synthesis: "主席正在综合结论…",
    host_final: "主持人正在总结…",
    host_decides_next: "主持人正在决定下一步…",
    member_response: "成员正在回应…",
    final_response: "本轮会诊完成",
    final: "正在生成最终结论…",
    final_decision: "正在形成最终决策…",
    opening: "主持人正在开场…",
    pro_argument: "正方正在陈述…",
    con_argument: "反方正在陈述…",
    final_argument: "正在总结陈词…",
    judge_review: "裁判正在评审…",
    discussion: "自由讨论中…",
    protocol_run: "协议运行中…",
    protocol_finalize: "正在生成最终答复…",
    failed: "运行失败",
  };

  const PROTOCOL_RUN_STATUS_LABELS = {
    pending: "待开始",
    running: "运行中",
    paused: "已暂停",
    success: "已完成",
    partial_success: "部分完成",
    failed: "失败",
    cancelled: "已取消",
    waiting_clarification: "等待补充信息",
  };

  function protocolStageLabel(stage) {
    return PROTOCOL_STAGE_LABELS[stage] || stage || "";
  }

  function participantName(room, participantId) {
    const p = (room.participants || []).find(item => item.participant_id === participantId);
    return p ? (p.display_name || p.persona_id) : participantId;
  }

  function personaIdForParticipant(participantId) {
    const room = state.currentRoom;
    if (!room || !participantId) return "";
    const p = (room.participants || []).find(item => item.participant_id === participantId);
    return p ? p.persona_id : "";
  }

  function protocolStageBanner(room) {
    const runtime = room.protocol_state || {};
    if (runtime.status !== "running" && room.status !== "discussing") return "";
    const label = protocolStageLabel(runtime.current_stage);
    if (!label) return "";
    const working = (runtime.active_participant_ids || [])
      .filter(id => id)
      .map(id => participantName(room, id));
    if (working.length) return `${label}（${working.join("、")}）`;
    return label;
  }

  function applyLiveRoom(room) {
    if (!room) return;
    state.currentRoom = room;
    const title = $("#live-title");
    if (title) title.textContent = room.title || "Room";
    const topic = $("#live-topic");
    if (topic) {
      const topicKey = String(room.topic || "");
      if (state.topicKey !== topicKey) {
        state.topicKey = topicKey;
        state.topicExpanded = false;
      }
      topic.textContent = `${t("topicPrefix")} · ${topicKey}`;
      requestAnimationFrame(syncTopicClamp);
    }
    const turn = $("#live-turn");
    if (turn) turn.textContent = `${t("turnPrefix")} #${room.turn_index || 0}`;
    const statusEl = $("#live-status");
    if (statusEl) statusEl.textContent = statusLabel(room.status);
    syncClearChatButton();

    const canDiscuss = ["ready", "discussing"].includes(room.status);
    const isAutonomous = (room.mode === "autonomous" || room.mode === "auto");
    const isProtocolRoom = (room.protocol || "free_discussion") !== "free_discussion";
    if ($("#btn-pause")) $("#btn-pause").hidden = !canDiscuss;
    if ($("#btn-resume")) {
      const canResume = ROOM_UI_STATE.canResumeRoom(room);
      $("#btn-resume").hidden = !canResume;
      $("#btn-resume").textContent = room.status === "completed" ? t("resumeReady") : t("resume");
    }
    if ($("#btn-next")) {
      $("#btn-next").disabled = !canDiscuss || (isProtocolRoom && state.isTurnBusy);
      $("#btn-next").hidden = isAutonomous && !isProtocolRoom;
      $("#btn-next").textContent = isProtocolRoom ? "运行协作协议" : t("nextTurn");
    }
    if ($("#manual-speaker")) $("#manual-speaker").hidden = isAutonomous || isProtocolRoom;
    if ($("#btn-manual")) $("#btn-manual").hidden = isAutonomous || isProtocolRoom;
    if ($("#btn-finalize-room")) $("#btn-finalize-room").hidden = !isProtocolRoom;
    if ($("#btn-cancel-turn")) {
      const canCancel = ROOM_UI_STATE.canCancelTurn(room);
      $("#btn-cancel-turn").hidden = !canCancel;
      $("#btn-cancel-turn").disabled = !canCancel;
    }
    const btnStop = $("#btn-stop");
    if (btnStop) btnStop.hidden = room.status === "completed";
    const panel = $("#protocol-live-panel");
    if (panel) panel.hidden = !ROOM_UI_STATE.shouldShowProtocolPanel(room);
    const upgradeWarn = $("#protocol-upgrade-warning");
    if (upgradeWarn) upgradeWarn.hidden = !ROOM_UI_STATE.shouldSuggestProtocolUpgrade(room);

    renderLivePills();
    renderChatFeed();
    renderLiveInspector();
    renderProtocolStatus();
    renderLiveError();

    // A terminal protocol state ends the turn even if the WebSocket
    // final_response event was lost: HTTP polling is the fallback authority.
    const call = (room.metadata && room.metadata.model_call) || {};
    if (ROOM_UI_STATE.shouldSettleBusy(room)) {
      state.isTurnBusy = false;
      banner(false);
    } else if (call.status === "calling") {
      const bits = [t("bannerGen")];
      if (call.model_id) bits.push(call.model_id);
      if (call.display_name) bits.push(call.display_name);
      banner(true, bits.join(" · "));
    } else if (room.status === "discussing") {
      banner(true, protocolStageBanner(room) || t("bannerWaiting"));
    }
  }

  // Topic line clamp: default max two lines with an expand/collapse toggle
  // rendered only when the full text actually overflows the clamp.
  function syncTopicClamp() {
    const topicEl = $("#live-topic");
    const toggle = $("#btn-topic-toggle");
    if (!topicEl || !toggle) return;
    topicEl.classList.toggle("is-clamped", !state.topicExpanded);
    if (!state.topicExpanded) {
      state.topicOverflow = topicEl.clientHeight > 0
        && topicEl.scrollHeight > topicEl.clientHeight + 1;
    }
    toggle.hidden = !(state.topicExpanded || state.topicOverflow);
    toggle.textContent = t(state.topicExpanded ? "collapseTopic" : "expandTopic");
  }

  function renderProtocolStatus() {
    const room = state.currentRoom;
    if (!room) return;
    const protocol = room.protocol || "free_discussion";
    const runtime = room.protocol_state || {};
    const participants = room.participants || [];
    const leaders = participants.filter(item => ["host", "chair"].includes(item.role));
    const runStatus = runtime.status || "pending";
    const isTerminalRun = ["success", "partial_success", "failed", "cancelled"].includes(runStatus);
    $("#live-protocol").textContent = (protocolMeta[protocol] || {}).label || protocol;
    const displayStage = ROOM_UI_STATE.protocolStageForDisplay(runtime);
    $("#live-protocol-stage").textContent = protocolStageLabel(displayStage) || "等待提问";

    $("#live-run-status").textContent = PROTOCOL_RUN_STATUS_LABELS[runStatus] || runStatus;

    // waiting_clarification: the host stopped on purpose to ask the user one
    // indispensable question (GET /api/rooms/{id} carries
    // protocol_state.clarification.question).
    const clarifyEl = $("#protocol-clarification-banner");
    const clarifyText = $("#protocol-clarification-text");
    const clarifyQuestion = String((runtime.clarification || {}).question || "").trim();
    const showClarify = runStatus === "waiting_clarification" && !!clarifyQuestion;
    if (clarifyEl) clarifyEl.hidden = !showClarify;
    if (clarifyText && showClarify) {
      clarifyText.textContent = `主持人需要补充以下信息：${clarifyQuestion}`;
    }

    // Stage progress: tasks in the current stage, done vs total.
    const stageTasks = (runtime.tasks || []).filter(task =>
      task.stage === runtime.current_stage && task.participant_id
    );
    const doneCount = stageTasks.filter(task => ["success", "skipped", "cancelled", "failed"].includes(task.status)).length;
    $("#live-protocol-progress").textContent = stageTasks.length
      ? `${doneCount} / ${stageTasks.length} 已完成`
      : "—";

    // Member groups for the current stage: working / done / not called yet.
    const active = new Set((runtime.active_participant_ids || []).filter(Boolean));
    const allTasks = (runtime.tasks || []).filter(task => task && task.participant_id);
    const taskByParticipant = new Map(stageTasks.map(task => [task.participant_id, task]));
    // Working = active participants whose current-stage task is running.
    // Sparse task history (no per-participant rows yet) falls back to every
    // active id so a running stage never renders as if nobody works.
    const workingIds = runStatus === "running"
      ? [...active].filter(pid => stageTasks.length
        ? (taskByParticipant.get(pid) || {}).status === "running"
        : true)
      : [];
    // Done groups come from the current stage; a finished run falls back to
    // the whole task history so completed members survive the stage switch.
    const doneSource = stageTasks.length || !isTerminalRun ? stageTasks : allTasks;
    const okIds = [...new Set(doneSource
      .filter(task => ["success", "skipped"].includes(task.status))
      .map(task => task.participant_id))];
    const badIds = [...new Set(doneSource
      .filter(task => ["cancelled", "failed"].includes(task.status))
      .map(task => task.participant_id))];
    // Uncalled = expected members without a task in this run/stage yet.
    // Sparse task history falls back to selected experts + host.
    let uncalledIds;
    if (allTasks.length) {
      const stagePids = new Set(stageTasks.map(task => task.participant_id));
      uncalledIds = participants.map(item => item.participant_id)
        .filter(pid => pid && !stagePids.has(pid) && !active.has(pid));
    } else {
      const expected = new Set((runtime.selected_expert_ids || []).filter(Boolean));
      leaders.forEach(item => expected.add(item.participant_id));
      if (!expected.size) participants.forEach(item => expected.add(item.participant_id));
      const engaged = new Set([...active, ...okIds, ...badIds]);
      uncalledIds = [...expected].filter(pid => !engaged.has(pid));
    }

    const workingEl = $("#live-active-members");
    if (workingEl) {
      workingEl.innerHTML = workingIds.length
        ? workingIds.map(pid => `<span class="tag">${esc(participantName(room, pid))} ●</span>`).join("")
        : `<span class="meta">—</span>`;
    }
    const doneEl = $("#live-completed-members");
    if (doneEl) {
      const okHtml = okIds.map(pid => `<span class="tag ok">${esc(participantName(room, pid))} ✓</span>`).join("");
      const badHtml = badIds.map(pid => `<span class="tag warn">${esc(participantName(room, pid))} ✕</span>`).join("");
      doneEl.innerHTML = (okHtml + badHtml) || `<span class="meta">—</span>`;
    }

    // 未调用成员 lives in the main status card (正在工作 / 已完成 / 未调用成员)
    // and stays mirrored inside the collapsed 运行详情 block for operators.
    const memberHtml = (ids) => ids.length
      ? ids.map((pid) => {
        const item = participants.find(p => p.participant_id === pid);
        const label = (item && (item.display_name || item.persona_id)) || pid;
        const role = item && item.role ? ` · ${esc(item.role)}` : "";
        return `<span class="tag">${esc(label)}${role}</span>`;
      }).join("")
      : `<span class="meta">—</span>`;
    const inactiveEl = $("#live-inactive-members");
    if (inactiveEl) inactiveEl.innerHTML = memberHtml(uncalledIds);
    const uncalledMainEl = $("#live-uncalled-members");
    if (uncalledMainEl) uncalledMainEl.innerHTML = memberHtml(uncalledIds);
    const hostEl = $("#live-protocol-host");
    if (hostEl) hostEl.textContent = leaders.map(item => item.display_name || item.persona_id).join("、") || "—";

    // Raw event timeline lives under "运行详情" for operators, not for chat.
    const events = room.protocol_events || [];
    const feed = $("#protocol-event-feed");
    if (feed) {
      feed.innerHTML = events.length ? events.slice(-30).map(event => {
        const when = event.created_at ? new Date(event.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
        const label = event.event_type || event.event || "event";
        const actor = participants.find(item => item.participant_id === event.actor_id);
        return `<div class="protocol-event"><span class="num meta">${esc(when)}</span><strong>${esc(label)}</strong><span>${esc((actor && (actor.display_name || actor.persona_id)) || event.actor_id || event.stage || "")}</span></div>`;
      }).join("") : `<p class="meta">协议尚未运行。</p>`;
    }
  }

  async function watchLiveRoom(roomId) {
    const token = ++state.liveWatchToken;
    while (
      token === state.liveWatchToken &&
      state.roomSub === "live" &&
      state.currentRoom &&
      state.currentRoom.id === roomId
    ) {
      const latest = await api(`/api/rooms/${encodeURIComponent(roomId)}`);
      if (token !== state.liveWatchToken) return;
      if (latest && latest.ok && latest.data) {
        applyLiveRoom(latest.data);
        const data = latest.data;
        // Stop polling once the room reaches a resting state; never poll
        // forever while the HTTP GET is the fallback that clears the
        // "waiting for model" banner.
        if (ROOM_UI_STATE.shouldStopWatch(data)) return;
      }
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  }

  async function openLiveRoom(roomId) {
    let room = state.rooms.find(r => r.id === roomId);
    const res = await api(`/api/rooms/${roomId}`);
    if (res && res.ok) {
      room = res.data;
    }
    if (!room) return;

    if (!state.inspectParticipantId) {
      state.inspectParticipantId = room.participants && room.participants.length ? room.participants[0].participant_id : null;
    }
    state.activeSpeakingSlotId = null;

    const sel = $("#manual-speaker");
    if (sel) {
      sel.innerHTML = `<option value="">${t("selectSpeaker")}</option>` +
        (room.participants || []).map(p => `<option value="${esc(p.participant_id)}">${esc(p.display_name || p.persona_id)}</option>`).join("");
    }

    applyLiveRoom(room);
    showRoomSub("live");
    connectRoomWs(room.id);
    // (Re)opening a room is an explicit data request: pull the real runtime
    // state for the selected persona now.
    refreshLiveInspector({ force: true });
    if (["discussing", "initializing"].includes(room.status)) {
      watchLiveRoom(room.id);
    }
  }

  function wsSend(payload) {
    const ws = state.currentRoomWs;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(payload));
    return true;
  }

  function pushTranscript(entry) {
    const room = state.currentRoom;
    if (!room || !entry) return;
    room.transcript = room.transcript || [];
    const turnId = entry.turn_id;
    if (turnId && room.transcript.some((item) => item.turn_id === turnId)) return;
    if (
      !turnId &&
      room.transcript.some(
        (item) => item.content === entry.content && item.speaker_name === entry.speaker_name
      )
    ) {
      return;
    }
    room.transcript.push(entry);
  }

  function transcriptHasModelOutput(room) {
    return (room.transcript || []).some(
      (item) => item.participant_id && item.participant_id !== "user"
    );
  }

  function stopRoomWsHeartbeat() {
    if (state.wsHeartbeatTimer) {
      clearInterval(state.wsHeartbeatTimer);
      state.wsHeartbeatTimer = null;
    }
  }

  function disconnectRoomWs() {
    state.wsGeneration += 1;
    stopRoomWsHeartbeat();
    if (state.wsReconnectTimer) {
      clearTimeout(state.wsReconnectTimer);
      state.wsReconnectTimer = null;
    }
    const prev = state.currentRoomWs;
    state.currentRoomWs = null;
    state.wsRoomId = null;
    if (prev) {
      try {
        prev.onclose = null;
        prev.onerror = null;
        prev.onmessage = null;
        prev.onopen = null;
        if (prev.readyState === WebSocket.OPEN || prev.readyState === WebSocket.CONNECTING) {
          prev.close(1000, "client-leave");
        }
      } catch (_) {}
    }
  }

  function connectRoomWs(roomId) {
    if (
      state.currentRoomWs &&
      state.wsRoomId === roomId &&
      (state.currentRoomWs.readyState === WebSocket.OPEN ||
        state.currentRoomWs.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    const generation = ++state.wsGeneration;
    stopRoomWsHeartbeat();
    const prev = state.currentRoomWs;
    if (prev) {
      try {
        prev.onclose = null;
        prev.onerror = null;
        prev.onmessage = null;
        prev.onopen = null;
        if (prev.readyState === WebSocket.OPEN || prev.readyState === WebSocket.CONNECTING) {
          prev.close(1000, "replace");
        }
      } catch (_) {}
    }
    try {
      const loc = window.location;
      const wsProto = loc.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${wsProto}//${loc.host}/api/rooms/${encodeURIComponent(roomId)}/ws`;
      const ws = new WebSocket(wsUrl);
      state.currentRoomWs = ws;
      state.wsRoomId = roomId;
      ws.onopen = () => {
        if (generation !== state.wsGeneration) return;
        state.wsReconnectAttempts = 0;
        stopRoomWsHeartbeat();
        state.wsHeartbeatTimer = setInterval(() => {
          if (generation !== state.wsGeneration) return;
          wsSend({ action: "ping" });
        }, 25000);
        const room = state.currentRoom;
        if (ROOM_UI_STATE.shouldStartLegacyAutonomous(room, transcriptHasModelOutput(room))) {
          // Legacy autonomous discussion only ever starts for free_discussion
          // rooms; protocol rooms advance through their own runtime.
          ws.send(JSON.stringify({ action: "autonomous", max_turns: 6 }));
          const hasUser = (room.transcript || []).some((item) => item.participant_id === "user");
          banner(true, hasUser ? t("bannerWaiting") : t("bannerHost"));
          watchLiveRoom(room.id);
        }
      };

      ws.onmessage = (e) => {
        if (generation !== state.wsGeneration) return;
        try {
          const data = JSON.parse(e.data);
          if ((data.event || data.type) === "pong") return;
          handleRoomEvent(data);
        } catch (_) {}
      };
      ws.onerror = () => {
        if (generation !== state.wsGeneration) return;
        console.warn("Room websocket error", wsUrl);
      };
      ws.onclose = (ev) => {
        if (generation !== state.wsGeneration) return;
        stopRoomWsHeartbeat();
        if (!state.currentRoom || state.currentRoom.id !== roomId) return;
        if (!["ready", "discussing"].includes(state.currentRoom.status)) return;
        if (ev && ev.code === 1000) return;
        if (state.wsReconnectAttempts >= 8) return;
        state.wsReconnectAttempts += 1;
        if (state.wsReconnectAttempts === 2) toast(t("wsLost"));
        if (state.wsReconnectTimer) clearTimeout(state.wsReconnectTimer);
        state.wsReconnectTimer = setTimeout(() => {
          if (generation !== state.wsGeneration) return;
          if (state.currentRoom && state.currentRoom.id === roomId) connectRoomWs(roomId);
        }, Math.min(8000, 600 * (2 ** (state.wsReconnectAttempts - 1))));
      };
    } catch (err) {
      console.error("Room websocket connect failed", err);
    }
  }

  function handleRoomEvent(ev) {
    const room = state.currentRoom;
    if (!room) return;
    const type = ev.event || ev.type;

    // Protocol public messages carry real chat content: they enter the chat
    // feed immediately (turn_id dedup keeps WS replays idempotent).
    if (type === "protocol_message" && ev.message) {
      pushTranscript(ev.message);
      renderChatFeed();
      return;
    }

    const protocolEvents = new Set([
      "room_started", "stage_changed", "expert_selected", "task_created",
      "host_analysis_submitted", "analysis_submitted", "review_submitted",
      "rebuttal_submitted", "synthesis_submitted", "vote_submitted",
      "task_completed", "final_response", "cancelled", "error"
    ]);
    if (ev.run_id && protocolEvents.has(type)) {
      room.protocol_events = room.protocol_events || [];
      if (!room.protocol_events.some(item => item.id && item.id === ev.id)) room.protocol_events.push({ ...ev, event_type: type });
      room.protocol_state = room.protocol_state || {};
      if (ev.stage) room.protocol_state.current_stage = ev.stage;
      if (ev.status) room.protocol_state.status = ev.status;
      if (ev.actor_id) room.protocol_state.active_participant_ids = [...new Set([...(room.protocol_state.active_participant_ids || []), ev.actor_id])];
      // final_response is a per-run terminal marker; dedup by run so a
      // replayed or duplicated event cannot end the next run's busy state.
      if (type === "final_response") {
        state.finalResponseSeenRuns = state.finalResponseSeenRuns || new Set();
        const runKey = String(ev.run_id || "");
        if (runKey && state.finalResponseSeenRuns.has(runKey)) {
          renderProtocolStatus();
          return;
        }
        if (runKey) state.finalResponseSeenRuns.add(runKey);
      }
      if (type === "final_response" && ["success", "partial_success", "failed"].includes(ev.status)) {
        state.isTurnBusy = false;
        room.status = ev.status === "failed" ? "error" : "ready";
        banner(false);
        renderChatFeed();
      } else if (type === "cancelled") {
        state.isTurnBusy = false;
        room.status = "ready";
        banner(false);
      } else if (type === "error" && ev.status === "failed") {
        state.isTurnBusy = false;
        banner(false);
      } else if (type === "stage_changed") {
        banner(true, protocolStageBanner(room) || protocolStageLabel(ev.stage) || t("bannerWaiting"));
      } else if (["analysis_submitted", "review_submitted", "synthesis_submitted", "host_analysis_submitted", "rebuttal_submitted", "task_completed"].includes(type)) {
        banner(true, protocolStageBanner(room) || t("bannerWaiting"));
      } else {
        banner(true, `${(protocolMeta[room.protocol] || {}).label || room.protocol} · ${protocolStageLabel(ev.stage) || ev.stage || type}`);
      }
      renderProtocolStatus();
    }

    if (type === "room_initialization_progress") {
      banner(true, `${ev.stage || "initializing"} · ${ev.progress || 0}%`);
    } else if (type === "autonomous_discussion_started") {
      room.status = "discussing";
      room.last_error = null;
      $("#live-status").textContent = statusLabel("discussing");
      $("#btn-inject").disabled = !$("#inject").value.trim();
      const hasUser = (room.transcript || []).some((item) => item.participant_id === "user");
      banner(true, hasUser ? t("bannerWaiting") : t("bannerHost"));
      renderLiveError();
    } else if (type === "host_opened" || type === "host_steered" || type === "host_summarized") {
      pushTranscript({ participant_id: "host", speaker_name: "Host", content: ev.content });
      renderChatFeed();
      if (type === "host_summarized") {
        room.status = "completed";
        $("#live-status").textContent = statusLabel("completed");
        $("#btn-inject").disabled = true;
        banner(false);
      } else {
        banner(true, t("bannerWaiting"));
      }
    } else if (type === "speaker_selected" || type === "discussion_director_selected") {
      banner(true, `${t("bannerSelect")} · ${ev.speaker_name || ev.next_speaker || ""}`);
    } else if (type === "agent_started") {
      banner(true, t("bannerGen"));
    } else if (type === "recall_completed") {
      banner(true, t("bannerRecall"));
    } else if (type === "agent_message_delta") {
      banner(true, t("bannerGen"));
      appendChunk(ev.delta || ev.text_delta || "", !!ev.replace_response);
    } else if (type === "persona_commit_completed") {
      // Runtime state was just persisted for this persona.  Debounced so a
      // burst of commits (one per participant) triggers one refresh, not N.
      const committedId = ev.persona_id || personaIdForParticipant(ev.participant_id);
      if (committedId && ev.state_summary) {
        room.stateSummaries = room.stateSummaries || {};
        room.stateSummaries[committedId] = ev.state_summary;
      }
      scheduleRuntimeRefresh(committedId);
    } else if (type === "turn_completed") {
      banner(false);
      room.turn_index = ev.turn_index != null ? ev.turn_index : (room.turn_index + 1);
      $("#live-turn").textContent = `${t("turnPrefix")} #${room.turn_index}`;
      state.activeSpeakingSlotId = null;
      renderLivePills();
      renderChatFeed();
      renderLiveInspector();
      renderLiveError();
      state.isTurnBusy = false;
      refreshLiveInspector({ force: true });
    } else if (type === "room_paused") {
      room.status = "paused";
      $("#live-status").textContent = statusLabel("paused");
      $("#btn-pause").hidden = true;
      $("#btn-resume").hidden = false;
      $("#btn-next").disabled = true;
    } else if (type === "room_resumed") {
      room.status = "ready";
      $("#live-status").textContent = statusLabel("ready");
      $("#btn-pause").hidden = false;
      $("#btn-resume").hidden = true;
      $("#btn-next").disabled = false;
    } else if (type === "room_message_injected") {
      if (ev.message) pushTranscript(ev.message);
      renderChatFeed();
    } else if (type === "agent_error" || type === "room_initialization_error") {
      room.status = "error";
      room.last_error = ev.error || ev.last_error || "Agent runtime error";
      $("#live-status").textContent = statusLabel("error");
      banner(false);
      state.isTurnBusy = false;
      renderLiveError();
      toast(room.last_error);
    } else if (type === "transcript_cleared") {
      // Another client wiped the room's conversation.  Drop the in-memory
      // window and re-render so both tabs stay in sync.
      room.transcript = [];
      renderChatFeed();
      syncClearChatButton();
      toast(t("clearConversationDone"));
    }
  }

  function banner(on, text) {
    const b = $("#turn-banner");
    if (!b) return;
    b.classList.toggle("is-on", !!on);
    const txt = $("#banner-text");
    if (txt) txt.textContent = text || "";
  }

  function appendChunk(chunk, replace = false) {
    const feed = $("#chat-feed");
    if (!feed) return;
    let lastMsg = feed.lastElementChild;
    if (replace && lastMsg && lastMsg.classList.contains("streaming")) {
      const body = lastMsg.querySelector(".msg-body");
      if (body) body.textContent = "";
    }
    if (!lastMsg || !lastMsg.classList.contains("streaming")) {
      lastMsg = document.createElement("div");
      lastMsg.className = "msg streaming";
      lastMsg.innerHTML = `
        <div class="avatar sm">?</div>
        <div style="flex:1;min-width:0;">
          <div class="row"><span class="msg-name">Speaking</span><span class="msg-time num">${nowTime()}</span></div>
          <div class="msg-body"></div>
        </div>`;
      feed.appendChild(lastMsg);
    }
    const body = lastMsg.querySelector(".msg-body");
    if (body) body.textContent += chunk;
    feed.scrollTop = feed.scrollHeight;
  }

  function renderLivePills() {
    const room = state.currentRoom;
    const barEl = $("#live-pills");
    if (!barEl || !room) return;

    barEl.innerHTML = (room.participants || []).map((p) => {
      const snap = (room.binding_snapshots && room.binding_snapshots[p.participant_id]) || {};
      const isSel = state.inspectParticipantId === p.participant_id ? "is-selected" : "";
      const isSpk = state.activeSpeakingSlotId === p.participant_id ? "is-speaking" : "";
      const name = p.display_name || p.persona_id;
      return `
        <button type="button" class="person-pill ${isSel} ${isSpk}" data-pid="${esc(p.participant_id)}">
          <div class="avatar sm">${name.charAt(0).toUpperCase()}</div>
          <div>
            <h4>${esc(name)}</h4>
            <p>${esc(snap.agent_runtime_name || snap.agent_runtime_id || "Agent")} · ${esc(snap.model_name || snap.model_id || "Model")}</p>
          </div>
        </button>`;
    }).join("");

    barEl.querySelectorAll("[data-pid]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.inspectParticipantId = btn.dataset.pid;
        renderLivePills();
        // Switching persona is a real data switch: re-read that persona's
        // runtime state instead of re-painting the previous one's numbers.
        refreshLiveInspector({ force: true });
      });
    });
  }

  // Protocol message cards: light labels over the normal chat style. Only
  // public structured output reaches the transcript, never hidden reasoning.
  const PROTOCOL_KIND_META = {
    protocol_host_analysis: { label: "主持分析", cls: "" },
    protocol_routing: { label: "专家路由", cls: "protocol-muted" },
    protocol_analysis: { label: "独立分析", cls: "" },
    protocol_review: { label: "交叉评审", cls: "protocol-collapsible" },
    protocol_rebuttal: { label: "补充反驳", cls: "protocol-collapsible" },
    protocol_synthesis: { label: "综合结论 · 最终答复", cls: "protocol-final" },
    protocol_final: { label: "最终答复", cls: "protocol-final" },
  };

  // Safe markdown bridge: safe_markdown.js loads before app.js.  Fall back
  // to plain escaping if the module is missing so bodies never go raw.
  const renderMessageBody = (content) => (
    typeof window.renderSafeMessageContent === "function"
      ? window.renderSafeMessageContent(content)
      : esc(content)
  );

  const payloadText = (value) => {
    if (typeof value === "string") return value;
    if (value && typeof value === "object") {
      for (const key of ["text", "content", "summary", "position", "description", "title"]) {
        if (typeof value[key] === "string" && value[key].trim()) return value[key];
      }
      return "";
    }
    return value == null ? "" : String(value);
  };

  // Structured 【本轮会诊总结】 card.  Card identity stays tied to
  // turn_id/metadata (kind + public_payload); content is never used as a
  // key, so the existing once-only rendering guarantee is preserved.
  // Returns null whenever the public payload is missing/invalid so the
  // caller keeps the plain body path (old messages without public_payload).
  function synthesisSections(m) {
    const payload = m && m.metadata && m.metadata.public_payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
    const summary = payloadText(payload.summary).trim();
    if (!summary) return null;

    const room = state.currentRoom || {};
    const positions = Array.isArray(payload.participant_positions) ? payload.participant_positions : [];
    const list = (key) => (Array.isArray(payload[key]) ? payload[key] : [])
      .map(payloadText).filter(item => item.trim());
    const consensus = list("consensus");
    const conflicts = list("conflicts");
    const recommendations = list("recommendations");
    const judgment = payloadText(payload.final_judgment).trim();

    const title = state.lang === "zh-CN" ? `【${t("synthTitle")}】` : t("synthTitle");
    const positionTitle = positions.length === 1 ? t("synthSingle") : t("synthPositions");
    const section = (label, inner) =>
      `<div class="synthesis-section"><h4 class="synthesis-label">${esc(label)}</h4>${inner}</div>`;
    const bullets = (items) => `<ul>${items.map(item => `<li>${renderMessageBody(item)}</li>`).join("")}</ul>`;

    const sections = [section(t("synthSummary"), `<p class="synthesis-lead">${renderMessageBody(summary)}</p>`)];
    const positionItems = positions.map((item) => {
      if (!item || typeof item !== "object") return "";
      const name = esc(participantName(room, item.participant_id) || item.participant_id || "—");
      const position = renderMessageBody(payloadText(item.position));
      const reason = payloadText(item.key_reason).trim();
      return `<li>${name}: ${position}${reason ? ` —— ${renderMessageBody(reason)}` : ""}</li>`;
    }).filter(Boolean).join("");
    if (positionItems) sections.push(section(positionTitle, `<ul>${positionItems}</ul>`));
    if (consensus.length) sections.push(section(t("synthConsensus"), bullets(consensus)));
    if (conflicts.length) sections.push(section(t("synthConflicts"), bullets(conflicts)));
    if (judgment) sections.push(section(t("synthJudgment"), `<p>${renderMessageBody(judgment)}</p>`));
    if (recommendations.length) {
      sections.push(section(t("synthNext"), `<ol>${recommendations.map(item => `<li>${renderMessageBody(item)}</li>`).join("")}</ol>`));
    }

    return `<div class="protocol-synthesis"><div class="synthesis-title"><span class="synthesis-chip">${esc(title)}</span></div>${sections.join("")}</div>`;
  }

  function renderChatFeed() {
    const room = state.currentRoom;
    const feed = $("#chat-feed");
    if (!feed || !room) return;

    feed.innerHTML = (room.transcript || []).map((m) => {
      const kind = m.metadata && m.metadata.message_kind;
      const meta = kind ? PROTOCOL_KIND_META[kind] : null;
      const isUser = m.participant_id === "user" || m.commit_status === "user_injected";
      if (meta) {
        const structured = (kind === "protocol_synthesis" || kind === "protocol_final")
          ? synthesisSections(m)
          : null;
        const bodyContent = structured || renderMessageBody(m.content);
        const body = meta.cls.includes("protocol-collapsible") && !structured
          ? `<details class="protocol-details"><summary>${esc(t("expandReview"))}</summary><div class="msg-body md">${bodyContent}</div></details>`
          : `<div class="msg-body md">${bodyContent}</div>`;
        return `
      <div class="msg protocol-msg ${meta.cls}">
        <div class="avatar sm">${esc((m.speaker_name || "?").charAt(0).toUpperCase())}</div>
        <div style="flex:1;min-width:0;">
          <div class="row">
            <span class="msg-name">${esc(m.speaker_name || m.participant_id)}</span>
            <span class="tag protocol-tag">${esc(meta.label)}</span>
            <span class="msg-time num">${esc(m.created_at ? m.created_at.slice(11, 16) : nowTime())}</span>
          </div>
          ${body}
        </div>
      </div>`;
      }
      return `
      <div class="msg ${isUser ? "user" : ""}">
        <div class="avatar sm">${esc((m.speaker_name || "?").charAt(0).toUpperCase())}</div>
        <div style="flex:1;min-width:0;">
          <div class="row">
            <span class="msg-name">${esc(m.speaker_name || m.participant_id)}</span>
            <span class="msg-time num">${esc(m.created_at ? m.created_at.slice(11, 16) : nowTime())}</span>
          </div>
          <div class="msg-body md">${renderMessageBody(m.content)}</div>
        </div>
      </div>`;
    }).join("");
    feed.scrollTop = feed.scrollHeight;
    syncClearChatButton();
  }

  // ─── Real Persona Runtime State ──────────────────────────────────────────
  // The inspector must never invent numbers.  Everything below is read from
  // GET /api/personas/{id}/runtime, which returns the persisted AffectState,
  // NeedState and RelationshipState rows for the persona's branch.
  const RELATIONSHIP_UI_FIELDS = [
    "familiarity", "trust", "affection", "respect", "dependence",
    "resentment", "jealousy", "perceived_threat", "unresolved_conflict"
  ];

  // Domain field names are English identifiers; the UI is bilingual.
  const STATE_LABELS = {
    joy: { "zh-CN": "喜悦", en: "Joy" },
    sadness: { "zh-CN": "悲伤", en: "Sadness" },
    anger: { "zh-CN": "愤怒", en: "Anger" },
    fear: { "zh-CN": "恐惧", en: "Fear" },
    disgust: { "zh-CN": "厌恶", en: "Disgust" },
    surprise: { "zh-CN": "惊讶", en: "Surprise" },
    anxiety: { "zh-CN": "焦虑", en: "Anxiety" },
    jealousy: { "zh-CN": "嫉妒", en: "Jealousy" },
    shame: { "zh-CN": "羞耻", en: "Shame" },
    guilt: { "zh-CN": "内疚", en: "Guilt" },
    hope: { "zh-CN": "希望", en: "Hope" },
    loneliness: { "zh-CN": "孤独", en: "Loneliness" },
    affection: { "zh-CN": "亲近", en: "Affection" },
    frustration: { "zh-CN": "挫败", en: "Frustration" },
    attachment: { "zh-CN": "依恋", en: "Attachment" },
    recognition: { "zh-CN": "被认可", en: "Recognition" },
    autonomy: { "zh-CN": "自主", en: "Autonomy" },
    control: { "zh-CN": "掌控", en: "Control" },
    safety: { "zh-CN": "安全", en: "Safety" },
    belonging: { "zh-CN": "归属", en: "Belonging" },
    achievement: { "zh-CN": "成就", en: "Achievement" },
    curiosity: { "zh-CN": "好奇", en: "Curiosity" },
    continuity: { "zh-CN": "连续性", en: "Continuity" },
    being_understood: { "zh-CN": "被理解", en: "Being understood" },
    familiarity: { "zh-CN": "熟悉度", en: "Familiarity" },
    trust: { "zh-CN": "信任", en: "Trust" },
    respect: { "zh-CN": "尊重", en: "Respect" },
    dependence: { "zh-CN": "依赖", en: "Dependence" },
    resentment: { "zh-CN": "怨恨", en: "Resentment" },
    perceived_threat: { "zh-CN": "威胁感", en: "Perceived threat" },
    unresolved_conflict: { "zh-CN": "未解冲突", en: "Unresolved conflict" }
  };

  function stateLabel(name) {
    const entry = STATE_LABELS[name];
    if (entry) return entry[state.lang] || entry.en;
    return String(name == null ? "" : name).replace(/_/g, " ");
  }

  const pctText = (v) => `${Math.round((Number(v) || 0) * 100)}%`;

  function fmtStateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString(state.lang === "zh-CN" ? "zh-CN" : "en-US", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
    });
  }

  // Significance = distance from the persona's own baseline.  A flat 0.5 is
  // uninteresting; a 0.5 sitting on a 0.1 baseline is the story.
  const significanceOf = (value, baseline) => Math.abs((Number(value) || 0) - (Number(baseline) || 0));

  function latestReason(list) {
    return Array.isArray(list) && list.length ? String(list[list.length - 1]) : "";
  }

  function normalizeRuntime(runtime) {
    const emotions = ((runtime && runtime.emotions) || []).map((e) => ({
      name: e.name,
      value: Number(e.intensity) || 0,
      baseline: Number(e.baseline) || 0,
      confidence: Number(e.confidence) || 0,
      updated: fmtStateTime(e.updated_at),
      trigger: latestReason(e.triggers)
    }));
    const needs = ((runtime && runtime.needs) || []).map((n) => ({
      name: n.name,
      value: Number(n.level) || 0,
      baseline: Number(n.baseline) || 0,
      confidence: Number(n.confidence) || 0,
      updated: fmtStateTime(n.updated_at),
      trigger: latestReason(n.reasons)
    }));
    const relationships = ((runtime && runtime.relationships) || []).map((r) => ({
      counterpart: r.counterpart,
      fields: RELATIONSHIP_UI_FIELDS
        .map((f) => ({ name: f, value: Number(r[f]) || 0 }))
        .filter((f) => Math.abs(f.value) > 1e-6),
      updated: fmtStateTime(r.updated_at),
      trigger: latestReason(r.reasons)
    })).filter((r) => r.fields.length);
    return { emotions, needs, relationships };
  }

  // Top N by significance; the rest fold behind a disclosure instead of
  // drowning the panel in fourteen near-identical bars.
  function stateRows(items, topN = 5) {
    if (!items || !items.length) return `<p class="meta">${t("noState")}</p>`;
    const rows = [...items].sort(
      (a, b) => significanceOf(b.value, b.baseline) - significanceOf(a.value, a.baseline)
    );
    const render = (row) => `
      <div class="bar">
        <div class="bar-top"><span>${esc(stateLabel(row.name))}</span><span class="num">${pctText(row.value)}</span></div>
        <div class="track"><div class="fill" style="width:${Math.round(row.value * 100)}%"></div></div>
        <div class="meta num" style="margin-top:4px;display:flex;gap:10px;flex-wrap:wrap;">
          <span>${t("stateBaseline")} ${pctText(row.baseline)}</span>
          <span>${t("stateConfidence")} ${pctText(row.confidence)}</span>
          <span>${t("stateUpdated")} ${esc(row.updated)}</span>
        </div>
        ${row.trigger ? `<div class="meta" style="margin-top:2px;">${t("stateTrigger")} · ${esc(row.trigger)}</div>` : ""}
      </div>`;
    const head = rows.slice(0, topN).map(render).join("");
    const rest = rows.slice(topN);
    if (!rest.length) return head;
    return `${head}
      <details style="margin-top:8px;">
        <summary class="meta" style="cursor:pointer;">${t("viewAll")} (${rest.length})</summary>
        <div style="margin-top:8px;">${rest.map(render).join("")}</div>
      </details>`;
  }

  function relationCards(relationships) {
    if (!relationships.length) return `<p class="meta">${t("noRelation")}</p>`;
    return relationships.map((r) => `
      <div class="card" style="padding:12px;margin-bottom:8px;">
        <div class="row-between">
          <strong style="font-size:13px;">${esc(r.counterpart)}</strong>
          <span class="meta num">${esc(r.updated)}</span>
        </div>
        <div style="margin-top:8px;">
          ${r.fields.map((f) => `
            <div class="bar">
              <div class="bar-top"><span>${esc(stateLabel(f.name))}</span><span class="num">${pctText(f.value)}</span></div>
              <div class="track"><div class="fill" style="width:${Math.round(f.value * 100)}%"></div></div>
            </div>`).join("")}
        </div>
        ${r.trigger ? `<p class="meta" style="margin-top:6px;">${esc(r.trigger)}</p>` : ""}
      </div>`).join("");
  }

  function runtimeCacheKey(personaId, branchId) {
    return `${personaId || ""}::${branchId || "main"}`;
  }

  function currentBranchId() {
    return (state.currentRoom && state.currentRoom.branch_id) || "main";
  }

  function invalidatePersonaRuntime(personaId, branchId) {
    if (!personaId) return;
    state.runtimeCache.delete(runtimeCacheKey(personaId, branchId || currentBranchId()));
  }

  async function fetchPersonaRuntime(personaId, branchId) {
    if (!personaId) return null;
    const key = runtimeCacheKey(personaId, branchId);
    const inflight = state.runtimeInflight.get(key);
    if (inflight) return inflight;
    const task = (async () => {
      const res = await api(
        `/api/personas/${encodeURIComponent(personaId)}/runtime?branch_id=${encodeURIComponent(branchId)}`
      );
      const data = res && res.ok && res.data ? res.data : null;
      state.runtimeCache.set(key, { ts: Date.now(), data });
      return data;
    })().finally(() => state.runtimeInflight.delete(key));
    state.runtimeInflight.set(key, task);
    return task;
  }

  // Fetch-then-render.  Called on room open, participant switch and turn
  // completion — never from a timer, so there is no polling loop.
  async function refreshLiveInspector({ force = false } = {}) {
    const room = state.currentRoom;
    if (!room) return;
    const branchId = currentBranchId();
    const part = (room.participants || []).find((p) => p.participant_id === state.inspectParticipantId);
    const personaId = part && part.persona_id;
    if (!personaId) {
      renderLiveInspector();
      return;
    }
    if (force) invalidatePersonaRuntime(personaId, branchId);
    if (!state.runtimeCache.has(runtimeCacheKey(personaId, branchId))) {
      state.runtimePending = true;
      renderLiveInspector();
    }
    try {
      await fetchPersonaRuntime(personaId, branchId);
    } catch (_err) {
      // A failed read must leave the panel in its "read failed" state, never
      // propagate out of a UI refresh and break the event handler chain.
      state.runtimeCache.set(runtimeCacheKey(personaId, branchId), { ts: Date.now(), data: null });
    } finally {
      state.runtimePending = false;
      renderLiveInspector();
    }
  }

  // persona_commit_completed arrives once per participant, so a multi-persona
  // turn fires a small burst.  Debounce it into a single refresh.
  function scheduleRuntimeRefresh(personaId) {
    invalidatePersonaRuntime(personaId, currentBranchId());
    if (state.runtimeRefreshTimer) clearTimeout(state.runtimeRefreshTimer);
    state.runtimeRefreshTimer = setTimeout(() => {
      state.runtimeRefreshTimer = null;
      refreshLiveInspector({ force: true });
    }, 400);
  }

  function renderLiveInspector() {
    const room = state.currentRoom;
    if (!room) return;
    const part = (room.participants || []).find(p => p.participant_id === state.inspectParticipantId);
    const snap = (room.binding_snapshots && state.inspectParticipantId && room.binding_snapshots[state.inspectParticipantId]);
    const per = part && state.personas.find(p => p.id === part.persona_id);
    const personaId = part ? part.persona_id : "";
    const cached = personaId ? state.runtimeCache.get(runtimeCacheKey(personaId, currentBranchId())) : null;
    const view = cached && cached.data ? normalizeRuntime(cached.data) : null;
    // No cache entry yet means the read has not finished; an entry with a null
    // payload means the read ran and failed.  Different states, different copy.
    const pendingNote = (!cached || state.runtimePending) ? t("stateLoading") : t("stateError");

    const head = `
      <h4>${esc((per && (per.display_name || per.id)) || personaId || "")}</h4>
      ${per ? `<p class="meta" style="margin:8px 0 16px;">${esc(per.summary || per.system_prompt || "")}</p>` : ""}`;

    // Public per-turn delta strip, e.g. "焦虑 18% → 27% +9%".  This is the
    // redacted summary produced by the appraisal service, not chain-of-thought.
    const delta = (room.stateSummaries && personaId && room.stateSummaries[personaId]) || "";
    const deltaBlock = delta
      ? `<div style="margin-bottom:14px;padding:10px;background:var(--surface);border-radius:var(--radius-sm);">
           <span class="meta">${t("stateDelta")}</span>
           <div class="meta num" style="margin-top:4px;white-space:pre-wrap;">${esc(delta)}</div>
         </div>`
      : "";

    // 1. State Tab — real runtime state, sorted by deviation from baseline.
    const stateBox = $("#insp-state");
    if (stateBox) {
      if (!personaId) {
        stateBox.innerHTML = `<p class="meta">${t("selectParticipant")}</p>`;
      } else if (!view) {
        stateBox.innerHTML = `${head}<p class="meta">${pendingNote}</p>`;
      } else {
        stateBox.innerHTML = `${head}${deltaBlock}
          <div class="meta">${t("affect")}</div>
          ${stateRows(view.emotions)}
          <div class="meta" style="margin-top:16px;">${t("needs")}</div>
          ${stateRows(view.needs)}`;
      }
    }

    // 2. Relationship Tab — real RelationshipState; zero fields are omitted.
    const relationBox = $("#insp-relation");
    if (relationBox) {
      if (!personaId) {
        relationBox.innerHTML = `<p class="meta">${t("selectParticipant")}</p>`;
      } else if (!view) {
        relationBox.innerHTML = `${head}<p class="meta">${pendingNote}</p>`;
      } else {
        relationBox.innerHTML = `${head}${relationCards(view.relationships)}`;
      }
    }

    // 3. Recall Evidence Tab
    const recallBox = $("#insp-recall");
    if (recallBox) {
      const recalls = (room.transcript && room.transcript.length) ? (room.transcript[room.transcript.length - 1].recall_ids || []) : [];
      if (recalls.length) {
        recallBox.innerHTML = recalls.map(r => `
          <div class="card" style="padding:12px;margin-bottom:8px;">
            <span class="tag ok">Memory #${esc(r)}</span>
            <p class="meta" style="margin-top:6px;">${t("verifiedRecall")}</p>
          </div>`).join("");
      } else {
        recallBox.innerHTML = `<p class="meta">${t("recallNone")}</p>`;
      }
    }

    // 4. Bindings Tab
    const bindBox = $("#insp-bind");
    if (bindBox) {
      if (snap) {
        bindBox.innerHTML = `
          <div class="stack">
            <div><span class="meta">${t("host")}</span><p>${esc(snap.agent_runtime_name || snap.agent_runtime_id)}</p></div>
            <div><span class="meta">${t("model")}</span><p>${esc(snap.model_name || snap.model_id)}</p></div>
            <div><span class="meta">${t("effort")}</span><p>${esc(snap.reasoning_effort || "default")}</p></div>
          </div>`;
      } else {
        bindBox.innerHTML = `<p class="meta">${t("bindNone")}</p>`;
      }
    }
  }

  function progressBar(name, val) {
    const pct = Math.round(val * 100);
    return `
      <div class="bar">
        <div class="bar-top"><span>${esc(name)}</span><span class="num">${pct}%</span></div>
        <div class="track"><div class="fill" style="width:${pct}%"></div></div>
      </div>`;
  }

  async function stepTurn(manualSpeakerId = null) {
    const room = state.currentRoom;
    if (!room || !["ready", "discussing"].includes(room.status) || state.isTurnBusy) return;
    state.isTurnBusy = true;
    banner(true, t("bannerSelect"));

    if ((room.protocol || "free_discussion") !== "free_discussion") {
      const latestUser = [...(room.transcript || [])].reverse().find(item => item.participant_id === "user");
      const res = await api(`/api/rooms/${room.id}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: (latestUser && latestUser.content) || room.topic || "", background: true })
      });
      if (res && res.ok) watchLiveRoom(room.id);
      else {
        state.isTurnBusy = false;
        banner(false);
        toast((res && res.error) || "协议运行启动失败");
      }
      return;
    }

    if (wsSend({
      action: "step",
      manual_speaker_id: manualSpeakerId || undefined,
      user_message: ""
    })) {
      watchLiveRoom(room.id);
      return;
    }

    const payload = {};
    if (manualSpeakerId) payload.manual_speaker_id = manualSpeakerId;

    const res = await api(`/api/rooms/${room.id}/step`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (res && res.ok) {
      const events = (res.data && res.data.events) || [];
      events.forEach(e => handleRoomEvent(e));
      const terminal = events.some((e) => ["turn_completed", "agent_error"].includes(e.event || e.type));
      if (!terminal) {
        state.isTurnBusy = false;
        banner(false);
        toast(t("modelSilent"));
      }
    } else {
      state.isTurnBusy = false;
      banner(false);
      toast((res && res.error) || "Agent 调用失败");
    }
  }

  function newRequestId() {
    return (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : `req-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  async function injectUserMessage() {
    const input = $("#inject");
    const text = input.value.trim();
    if (!text || !state.currentRoom || !["ready", "discussing"].includes(state.currentRoom.status)) return;
    if (state.isTurnBusy) return;
    input.value = "";
    $("#btn-inject").disabled = true;

    // client_message_id makes one submission idempotent end to end: a retry
    // after a network error can never create a second user message.
    const clientMessageId = newRequestId();
    const res = await api(`/api/rooms/${state.currentRoom.id}/inject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content: text,
        type: "external_information",
        client_message_id: clientMessageId
      })
    });
    if (!res || !res.ok) {
      toast((res && res.error) || "注入失败");
      return;
    }
    const latest = await api(`/api/rooms/${state.currentRoom.id}`);
    if (latest && latest.ok && latest.data) applyLiveRoom(latest.data);
    else renderChatFeed();
    const room = state.currentRoom;
    if (!room) return;
    if ((room.protocol || "free_discussion") !== "free_discussion") {
      // Protocol rooms: the backend starts the protocol run from the injected
      // message exactly once.  The frontend only watches and waits.
      state.isTurnBusy = true;
      banner(true, "主持人正在分析问题…");
      watchLiveRoom(room.id);
      return;
    }
    if (room.mode === "autonomous" || room.mode === "auto") {
      banner(true, t("bannerWaiting"));
      if (ROOM_UI_STATE.shouldStartLegacyAutonomous(room, transcriptHasModelOutput(room))) {
        wsSend({ action: "autonomous", max_turns: 6 });
      }
      watchLiveRoom(room.id);
    }
  }

  // ─── Clear conversation ──────────────────────────────────────────────────
  // Wipes the visible transcript for the current room.  Persona state,
  // memory, binding snapshots, round counters and protocol history stay
  // intact: this is purely a UI reset, so the same room can host a fresh
  // question without dragging the previous thread's Affect updates along.
  async function clearCurrentConversation() {
    const room = state.currentRoom;
    if (!room) return;
    if (!Array.isArray(room.transcript) || room.transcript.length === 0) return;
    const ok = window.confirm(t("clearConversationConfirm"));
    if (!ok) return;
    const btn = $("#btn-clear-chat");
    if (btn) btn.disabled = true;
    const res = await api(`/api/rooms/${room.id}/transcripts`, { method: "DELETE" });
    if (!res || !res.ok) {
      // ``Method Not Allowed`` means the web server is running an older
      // build that does not yet know the DELETE route -- the operator
      // has to restart the Python process for new routes to register.
      const hint =
        res && (res.status === 405 || /not\s*allowed/i.test(String(res.error || "")))
          ? "服务进程未重启，新路由未注册。请 Ctrl+C 停掉后重新运行 uv run persona-continuum web"
          : null;
      toast(hint || (res && res.error) || "清空失败");
      if (btn) btn.disabled = false;
      return;
    }
    // Local view: drop the in-memory window and re-render.  The server has
    // already broadcast a ``transcript_cleared`` event so other connected
    // clients get the same update.
    if (state.currentRoom && state.currentRoom.id === room.id) {
      state.currentRoom.transcript = [];
    }
    renderChatFeed();
    toast(t("clearConversationDone"));
  }

  function syncClearChatButton() {
    const btn = $("#btn-clear-chat");
    if (!btn) return;
    const room = state.currentRoom;
    const hasMessages = !!(room && Array.isArray(room.transcript) && room.transcript.length > 0);
    btn.disabled = !room || !hasMessages;
  }

  // ─── 2. Parallel World Subsystem ─────────────────────────────────────────
  async function loadWorlds() {
    const res = await api("/api/worlds");
    if (res && res.ok) {
      state.worlds = res.data || [];
      renderWorlds();
    }
  }

  function renderWorlds() {
    const grid = $("#worlds-grid");
    if (!grid) return;
    const countEl = $("#world-count");
    if (countEl) countEl.textContent = `${state.worlds.length}`;

    if (!state.worlds.length) {
      grid.innerHTML = `
        <div class="empty" style="grid-column:1/-1">
          <h3>${t("emptyWorlds")}</h3>
          <p>${t("emptyWorldsHint")}</p>
          <button class="btn btn-primary" id="empty-new-world">${t("newWorld")}</button>
        </div>`;
      const b = $("#empty-new-world");
      if (b) b.onclick = () => {
        const btn = $("#btn-new-world");
        if (btn) btn.click();
      };
      return;
    }

    grid.innerHTML = state.worlds.map((w) => `
      <article class="card interactive world-card" data-world-id="${w.id}">
        <div class="row-between" style="align-items:flex-start;">
          <h3>${esc(w.title)}</h3>
          <span class="tag ok">${esc(w.status || "active")}</span>
        </div>
        <p class="kicker">${esc(w.description)}</p>
        <div class="row-between" style="margin-top:16px;">
          <span class="meta num">Start: ${esc(w.seed ? w.seed.start_date : "2011-10-05")}</span>
          <div class="row" style="gap:8px;">
            <button class="btn btn-sm btn-ghost" style="color:var(--danger);" data-del-world="${w.id}">${t("delete")}</button>
            <button class="btn btn-sm btn-primary" data-open-world="${w.id}">${t("openWorld")}</button>
          </div>
        </div>
      </article>`).join("");

    grid.querySelectorAll(".world-card").forEach((card) => {
      card.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        const wid = card.getAttribute("data-world-id");
        if (wid) openLiveWorld(wid);
      });
    });

    grid.querySelectorAll("[data-open-world]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        openLiveWorld(el.getAttribute("data-open-world"));
      });
    });

    grid.querySelectorAll("[data-del-world]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const wid = el.getAttribute("data-del-world");
        if (!wid) return;
        confirmDlg(
          t("deleteWorldConfirm") || "确定要彻底删除该平行世界及其所有分支、时间线与快照吗？此操作无法撤销。",
          t("delete") || "删除",
          () => deleteWorldAction(wid),
          true
        );
      });
    });
  }

  async function openLiveWorld(worldId) {
    const res = await api(`/api/worlds/${worldId}`);
    if (res && res.ok) {
      state.currentWorld = res.data.world;
      const branches = res.data.branches || [];
      state.currentBranchId = branches.length ? branches[0].id : null;

      // Populate branches dropdown
      const sel = $("#world-branch-select");
      if (sel) {
        sel.innerHTML = branches.map(b => `<option value="${esc(b.id)}">${esc(b.name || b.id)}</option>`).join("");
        sel.onchange = () => {
          state.currentBranchId = sel.value;
          refreshWorldBranchState();
        };
      }

      $("#world-live-title").textContent = state.currentWorld.title || "Parallel World";
      const paused = state.currentWorld.status === "paused";
      $("#btn-step-world").disabled = paused;
      $("#btn-pause-world").hidden = paused;
      $("#btn-resume-world").hidden = !paused;
      await refreshWorldBranchState();
      showWorldSub("live");
      connectWorldWs(worldId);
    }
  }

  function connectWorldWs(worldId) {
    if (state.currentWorldWs) {
      try { state.currentWorldWs.close(); } catch (_) {}
    }
    try {
      const loc = window.location;
      const wsProto = loc.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${wsProto}//${loc.host}/api/worlds/${worldId}/ws`;
      const ws = new WebSocket(wsUrl);
      state.currentWorldWs = ws;

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          handleWorldEvent(data);
        } catch (_) {}
      };
    } catch (_) {}
  }

  function handleWorldEvent(ev) {
    const eventName = ev.event || ev.type;
    if (eventName === "world_step_started") {
      toast(`[Sim] Timestep started: ${ev.timestamp}`);
    } else if (eventName === "world_actor_proposal") {
      const p = ev.proposal || {};
      toast(`[Actor] ${p.actor || ev.actor_id}: ${p.action_type || "reasoned"}`);
    } else if (eventName === "world_event_committed") {
      const te = ev.timeline_event;
      if (te) {
        if (!state.currentWorldEvents) state.currentWorldEvents = [];
        if (!state.currentWorldEvents.some(x => x.id === te.id)) {
          state.currentWorldEvents.push(te);
          renderWorldTimeline();
        }
      }
    } else if (eventName === "world_step_completed") {
      toast(`[Sim] Timestep completed: ${ev.timestamp}`);
      refreshWorldBranchState();
    }
  }

  async function refreshWorldBranchState() {
    const wid = state.currentWorld.id;
    const bid = state.currentBranchId;
    if (!wid || !bid) return;

    const [stateRes, tlRes, actorsRes] = await Promise.all([
      api(`/api/worlds/${wid}/branches/${bid}`),
      api(`/api/worlds/${wid}/branches/${bid}/timeline`),
      api(`/api/worlds/${wid}/branches/${bid}/actors`)
    ]);

    if (stateRes && stateRes.ok) {
      state.currentWorldState = stateRes.data.current_state;
      $("#world-live-time").textContent = state.currentWorldState.timestamp || "2011-10-05";
    }
    if (tlRes && tlRes.ok) {
      state.currentWorldEvents = tlRes.data || [];
      renderWorldTimeline();
    }
    if (actorsRes && actorsRes.ok) {
      state.currentWorldActors = actorsRes.data || [];
    }

    renderWorldInspectors();
  }

  function renderWorldTimeline() {
    const feed = $("#world-timeline-feed");
    if (!feed) return;
    const countEl = $("#world-event-count");
    if (countEl) countEl.textContent = `${state.currentWorldEvents.length} events`;

    if (!state.currentWorldEvents.length) {
      feed.innerHTML = `<div class="empty"><p class="meta">${t("worldNoEvents")}</p></div>`;
      return;
    }

    feed.innerHTML = state.currentWorldEvents.map(e => `
      <div class="timeline-card ${e.cause.includes('Divergence') ? 'divergence' : e.cause.includes('Scene') ? 'scene' : 'action'}">
        <div class="timeline-header">
          <span class="tag">${esc(e.event_time)}</span>
          <span class="num meta">Confidence: ${(e.confidence * 100).toFixed(0)}%</span>
        </div>
        <div class="timeline-cause">${esc(e.cause)}</div>
        <div class="timeline-effect">${esc(e.effect)}</div>
        ${e.data && e.data.runtime ? `<p class="meta" style="margin-top:8px;">Actor: ${esc(e.data.runtime.actor_name || e.actors?.[0] || "")} · Model: ${esc(e.data.runtime.model_id || "")} · Agent: ${esc(e.data.runtime.agent_id || "")} · Decision source: ${esc(e.data.decision_source || "llm")}</p>` : ""}
        <div class="row" style="margin-top:8px;gap:6px;">
          ${(e.actors || []).map(a => `<span class="tag solid" style="font-size:10px;">${esc(a)}</span>`).join("")}
        </div>
      </div>`).join("");
    feed.scrollTop = feed.scrollHeight;
  }

  function renderWorldInspectors() {
    const ws = state.currentWorldState;
    if (!ws) return;

    // 1. Strategic Projects
    const pBox = $("#world-insp-projects");
    if (pBox) {
      const projs = Object.entries(ws.active_projects || {});
      pBox.innerHTML = projs.length ? projs.map(([pid, p]) => `
        <div class="card" style="padding:14px;margin-bottom:10px;">
          <div class="row-between"><strong>${esc(p.name || pid)}</strong><span class="tag ${p.status === 'active' ? 'ok' : ''}">${esc(p.status)}</span></div>
          <p class="meta" style="margin:4px 0 8px;">Owner: ${esc(p.owner)} · Budget: $${esc(p.budget_billions || 0)}B</p>
          ${progressBar("Progress", (p.progress_percent || 0) / 100)}
        </div>`).join("") : `<p class="meta">${t("worldNoProjects")}</p>`;
    }

    // 2. Organizations
    const oBox = $("#world-insp-orgs");
    if (oBox) {
      const orgs = Object.entries(ws.organizations || {});
      oBox.innerHTML = orgs.map(([oid, o]) => `
        <div class="card" style="padding:14px;margin-bottom:10px;">
          <div class="row-between"><strong>${esc(o.name || oid)}</strong><span class="tag">Org</span></div>
          <p class="meta" style="margin:6px 0;">${t("cashReserves")}：<strong class="num">$${esc(o.cash_reserves_billions || 0)}B</strong></p>
          <p class="meta">Engineers: <strong class="num">${esc(o.headcount_engineers || 0)}</strong></p>
        </div>`).join("");
    }

    // 3. Tech & Markets
    const tBox = $("#world-insp-tech");
    if (tBox) {
      const techs = Object.entries(ws.technologies || {});
      tBox.innerHTML = techs.map(([tid, t]) => `
        <div class="card" style="padding:14px;margin-bottom:10px;">
          <div class="row-between"><strong>${esc(tid)}</strong><span class="tag ok">${esc(t.maturity_level || "concept")}</span></div>
        </div>`).join("");
    }

    // 4. Actors
    const aBox = $("#world-insp-actors");
    if (aBox) {
      aBox.innerHTML = state.currentWorldActors.map(a => `
        <div class="card" style="padding:14px;margin-bottom:10px;">
          <div class="row-between"><strong>${esc(a.name)}</strong><span class="tag">${esc(a.actor_type)}</span></div>
          <p class="kicker" style="margin-top:6px;">${esc(a.goals ? a.goals.join("; ") : "")}</p>
        </div>`).join("");
    }
  }

  // ─── 3. Agents, API, Personas ────────────────────────────────────────────
  async function loadAgents(forceRescan = false) {
    state.agentsLoading = true;
    try {
      const url = forceRescan ? "/api/agents/rescan" : "/api/agents";
      const res = await api(url, forceRescan ? { method: "POST" } : {});
      if (res && res.ok) {
        state.agents = res.data || [];
        renderAgents();
        renderApiProfiles();
        if (state.roomSub === "lobby") {
          renderSlots();
          renderBindingPreview();
        }
        if (state.worldSub === "create") {
          if (typeof updateWorldCreateRuntimes === "function") {
            updateWorldCreateRuntimes();
          }
        }
        if (typeof refreshOpenNarrativeRuntimeModal === "function") {
          refreshOpenNarrativeRuntimeModal();
        }
        return true;
      }
      return false;
    } finally {
      state.agentsLoading = false;
    }
  }

  // Click handlers must never stall on a cold discovery scan: dialogs open
  // immediately with a "scanning" placeholder and this re-runs their runtime
  // selector setup once agents land (or the scan fails, which restores the
  // honest empty-state message).
  async function hydrateRuntimeSelectors(resetup) {
    try {
      await loadAgents(false);
    } catch (err) {
      console.error("runtime selector hydration failed:", err);
    }
    resetup();
  }

  async function revalidateAgentResearch(agent, button) {
    if (!agent || !agent.id) return;
    if (button) {
      button.disabled = true;
      button.textContent = "验证中…";
    }
    try {
      const models = getRuntimeModels(agent);
      const res = await api(`/api/agents/${encodeURIComponent(agent.id)}/research/revalidate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: models[0] ? models[0].id : null })
      });
      if (!res || !res.ok) throw new Error((res && res.error) || "联网研究能力验证失败");
      const research = (res.data && res.data.research) || {};
      await loadAgents(false);
      toast(research.verification_status === "verified"
        ? "联网研究能力已验证"
        : (research.verification_error || "联网研究能力状态已更新"));
    } catch (err) {
      toast(err && err.message ? err.message : String(err));
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "重新验证联网研究";
      }
    }
  }

  function renderAgents() {
    const grid = $("#agents-grid");
    if (!grid) return;
    const list = state.agents.filter(a => state.agentFilter === "all" || a.status === state.agentFilter);

    if (!list.length) {
      grid.innerHTML = `
        <div class="empty" style="grid-column:1/-1">
          <h3>${state.agents.length ? t("emptyAgents") : t("scanning")}</h3>
          <p>${state.agents.length ? t("emptyAgentsHint") : t("agentsLead")}</p>
        </div>`;
      return;
    }

    grid.innerHTML = list.map(a => {
      const srcLabel = a.runtime_source === "local_cli" ? "Local CLI" : (a.runtime_source === "api" ? "API Provider" : "Test Mock");
      const defLabel = a.definition_source === "manifest" ? "Manifest" : (a.definition_source === "plugin" ? "Plugin" : (a.definition_source === "dynamic_api" ? "Dynamic API" : "Builtin"));
      const isReady = a.status === "ready";
      const statusMeta = {
        ready: { cls: "ok", label: t("ready") },
        auth_required: { cls: "warn", label: t("authNeeded") },
        detected: { cls: "", label: t("statusDetected") },
        detected_uncontrollable: { cls: "", label: t("statusDetected") },
        unsupported_version: { cls: "warn", label: t("statusBroken") },
        broken: { cls: "danger", label: t("statusBroken") },
        disabled: { cls: "", label: t("statusDisabled") },
      }[a.status] || { cls: "warn", label: esc(a.status || "unknown") };
      const research = a.research || {};
      const researchStatus = research.verification_status || "unknown";
      const researchLabel = researchStatus === "verified"
        ? "联网研究：已验证 ✓"
        : researchStatus === "declared"
          ? "联网研究：运行时声明支持，首次运行将验证"
          : researchStatus === "blocked"
            ? "联网研究：被当前策略阻止"
          : researchStatus === "unavailable"
            ? "联网研究：验证不可用"
            : a.runtime_source === "local_cli" && isReady
              ? "联网研究：尚未验证，首次使用时将自动测试原生能力"
              : "联网研究：未验证";
      const canDiscover = research.can_discover_sources ?? research.discovers_sources ?? research.search;
      const canRead = research.can_read_sources ?? research.reads_sources ?? research.fetch;

      return `
      <article class="card agent-card" data-agent-id="${a.id}">
        <div class="row-between" style="align-items:flex-start;">
          <div>
            <h3 style="margin-bottom:4px;">${esc(a.name)}</h3>
            <p class="meta num" style="font-size:12px;color:var(--muted);">${esc(a.id)}</p>
          </div>
          <div class="row" style="gap:6px;">
            <span class="tag" style="font-size:11px;">${defLabel}</span>
            <span class="tag ${statusMeta.cls}">${statusMeta.label}</span>
          </div>
        </div>

        <div class="meta num" style="margin:10px 0 8px;font-size:11px;display:flex;flex-direction:column;gap:3px;background:var(--surface);padding:8px 10px;border-radius:var(--radius-sm);border:1px solid var(--border-soft);">
          <div><strong style="color:var(--text);">Runtime Source:</strong> <span style="font-family:var(--font-mono);">${esc(a.runtime_source || "local_cli")}</span></div>
          <div><strong style="color:var(--text);">Definition Source:</strong> <span style="font-family:var(--font-mono);">${esc(a.definition_source || "builtin")}</span></div>
          <div style="word-break:break-all;"><strong style="color:var(--text);">Path / URL:</strong> <span style="font-family:var(--font-mono);">${esc(a.binary_path || "None")}</span></div>
          <div><strong style="color:var(--text);">Version:</strong> ${esc(a.version || "detected")}</div>
        </div>

        <p class="kicker" style="margin:6px 0;">${t("protocols")}: ${esc((a.protocols || []).join(", "))}</p>
        <div class="meta" style="margin:8px 0 10px;padding:8px 10px;border:1px solid var(--border-soft);border-radius:var(--radius-sm);">
          <strong>${esc(researchLabel)}</strong>
          <div style="font-size:11px;margin-top:4px;">Method: ${esc(research.verification_method || "not_verified")} · Search: ${canDiscover ? "yes" : "no"} · Read: ${canRead ? "yes" : "no"}</div>
          ${research.verified_at ? `<div style="font-size:11px;">Last verified: ${esc(research.verified_at)}</div>` : ""}
          ${research.verification_error ? `<div class="error" style="font-size:11px;margin-top:4px;">${esc(research.verification_error)}</div>` : ""}
        </div>
        <div class="row" style="flex-wrap:wrap;gap:6px;margin-bottom:12px;">
          ${(() => {
            const canListModels = a.status !== "disabled" && a.status !== "broken";
            const allModels = canListModels ? (a.models || []) : [];
            const initialLimit = 5;
            if (!allModels.length) {
              return `<span class="meta">${t("models")} 0</span>`;
            }
            if (allModels.length <= initialLimit) {
              return allModels.map(m => `<span class="tag">${esc(m.display_name || m.id)}</span>`).join("");
            }
            const visible = allModels.slice(0, initialLimit);
            const hidden = allModels.slice(initialLimit);
            const expandText = state.lang === "en" ? `+ ${hidden.length} more` : `+ 展开剩余 ${hidden.length} 个模型`;
            const collapseText = state.lang === "en" ? "收起" : "收起";
            return visible.map(m => `<span class="tag">${esc(m.display_name || m.id)}</span>`).join("")
              + hidden.map(m => `<span class="tag agent-extra-tag" data-agent-models="${esc(a.id)}" style="display:none;">${esc(m.display_name || m.id)}</span>`).join("")
              + `<button type="button" class="btn btn-sm btn-ghost btn-toggle-models" data-agent-toggle="${esc(a.id)}" data-expand-label="${esc(expandText)}" data-collapse-label="${esc(collapseText)}" style="padding:2px 8px;font-size:11px;color:var(--accent);height:22px;line-height:1.2;cursor:pointer;border:1px dashed var(--border-soft);">${esc(expandText)}</button>`;
          })()}
        </div>
        <div class="row-between" style="margin-top:auto;padding-top:8px;">
          <div class="row" style="gap:6px;flex-wrap:wrap;">
            <button class="btn btn-sm btn-ghost" data-test-agent="${a.id}">${t("test")}</button>
            ${a.runtime_source === "local_cli" && isReady ? `<button class="btn btn-sm btn-ghost" data-revalidate-research="${esc(a.id)}">重新验证联网研究</button>` : ""}
          </div>
          <span class="meta" style="font-size:11px;color:var(--muted);">${isReady ? "就绪可用于房间推演" : (a.status_detail || "未就绪")}</span>
        </div>
      </article>`;
    }).join("");

    grid.querySelectorAll("[data-test-agent]").forEach(b => {
      b.addEventListener("click", () => toast(`${t("tested")} · ${b.dataset.testAgent}`));
    });
    grid.querySelectorAll("[data-revalidate-research]").forEach(button => {
      button.addEventListener("click", () => {
        const agent = state.agents.find(item => item.id === button.dataset.revalidateResearch);
        revalidateAgentResearch(agent, button);
      });
    });
    grid.querySelectorAll("[data-agent-toggle]").forEach(btn => {
      btn.addEventListener("click", () => {
        const agentId = btn.dataset.agentToggle;
        const extraTags = grid.querySelectorAll(`.agent-extra-tag[data-agent-models="${CSS.escape(agentId)}"]`);
        const isCollapsed = btn.getAttribute("data-expanded") !== "true";
        if (isCollapsed) {
          extraTags.forEach(tag => { tag.style.display = ""; });
          btn.textContent = btn.dataset.collapseLabel || (state.lang === "en" ? "Collapse" : "收起");
          btn.setAttribute("data-expanded", "true");
        } else {
          extraTags.forEach(tag => { tag.style.display = "none"; });
          btn.textContent = btn.dataset.expandLabel || `+ ${extraTags.length}`;
          btn.setAttribute("data-expanded", "false");
        }
      });
    });
  }

  async function loadApiProfiles() {
    const res = await api("/api/auth-profiles");
    if (res && res.ok) {
      state.apiProfiles = res.data || [];
      renderApiProfiles();
    }
  }

  function renderApiProfiles() {
    const grid = $("#api-grid");
    if (!grid) return;
    if (!state.apiProfiles.length) {
      grid.innerHTML = `
        <div class="empty" style="grid-column:1/-1">
          <h3>${t("emptyApi")}</h3>
          <p>${t("emptyApiHint")}</p>
          <button class="btn btn-primary" id="empty-add-api">${t("addApi")}</button>
        </div>`;
      const b = $("#empty-add-api");
      if (b) b.onclick = () => {
        resetApiProfileEditor();
        $("#dlg-api").showModal();
      };
      return;
    }

    grid.innerHTML = state.apiProfiles.map(p => {
      const reasoningSummary = apiProfileReasoningSummary(p);
      return `
      <article class="card">
        <div class="row-between"><h3>${esc(p.name)}</h3><span class="tag">${esc(p.default_model || "API")}</span></div>
        <p class="meta num" style="margin:8px 0;">${esc(p.base_url || "")}</p>
        <p class="kicker">${esc(p.credential_status || "not_configured")} · ${esc(p.api_key_hint || "")}</p>
        ${(p.connection_models || []).length ? `<p class="meta">模型：${esc(p.connection_models.slice(0, 4).join(", "))}${p.connection_models.length > 4 ? ` · 另有 ${p.connection_models.length - 4} 个` : ""}</p>` : ""}
        <p class="meta" style="margin-top:6px;">${esc(reasoningSummary.label)}</p>
        <div class="row-between" style="margin-top:12px;">
          <button class="btn btn-sm btn-ghost" data-test-api="${p.id}">${t("test")}</button>
          <button class="btn btn-sm btn-ghost" data-edit-api="${p.id}">配置</button>
          <button class="btn btn-sm btn-ghost" style="color:var(--danger);" data-del-api="${p.id}">${t("delete")}</button>
        </div>
      </article>`;
    }).join("");

    grid.querySelectorAll("[data-test-api]").forEach(b => {
      b.addEventListener("click", async () => {
        const pid = b.dataset.testApi;
        b.disabled = true;
        b.textContent = "探测中…";
        toast("正在探测模型与 Reasoning 能力...");
        const res = await api(`/api/providers/${pid}/test`, { method: "POST" });
        if (res && res.ok && res.data) {
          const d = res.data;
          if (d.status === "connected") {
            const profile = state.apiProfiles.find(p => p.id === pid);
            if (profile) profile.connection_models = d.models || [];
            await loadAgents(true);
            const summary = apiProfileReasoningSummary(profile);
            renderApiProfiles();
            toast(`Connected · ${d.latency}ms · Reasoning ${summary.reportedCount}/${summary.modelCount}`);
          } else {
            toast(`探测失败: ${d.error || "无法连接模型端点"}`);
            renderApiProfiles();
          }
        } else {
          const err = (res && res.error) || "网络或服务端错误";
          toast(`探测失败: ${err}`);
          renderApiProfiles();
        }
      });
    });
    grid.querySelectorAll("[data-edit-api]").forEach(b => {
      b.addEventListener("click", () => {
        const profile = state.apiProfiles.find(item => item.id === b.dataset.editApi);
        if (profile) openApiProfileEditor(profile);
      });
    });
    grid.querySelectorAll("[data-del-api]").forEach(b => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const apid = b.dataset.delApi;
        if (!apid) return;
        confirmDlg(t("deleteApi"), t("delete"), async () => {
          await api(`/api/auth-profiles/${apid}`, { method: "DELETE" });
          state.apiProfiles = state.apiProfiles.filter(p => p.id !== apid);
          renderApiProfiles();
          toast(t("deletedApiSuccess"));
        }, true);
      });
    });
  }

  function apiProfileReasoningModel(profile) {
    const metadata = profile && profile.metadata;
    const capabilities = metadata && metadata.model_capabilities;
    if (!capabilities || typeof capabilities !== "object") return null;
    const entry = capabilities[profile.default_model] || capabilities.default;
    if (!entry || typeof entry !== "object") return null;
    const capability = entry.reasoning_capability || entry;
    return capability && typeof capability === "object"
      ? { reasoning_capability: capability }
      : null;
  }

  function apiProfileRuntimeAgent(profile) {
    if (!profile || !profile.id) return null;
    return state.agents.find(agent => agent.id === `api_${profile.id}`) || null;
  }

  function apiProfileReasoningSummary(profile) {
    const agent = apiProfileRuntimeAgent(profile);
    const models = getRuntimeModels(agent);
    if (!models.length) {
      return {
        label: reasoningCapabilityLabel(apiProfileReasoningModel(profile)),
        reportedCount: 0,
        modelCount: 0,
      };
    }
    const reported = models.filter(model => getRuntimeReasoningOptions(agent, model).length);
    if (!reported.length) {
      return {
        label: `Reasoning：已扫描 ${models.length} 个模型，均未报告可选档位`,
        reportedCount: 0,
        modelCount: models.length,
      };
    }
    const manualCount = reported.filter(model => getReasoningCapability(model).mode === "manual_config").length;
    const efforts = Array.from(new Set(reported.flatMap(model => getRuntimeReasoningOptions(agent, model))));
    const provenance = manualCount === reported.length ? "手动配置" : "模型端点已验证";
    return {
      label: `Reasoning：${provenance} · ${reported.length}/${models.length} 个模型 · ${efforts.join(", ")}`,
      reportedCount: reported.length,
      modelCount: models.length,
    };
  }

  function openApiProfileEditor(profile) {
    state.apiEditingId = profile.id;
    $("#api-name").value = profile.name || "";
    $("#api-url").value = profile.base_url || "";
    $("#api-provider").value = profile.provider_type === "google"
      ? "gemini"
      : (profile.provider_type || "openai_compatible");
    $("#api-env").value = "";
    $("#api-model").value = profile.default_model || "";
    const model = apiProfileReasoningModel(profile);
    const capability = getReasoningCapability(model);
    const mode = $("#api-reasoning-mode");
    if (mode) {
      mode.value = capability.mode === "manual_config"
        ? "manual"
        : capability.mode === "default_only" ? "default" : "auto";
    }
    $("#api-reasoning-efforts").value = capability.supported_efforts.join(", ");
    $("#api-reasoning-default").value = capability.default_effort || "";
    syncApiReasoningFields();
    const title = $("#dlg-api h3");
    if (title) title.textContent = "编辑提供方";
    const error = $("#api-error");
    if (error) error.hidden = true;
    $("#dlg-api").showModal();
  }

  function resetApiProfileEditor() {
    state.apiEditingId = null;
    const title = $("#dlg-api h3");
    if (title) title.textContent = t("addApi");
    $("#form-api").reset();
    const error = $("#api-error");
    if (error) error.hidden = true;
    syncApiReasoningFields();
  }

  function syncApiReasoningFields() {
    const mode = $("#api-reasoning-mode");
    const manual = $("#api-reasoning-manual-wrap");
    if (manual) manual.hidden = !mode || mode.value !== "manual";
  }

  function apiReasoningMetadata() {
    const mode = $("#api-reasoning-mode")?.value || "auto";
    if (mode === "default") {
      return {
        mode: "default_only",
        supported_efforts: [],
        default_effort: null,
        binding_strategy: "provider_default",
        verified: false,
        source: "manual_ui",
      };
    }
    if (mode !== "manual") {
      return {
        mode: "unknown",
        supported_efforts: [],
        default_effort: null,
        binding_strategy: "unknown",
        verified: false,
        source: "automatic_detection",
      };
    }
    const effortPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
    const efforts = Array.from(new Map(
      ($( "#api-reasoning-efforts")?.value || "")
        .split(",")
        .map(value => value.trim())
        .filter(Boolean)
        .map(value => [value.toLowerCase(), value])
    ).values());
    const invalid = efforts.filter(value => !effortPattern.test(value));
    if (invalid.length) {
      throw new Error(`Reasoning 选项格式无效：${invalid.join(", ")}`);
    }
    if (!efforts.length) throw new Error("手动配置至少填写一个已确认的 Reasoning 选项");
    const rawDefault = ($( "#api-reasoning-default")?.value || "").trim();
    const defaultEffort = rawDefault
      ? efforts.find(value => value.toLowerCase() === rawDefault.toLowerCase()) || null
      : null;
    if (rawDefault && !defaultEffort) {
      throw new Error("默认 Reasoning 必须包含在支持选项中");
    }
    return {
      mode: "manual_config",
      supported_efforts: efforts,
      default_effort: defaultEffort,
      binding_strategy: "manual_config",
      verified: false,
      source: "manual_ui",
    };
  }

  async function loadPersonas() {
    const res = await api("/api/personas");
    if (res && res.ok) {
      state.personas = res.data || [];
      // The legacy Persona endpoint remains available to Tavern and room
      // bindings.  The library itself is loaded from the unified Profile API.
      renderPersonas();
      return true;
    }
    state.personas = [];
    renderPersonas();
    return false;
  }

  async function loadProfiles() {
    const params = new URLSearchParams();
    if (state.profileType) params.set("type", state.profileType);
    if (state.profileStatus) params.set("status", state.profileStatus);
    if (state.personaQ.trim()) params.set("q", state.personaQ.trim());
    params.set("sort", state.profileSort || "updated_at");
    const res = await api(`/api/profiles?${params.toString()}`);
    if (res && res.ok) {
      state.profiles = res.data || [];
      renderPersonas();
      return true;
    }
    state.profiles = [];
    renderPersonas();
    return false;
  }

  const BACKGROUND_TERMINAL = new Set([
    "completed",
    "completed_with_gaps",
    "failed",
    "failed_quality_gate",
    "cancelled",
  ]);
  const BACKGROUND_FAILED = new Set(["failed", "failed_quality_gate"]);

  function backgroundJobKind(job) {
    return job.job_kind || (job.creation_mode || job.persona_id ? "persona_creation" : "profile_enrichment");
  }

  function backgroundJobEndpoint(job, suffix = "") {
    const prefix = backgroundJobKind(job) === "persona_creation" ? "/api/persona-creation/jobs" : "/api/profile-enrichment/jobs";
    return `${prefix}/${encodeURIComponent(job.id)}${suffix}`;
  }

  function backgroundAgentDiagnostics(job) {
    const failure = job && (job.failure_json || ((job.progress || {}).failure));
    const failureDiagnostics = (failure && failure.diagnostics) || {};
    const progress = (job && job.progress) || {};
    const audits = (job && (job.agent_call_audits || ((job.progress || {}).agent_calls_audit))) || [];
    const latestAudit = Array.isArray(audits) && audits.length ? audits[audits.length - 1] : {};
    const auditDiagnostics = (latestAudit && latestAudit.runtime_diagnostics) || {};
    const selected = (job && job.selected_runtime) || {};
    const pick = (...values) => values.find(value => value !== undefined && value !== null && value !== "");
    const pickObject = (...values) => values.find(value => value && typeof value === "object") || {};
    const binding = pickObject(
      failureDiagnostics.runtime_binding_snapshot,
      auditDiagnostics.runtime_binding_snapshot,
      progress.runtime_binding_snapshot,
      job && job.job_config && job.job_config.runtime_binding_snapshot,
      selected.runtime_binding_snapshot,
    );
    const activity = pickObject(
      failureDiagnostics.activity_tracker,
      auditDiagnostics.activity_tracker,
      progress.activity_tracker,
      (progress.child_snapshot || {}).runtime_diagnostics &&
        (progress.child_snapshot || {}).runtime_diagnostics.activity_tracker,
    );
    const countValues = [
      job && job.agent_call_count,
      progress.agent_call_count,
      progress.child_agent_call_count,
    ].map(value => Number(value || 0)).filter(Number.isFinite);
    return {
      agent: pick(failureDiagnostics.adapter, selected.agent_id, latestAudit.agent_id),
      protocol: pick(failureDiagnostics.protocol, failure && failure.protocol, auditDiagnostics.protocol, latestAudit.protocol),
      requestedModel: pick(failureDiagnostics.requested_model, failure && failure.requested_model, auditDiagnostics.requested_model, selected.model_id),
      effectiveModel: pick(failureDiagnostics.effective_model, failure && failure.effective_model, auditDiagnostics.effective_model),
      requestedReasoning: pick(failureDiagnostics.requested_reasoning, failure && failure.requested_reasoning, auditDiagnostics.requested_reasoning, selected.reasoning_effort),
      effectiveReasoning: pick(failureDiagnostics.effective_reasoning, failure && failure.effective_reasoning, auditDiagnostics.effective_reasoning),
      promptMode: pick(failureDiagnostics.prompt_mode, auditDiagnostics.prompt_mode),
      structuredMode: pick(failureDiagnostics.structured_output_mode, auditDiagnostics.structured_output_mode),
      inputTokens: pick(failureDiagnostics.input_token_estimate, auditDiagnostics.input_token_estimate),
      lastActivity: pick(failureDiagnostics.last_activity_at, auditDiagnostics.last_activity_at),
      recentTransportActivity: pick(
        activity.last_transport_activity,
        failureDiagnostics.last_activity_at,
        auditDiagnostics.last_activity_at,
      ),
      lastOutputActivity: pick(activity.last_output_activity),
      outputMode: pick(
        failureDiagnostics.output_streaming_mode,
        auditDiagnostics.output_streaming_mode,
      ),
      firstResponseTimeout: pick(
        failureDiagnostics.first_response_timeout_seconds,
        auditDiagnostics.first_response_timeout_seconds,
      ),
      idleTimeout: pick(failureDiagnostics.idle_timeout_seconds, auditDiagnostics.idle_timeout_seconds),
      hardTimeout: pick(failureDiagnostics.hard_timeout_seconds, auditDiagnostics.hard_timeout_seconds),
      stdoutBytes: pick(activity.stdout_bytes),
      stderrBytes: pick(activity.stderr_bytes),
      protocolFrames: pick(activity.protocol_frames),
      processAlive: pick(
        failureDiagnostics.process_alive,
        auditDiagnostics.process_alive,
        progress.process_alive,
      ),
      workerState: pick(
        job && job.worker_state,
        progress.worker_state,
        progress.child_worker_state,
        (progress.child_snapshot || {}).worker_state,
      ),
      workerHeartbeat: pick(
        job && job.worker_heartbeat_at,
        progress.worker_heartbeat_at,
        progress.child_worker_heartbeat_at,
        (progress.child_snapshot || {}).worker_heartbeat_at,
      ),
      childJobId: pick(
        job && job.persona_creation_job_id,
        progress.child_job_id,
        progress.persona_job_id,
      ),
      agentCallCount: countValues.length ? Math.max(...countValues) : 0,
      bindingMethod: pick(binding.verification_method, binding.binding_strategy),
      bindingStatus: pick(binding.binding_status),
      reasoningVerified: pick(binding.reasoning_verified),
    };
  }

  function backgroundAgentDiagnosticsText(job) {
    const diag = backgroundAgentDiagnostics(job);
    return [
      diag.agent ? `Agent: ${diag.agent}` : "",
      diag.protocol ? `Protocol: ${diag.protocol}` : "",
      diag.requestedModel ? `Requested Model: ${diag.requestedModel}` : "",
      diag.effectiveModel ? `Effective Model: ${diag.effectiveModel}` : "",
      diag.requestedReasoning ? `Requested Reasoning: ${diag.requestedReasoning}` : "",
      diag.effectiveReasoning ? `Effective Reasoning: ${diag.effectiveReasoning}` : "",
      diag.promptMode ? `Prompt: ${diag.promptMode}` : "",
      diag.structuredMode ? `Structured: ${diag.structuredMode}` : "",
      diag.inputTokens !== undefined ? `Input: ${diag.inputTokens} tokens` : "",
      diag.lastActivity ? `Last activity: ${diag.lastActivity}` : "",
      diag.recentTransportActivity ? `Transport: ${diag.recentTransportActivity}` : "",
      diag.lastOutputActivity ? `Output: ${diag.lastOutputActivity}` : "",
      diag.outputMode ? `Output mode: ${diag.outputMode}` : "",
      diag.firstResponseTimeout !== undefined ? `First: ${diag.firstResponseTimeout}s` : "",
      diag.idleTimeout !== undefined ? `Idle: ${diag.idleTimeout}s` : "",
      diag.hardTimeout !== undefined ? `Hard: ${diag.hardTimeout}s` : "",
      diag.stdoutBytes !== undefined ? `stdout: ${diag.stdoutBytes} B` : "",
      diag.stderrBytes !== undefined ? `stderr: ${diag.stderrBytes} B` : "",
      diag.protocolFrames !== undefined ? `Frames: ${diag.protocolFrames}` : "",
      diag.processAlive !== undefined && diag.processAlive !== null ? `Process: ${diag.processAlive ? "alive" : "exited"}` : "",
      diag.workerState ? `Worker: ${diag.workerState}` : "",
      diag.workerHeartbeat ? `Heartbeat: ${diag.workerHeartbeat}` : "",
      diag.childJobId ? `Child job: ${diag.childJobId}` : "",
      diag.agentCallCount !== undefined ? `Agent calls: ${diag.agentCallCount}` : "",
      diag.bindingMethod ? `Binding: ${diag.bindingMethod}` : "",
      diag.reasoningVerified !== undefined ? `Reasoning verified: ${diag.reasoningVerified ? "yes" : "no"}` : "",
    ].filter(Boolean).join(" · ");
  }

  function updateBackgroundJobCounts() {
    const counts = state.backgroundJobCounts || {};
    const activeEl = $("#profile-task-active-count");
    const failedEl = $("#profile-task-failed-count");
    if (activeEl) activeEl.textContent = `进行中：${Number(counts.badge ?? counts.active ?? 0)}`;
    if (failedEl) failedEl.textContent = `失败：${Number(counts.failed || 0)}`;
    const pageEl = $("#profile-task-page-label");
    if (pageEl) pageEl.textContent = `结束任务历史：第 ${state.backgroundTerminalPage} 页`;
    const terminalCount = Number(counts.terminal || 0);
    const next = $("#profile-task-next");
    const prev = $("#profile-task-prev");
    if (next) next.disabled = state.backgroundTerminalPage * state.backgroundTerminalPageSize >= terminalCount;
    if (prev) prev.disabled = state.backgroundTerminalPage <= 1;
  }

  async function viewBackgroundJob(job) {
    try {
      if (backgroundJobKind(job) === "persona_creation") {
        state.personaCreationJob = job;
        openPersonaCreationProgressDialog(job);
        if (state.personaCreationPoll) clearInterval(state.personaCreationPoll);
        if (!BACKGROUND_TERMINAL.has(job.status)) {
          state.personaCreationPoll = setInterval(() => pollPersonaCreationJob(job.id), 1000);
        }
      } else {
        await openProfileEnrichmentProgress(job);
      }
    } catch (err) {
      console.error("Background job view failed:", err);
      toast(`无法打开任务：${err && err.message ? err.message : err}`);
    }
  }

  async function retryBackgroundJob(job) {
    const res = await api(backgroundJobEndpoint(job, "/retry"), { method: "POST" });
    if (!res || !res.ok) throw new Error((res && res.error) || "任务重试失败");
    await loadBackgroundJobs();
  }

  async function dismissBackgroundJob(job) {
    if (!window.confirm("只会从后台任务中心移除记录，不会删除已创建的人格、证据或版本。")) return;
    const res = await api(backgroundJobEndpoint(job), { method: "DELETE" });
    if (!res || !res.ok) throw new Error((res && res.error) || "删除任务记录失败");
    state.backgroundJobs = state.backgroundJobs.filter(item => item.id !== job.id);
    renderBackgroundJobs();
    await loadBackgroundJobs();
  }

  function renderBackgroundJobs() {
    const box = $("#profile-task-list");
    if (!box) return;
    updateBackgroundJobCounts();
    const filter = state.backgroundJobFilter || "all";
    const jobs = (state.backgroundJobs || []).filter(job => {
      if (filter === "active") return !BACKGROUND_TERMINAL.has(job.status);
      if (filter === "failed") return BACKGROUND_FAILED.has(job.status);
      if (filter === "completed") return ["completed", "completed_with_gaps", "cancelled"].includes(job.status);
      return true;
    });
    if (!jobs.length) {
      box.innerHTML = '<p class="meta">暂无符合条件的后台任务</p>';
      return;
    }
    box.innerHTML = jobs.map(job => {
      const kind = backgroundJobKind(job);
      const progress = job.progress || {};
      const percent = Number(progress.percent ?? (["completed", "completed_with_gaps"].includes(job.status) ? 100 : 0));
      const title = job.display_name || job.target_profile_id || job.id;
      const failure = job.failure_json || progress.failure;
      const status = String(job.status || "queued");
      const statusLabel = status === "failed_quality_gate"
        ? "质量门禁未通过"
        : (progress.label || progress.stage || status);
      const failureRuntime = (failure && failure.runtime) || job.selected_runtime || {};
      const diagnostics = backgroundAgentDiagnostics(job);
      const agentDiagnostics = backgroundAgentDiagnosticsText(job);
      const operation = String(progress.current_operation || progress.stage || job.current_stage || "");
      const noAgentCallWarning = !BACKGROUND_TERMINAL.has(status)
        && diagnostics.agentCallCount === 0
        && /agent|research|reason|classif|relation|fusion|extract|compil/i.test(operation);
      const failureMeta = failure ? [
        failure.phase ? `Stage: ${failure.phase}` : "",
        failureRuntime.agent_id ? `Agent: ${failureRuntime.agent_id}` : "",
        failureRuntime.model_id ? `Model: ${failureRuntime.model_id}` : "",
        failure.protocol ? `Protocol: ${failure.protocol}` : "",
        job.retry_available === true || failure.retriable === true
          ? "Retryability: retriable"
          : "Retryability: not retriable",
        failure.frame_type ? `Frame: ${failure.frame_type}` : "",
        failure.event_type ? `Event: ${failure.event_type}` : "",
        failure.frame_bytes || failure.observed_frame_bytes
          ? `Bytes: ${failure.frame_bytes || failure.observed_frame_bytes}`
          : "",
      ].filter(Boolean).join(" · ") : "";
      const viewLabel = BACKGROUND_FAILED.has(status) ? "查看原因" : "查看";
      let actions = `<button type="button" class="btn btn-text" data-background-action="view" data-background-id="${esc(job.id)}">${viewLabel}</button>`;
      if (BACKGROUND_FAILED.has(status)) {
        if (job.retry_available === true || (failure && failure.retriable === true)) {
          actions += ` <button type="button" class="btn btn-text" data-background-action="retry" data-background-id="${esc(job.id)}">重试</button>`;
        }
        actions += ` <button type="button" class="btn btn-text" data-background-action="dismiss" data-background-id="${esc(job.id)}">删除</button>`;
      } else if (BACKGROUND_TERMINAL.has(status)) {
        actions += ` <button type="button" class="btn btn-text" data-background-action="dismiss" data-background-id="${esc(job.id)}">删除</button>`;
      } else if (["paused", "paused_runtime_unavailable"].includes(status)) {
        actions += ` <button type="button" class="btn btn-text" data-background-action="resume" data-background-id="${esc(job.id)}">继续</button>`;
        actions += ` <button type="button" class="btn btn-text" data-background-action="cancel" data-background-id="${esc(job.id)}">取消任务</button>`;
      } else {
        actions += ` <button type="button" class="btn btn-text" data-background-action="cancel" data-background-id="${esc(job.id)}">取消任务</button>`;
      }
      return `<div class="row-between" style="gap:12px;align-items:center;border-top:1px solid var(--border-soft);padding-top:8px;">
        <div style="min-width:0;flex:1;"><strong>${esc(title)}</strong><span class="tag" style="margin-left:6px;">${esc(kind === "persona_creation" ? "Persona" : "Profile")}</span>
          <p class="meta" style="margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(statusLabel)} · ${esc(job.id)}</p>
          ${failure ? `<p class="error" style="font-size:11px;margin-top:3px;">${esc(failure.code || job.error || "任务失败")}：${esc(failure.message || job.error || "")} ${esc(failureMeta)}</p>` : ""}
          ${agentDiagnostics ? `<p class="meta" style="font-size:11px;margin-top:3px;">${esc(agentDiagnostics)}</p>` : ""}
          ${noAgentCallWarning ? `<p class="meta" style="font-size:11px;margin-top:3px;color:var(--warning, #a66a00);">⚠ 尚未记录 Agent Call；当前可能仍在启动或等待 Agent。</p>` : ""}
          ${job.superseded_by ? `<p class="meta" style="font-size:11px;margin-top:3px;">已由新运行替代：${esc(job.superseded_by)}</p>` : ""}
        </div>
        <div style="width:260px;flex:0 0 260px;"><div class="progress"><div class="progress-bar" style="width:${Math.max(0, Math.min(100, percent))}%"></div></div><p class="meta" style="text-align:right;margin-top:3px;">${percent}% · ${esc(status)}</p><div style="text-align:right;margin-top:2px;">${actions}</div></div>
      </div>`;
    }).join("");
    box.querySelectorAll("[data-background-action]").forEach(button => {
      button.addEventListener("click", async () => {
        const id = button.getAttribute("data-background-id");
        const action = button.getAttribute("data-background-action");
        const job = (state.backgroundJobs || []).find(item => item.id === id);
        if (!job) return;
        try {
          if (action === "view") await viewBackgroundJob(job);
          else if (action === "retry") await retryBackgroundJob(job);
          else if (action === "dismiss") await dismissBackgroundJob(job);
          else {
            const suffix = action === "resume" ? "/resume" : "/cancel";
            const res = await api(backgroundJobEndpoint(job, suffix), { method: "POST" });
            if (!res || !res.ok) throw new Error((res && res.error) || "任务操作失败");
            await loadBackgroundJobs();
          }
        } catch (err) {
          toast(err && err.message ? err.message : String(err));
        }
      });
    });
  }

  async function loadBackgroundJobs() {
    const query = new URLSearchParams({ page: String(state.backgroundTerminalPage), page_size: String(state.backgroundTerminalPageSize) });
    const result = await api(`/api/background-jobs?${query.toString()}`);
    if (!result || !result.ok) {
      state.backgroundJobs = [];
      state.backgroundJobCounts = {};
      renderBackgroundJobs();
      return false;
    }
    const payload = result.data || {};
    state.backgroundJobs = Array.isArray(payload) ? payload : (payload.jobs || []);
    state.backgroundJobCounts = payload.counts || {};
    renderBackgroundJobs();
    for (const job of state.backgroundJobs) {
      if (BACKGROUND_TERMINAL.has(job.status) || state.backgroundJobPolls.has(job.id)) continue;
      const endpoint = backgroundJobEndpoint(job);
      const poll = async () => {
        const response = await api(endpoint);
        if (response && response.ok) {
          const index = state.backgroundJobs.findIndex(item => item.id === job.id);
          if (index >= 0) state.backgroundJobs[index] = { ...response.data, job_kind: backgroundJobKind(job) };
          renderBackgroundJobs();
          if (BACKGROUND_TERMINAL.has(response.data.status)) {
            state.backgroundJobPolls.delete(job.id);
            await loadProfiles();
            await loadBackgroundJobs();
            return;
          }
        }
        const timer = setTimeout(poll, response && response.ok ? 1000 : 4000);
        state.backgroundJobPolls.set(job.id, timer);
      };
      const timer = setTimeout(poll, 0);
      state.backgroundJobPolls.set(job.id, timer);
    }
    return true;
  }

  const PROFILE_LABELS = {
    persona: "Persona",
    organization: "Organization",
    institution: "Institution",
    collective: "Collective"
  };

  function profileTypeLabel(type) {
    return PROFILE_LABELS[type] || type || "Profile";
  }

  function profileActionLabel(profile) {
    return profile && profile.profile_type === "persona" ? "升级人格" : "升级档案";
  }

  function profileDateLabel(value) {
    if (!value) return "未记录";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleDateString(state.lang === "zh-CN" ? "zh-CN" : "en-US");
  }

  function profileTypeFields(profile) {
    const fieldMap = {
      persona: [
        ["identity_profile", "身份与时间线"],
        ["values_desires_contradictions", "价值观与矛盾"],
        ["expression_dna", "表达 DNA"],
        ["relationships", "重要关系"]
      ],
      organization: [
        ["mission", "使命"],
        ["culture", "组织文化"],
        ["strategy", "战略"],
        ["resources", "资源"],
        ["constraints", "核心约束"],
        ["decision_style", "决策风格"],
        ["competitive_relationships", "竞争关系"]
      ],
      institution: [
        ["institutional_goals", "制度目标"],
        ["policy_tools", "政策工具"],
        ["power_structure", "权力结构"],
        ["stakeholders", "利益相关方"],
        ["decision_patterns", "决策模式"],
        ["historical_behavior", "历史行为模式"]
      ],
      collective: [
        ["group_characteristics", "群体特征"],
        ["incentives", "激励"],
        ["adoption_tendency", "采用倾向"],
        ["common_positions", "常见立场"],
        ["internal_divisions", "内部分歧"]
      ]
    };
    const payload = profile && profile.payload && typeof profile.payload === "object" ? profile.payload : {};
    return (fieldMap[profile && profile.profile_type] || [])
      .map(([key, label]) => [label, payload[key]])
      .filter(([, value]) => value !== undefined && value !== null && value !== "");
  }

  function renderPersonas() {
    const grid = $("#personas-grid");
    if (!grid) return;
    const q = state.personaQ.trim().toLowerCase();
    // During the short initial load, retain the legacy Persona list rather
    // than showing a false empty state.  Once the Profile API responds, every
    // card is rendered from the unified library.
    let list = (state.profiles.length || state.profileType || state.profileStatus) ? state.profiles.slice() : state.personas.map(p => ({
      ...p,
      profile_type: "persona",
      slug: p.id,
      summary: p.summary || "",
      status: p.compile_state || "draft",
      coverage_state: p.compile_state === "compiled" ? "complete" : "partial",
      compile_state: p.compile_state || "draft",
      source_count: p.source_count || 0,
      evidence_count: p.evidence_count || 0,
      version: p.version || 1,
      updated_at: p.updated_at || ""
    }));
    if (!state.profiles.length && q) {
      list = list.filter(p => `${p.display_name || ""} ${p.id || ""}`.toLowerCase().includes(q));
    }

    if (!list.length) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><h3>${t("emptyPersona")}</h3></div>`;
      return;
    }

    const groups = {};
    list.forEach(profile => {
      const key = profile.profile_type || "persona";
      if (!groups[key]) groups[key] = [];
      groups[key].push(profile);
    });
    grid.innerHTML = Object.entries(groups).map(([type, profiles]) => `
      <div style="grid-column:1/-1;margin-top:10px;"><h3 style="margin:0 0 4px;">${esc(profileTypeLabel(type))}</h3><p class="meta">${profiles.length} 个档案</p></div>
      ${profiles.map(p => `
      <article class="card interactive persona-card profile-card" data-pid="${esc(p.id)}" data-profile-id="${esc(p.id)}" style="position:relative;cursor:pointer;">
        <div class="row-between" style="align-items:flex-start;margin-bottom:12px;">
          <div class="row" style="gap:12px;flex:1;min-width:0;">
            <div class="avatar">${(p.display_name || p.id).charAt(0).toUpperCase()}</div>
            <div style="min-width:0;">
              <h3 style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:4px;">${esc(p.display_name || p.id)}</h3>
              <p class="meta num" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(p.slug || p.id)}</p>
            </div>
          </div>
          <span class="tag">${esc(profileTypeLabel(p.profile_type))}</span>
        </div>
        <p class="kicker" style="min-height:42px;">${esc(p.summary || "档案简介将在证据与编译完成后生成。")}</p>
        <div class="row" style="margin-top:12px;gap:6px;flex-wrap:wrap;">
          <span class="tag">${esc(p.status || p.compile_state || "draft")}</span>
          <span class="tag">${Number(p.source_count || 0)} 来源</span>
          <span class="tag">${Number(p.evidence_count || 0)} 证据</span>
          <span class="tag">v${Number(p.version || 1)}</span>
        </div>
        <div class="row-between" style="margin-top:12px;align-items:center;">
          <span class="meta" style="font-size:11px;color:var(--muted);">覆盖：${esc(p.coverage_state || "unknown")} · 最近更新：${esc(profileDateLabel(p.updated_at))}</span>
          <div class="row" style="gap:5px;">
            <button class="btn btn-sm btn-secondary btn-enrich-profile" data-enrich-profile="${esc(p.id)}" type="button">${profileActionLabel(p)}</button>
            ${p.profile_type === "persona" ? `<button class="btn btn-sm btn-ghost btn-rename-persona" data-rename-persona="${esc(p.persona_id || p.id)}" data-rename-name="${esc(p.display_name || p.id)}" title="重命名" style="padding:4px 8px;font-size:12px;">重命名</button>` : ""}
            ${p.profile_type === "persona" ? `<button class="btn btn-sm btn-ghost btn-del-persona" data-del-persona="${esc(p.persona_id || p.id)}" data-del-profile="${esc(p.id)}" data-del-name="${esc(p.display_name || p.id)}" title="${t("delete")}" style="color:var(--danger);padding:4px 8px;font-size:12px;">${t("delete")}</button>` : `<button class="btn btn-sm btn-ghost btn-archive-profile" data-archive-profile="${esc(p.id)}" style="color:var(--danger);padding:4px 8px;font-size:12px;">归档</button>`}
          </div>
        </div>
      </article>`).join("")}`).join("");

    grid.querySelectorAll(".persona-card").forEach(el => {
      el.addEventListener("click", (e) => {
        if (e.target.closest("[data-del-persona], [data-enrich-profile], [data-archive-profile], [data-rename-persona]")) return;
        showProfileDetail(el.dataset.profileId || el.dataset.pid);
      });
    });

    grid.querySelectorAll("[data-rename-persona]").forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const pid = el.getAttribute("data-rename-persona");
        const currentName = el.getAttribute("data-rename-name") || pid;
        renamePersonaAction(pid, currentName);
      });
    });

    grid.querySelectorAll("[data-del-persona]").forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const pid = el.getAttribute("data-del-persona");
        const profileId = el.getAttribute("data-del-profile");
        const name = el.getAttribute("data-del-name") || pid;
        deletePersonaAction(pid, name, profileId);
      });
    });
    grid.querySelectorAll("[data-enrich-profile]").forEach(el => {
      el.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        try {
          await openProfileEnrichment(el.getAttribute("data-enrich-profile"));
        } catch (err) {
          console.error("Profile enrichment card action failed:", err);
          toast(`打开升级失败：${err && err.message ? err.message : err}`);
        }
      });
    });
    grid.querySelectorAll("[data-archive-profile]").forEach(el => {
      el.addEventListener("click", async (e) => {
        e.stopPropagation();
        const profileId = el.getAttribute("data-archive-profile");
        if (!profileId || !window.confirm("归档该档案？历史版本和证据仍会保留。")) return;
        const res = await api(`/api/profiles/${encodeURIComponent(profileId)}/archive`, { method: "POST" });
        if (res && res.ok) { toast("档案已归档"); await loadProfiles(); }
        else toast((res && res.error) || "归档失败");
      });
    });
  }

  async function showProfileDetail(pid) {
    const cached = state.profiles.find(x => x.id === pid) || state.personas.find(x => x.id === pid);
    if (!cached) return;
    const response = await api(`/api/profiles/${encodeURIComponent(pid)}`);
    const p = response && response.ok ? response.data : cached;
    const box = $("#persona-detail");
    if (!box) return;

    const typedFields = profileTypeFields(p);
    const typedFieldMarkup = typedFields.length
      ? `<div class="card" style="margin-top:12px;padding:12px;background:var(--surface);"><strong>类型专属字段</strong><div class="stack" style="gap:8px;margin-top:10px;">${typedFields.map(([label, value]) => `<div><span class="meta">${esc(label)}</span><div style="white-space:pre-wrap;margin-top:3px;">${esc(typeof value === "string" ? value : JSON.stringify(value, null, 2))}</div></div>`).join("")}</div></div>`
      : "";
    box.innerHTML = `
      <div class="row-between"><h3>${esc(p.display_name || p.id)}</h3><button class="btn btn-text" id="pd-close">${t("close")}</button></div>
      <p class="meta num" style="margin:8px 0 12px;">${esc(p.slug || p.id)} · ${esc(profileTypeLabel(p.profile_type || "persona"))}</p>
      <p>${esc(p.summary || "档案简介将在证据与编译完成后生成。")}</p>
      <div class="row" style="margin-top:16px;flex-wrap:wrap;gap:8px;">
        <span class="tag">${esc(p.status || p.compile_state || "draft")}</span>
        <span class="tag">Sources · ${esc(p.source_count || 0)}</span>
        <span class="tag">Evidence · ${esc(p.evidence_count || 0)}</span>
        <span class="tag">Version · ${esc(p.version || 1)}</span>
      </div>
      <div class="card" style="margin-top:16px;padding:12px;background:var(--surface);">
        <strong>覆盖与运行时</strong>
        <p class="meta" style="margin-top:6px;">${esc(JSON.stringify(p.coverage || {}, null, 2))}</p>
        <p class="meta">最近运行时：${esc(JSON.stringify(p.runtime_snapshot || {}, null, 2))}</p>
      </div>
      ${p.profile_type === "persona" && p.persona ? `<details style="margin-top:12px;"><summary>Persona 八维 / Manifest</summary><pre class="meta" style="white-space:pre-wrap;margin-top:8px;">${esc(JSON.stringify(p.persona, null, 2))}</pre></details>` : ""}
      ${typedFieldMarkup}
      ${p.payload && Object.keys(p.payload).length && !typedFields.length ? `<details style="margin-top:12px;"><summary>类型专属字段</summary><pre class="meta" style="white-space:pre-wrap;margin-top:8px;">${esc(JSON.stringify(p.payload, null, 2))}</pre></details>` : ""}
      <div class="row-between" style="margin-top:24px;padding-top:16px;border-top:1px solid var(--border);">
        ${p.profile_type === "persona" ? `<button class="btn btn-danger" id="pd-delete" style="font-size:13px;">${t("deletePersona") || "删除该人物"}</button>` : `<span></span>`}
        <div class="row" style="gap:8px;">
          ${p.profile_type === "persona" ? `<button class="btn btn-ghost" id="pd-rename" style="font-size:13px;">重命名</button>` : ""}
          <button class="btn btn-secondary" id="pd-enrich">${profileActionLabel(p)}</button>
          <button class="btn btn-secondary" id="pd-close-bottom">${t("close")}</button>
        </div>
      </div>`;

    const closeBtn = $("#pd-close");
    if (closeBtn) closeBtn.onclick = () => $("#dlg-persona").close();
    const closeBottomBtn = $("#pd-close-bottom");
    if (closeBottomBtn) closeBottomBtn.onclick = () => $("#dlg-persona").close();
    const renameBtn = $("#pd-rename");
    if (renameBtn) {
      renameBtn.onclick = () => {
        $("#dlg-persona").close();
        renamePersonaAction(p.persona_id || p.id, p.display_name || p.id);
      };
    }
    const delBtn = $("#pd-delete");
    if (delBtn) {
      delBtn.onclick = () => {
        $("#dlg-persona").close();
        deletePersonaAction(p.persona_id || p.id, p.display_name || p.id, p.id);
      };
    }
    const enrichBtn = $("#pd-enrich");
    if (enrichBtn) {
      enrichBtn.onclick = async (event) => {
        event.preventDefault();
        event.stopPropagation();
        $("#dlg-persona").close();
        try {
          await openProfileEnrichment(p.id);
        } catch (err) {
          console.error("Profile enrichment detail action failed:", err);
          toast(`打开升级失败：${err && err.message ? err.message : err}`);
        }
      };
    }
    $("#dlg-persona").showModal();
  }

  // Keep the old name as a compatibility shim for room-side callers.
  function showPersonaDetail(pid) { return showProfileDetail(pid); }

  function setupProfileRuntimeSelectors(prefix) {
    const sourceSel = $(`#${prefix}-source`);
    const agentSel = $(`#${prefix}-agent`);
    const modelSel = $(`#${prefix}-model`);
    const reasoningSel = $(`#${prefix}-reasoning`);
    if (!sourceSel || !agentSel || !modelSel || !reasoningSel) return false;
    const capabilityEl = $(`#${prefix}-capability`);
    const reasoningCapabilityEl = $(`#${prefix}-reasoning-capability`);
    const submitEl = $(`#${prefix}-submit`);
    const setSubmitAvailability = (enabled, reason = "") => {
      if (!submitEl) return;
      submitEl.disabled = !enabled;
      submitEl.title = enabled ? "" : reason;
      submitEl.setAttribute("aria-disabled", String(!enabled));
    };
    const populate = () => {
      const source = sourceSel.value || "local_cli";
      const agents = getSelectableAgentsForSource(source);
      if (!agents.length) {
        const scanning = state.agentsLoading;
        agentSel.innerHTML = `<option value="" selected>${scanning ? "正在扫描 Runtime…" : "没有 READY / Connected Runtime"}</option>`;
        modelSel.innerHTML = `<option value="" selected>${scanning ? "扫描结束后可选模型" : "没有模型能力报告"}</option>`;
        reasoningSel.innerHTML = `<option value="" selected>${scanning ? "扫描结束后可选 Reasoning" : "没有 Reasoning 能力报告"}</option>`;
        setReasoningCapabilityNotice(reasoningCapabilityEl, null, null, source);
        const message = scanning
          ? "正在扫描可用 Runtime…"
          : "当前来源没有可用 Runtime；请先完成连接后再升级。";
        if (capabilityEl) capabilityEl.textContent = message;
        setSubmitAvailability(false, message);
        return;
      }
      setSubmitAvailability(true);
      agentSel.innerHTML = agents.map(agent => `<option value="${esc(agent.id)}">${esc(agent.name || agent.id)} [READY]</option>`).join("");
      const updateModels = () => {
        const agent = agents.find(item => item.id === agentSel.value) || agents[0];
        const models = getRuntimeModels(agent);
        modelSel.innerHTML = models.length
          ? models.map(model => `<option value="${esc(model.id)}">${esc(runtimeModelOptionLabel(agent, model))}</option>`).join("")
          : `<option value="" selected>该 Runtime 未报告模型</option>`;
        const updateReasoning = () => {
          const model = models.find(item => item.id === modelSel.value) || models[0];
          const efforts = getRuntimeReasoningOptions(agent, model);
          const capability = getReasoningCapability(model);
          reasoningSel.innerHTML = efforts.length
            ? efforts.map(effort => {
              const selected = effort === (model && model.default_reasoning_effort)
                || effort === capability.default_effort;
              return `<option value="${esc(effort)}" ${selected ? "selected" : ""}>${esc(reasoningOptionLabel(effort))}</option>`;
            }).join("")
            : `<option value="" selected>Agent 未报告 Reasoning</option>`;
          reasoningSel.disabled = !efforts.length;
          setReasoningCapabilityNotice(reasoningCapabilityEl, agent, model, source);
        };
        modelSel.onchange = updateReasoning;
        updateReasoning();
        const research = (agent && agent.research) || {};
        if (capabilityEl) {
          const status = research.verification_status || "unknown";
          if (status === "verified") {
            capabilityEl.textContent = `联网研究能力：已验证 ✓ · ${research.verification_method || "behavioral_probe"}`;
          } else if (status === "declared") {
            capabilityEl.textContent = "联网研究能力：运行时声明支持，首次运行将验证。";
          } else if (status === "blocked") {
            capabilityEl.textContent = `联网研究能力：被当前 Headless Tool Policy 阻止。${research.verification_error || "请调整权限后重新验证。"}`;
          } else if (status === "unavailable") {
            capabilityEl.textContent = `联网研究能力：验证不可用。${research.verification_error || "请更换 Runtime 或配置 Research Broker。"}`;
          } else if ((source === "local_cli" || agent.runtime_source === "local_cli") && agent.status === "ready") {
            capabilityEl.textContent = "联网研究能力：尚未验证。首次使用时将自动测试该 Agent 的原生 Web Research 能力。";
          } else if (research.mode === "broker" || research.mode === "mcp") {
            capabilityEl.textContent = "联网研究能力：经配置的 Research Broker / MCP；仍会经过来源、证据与覆盖门禁。";
          } else {
            capabilityEl.textContent = "联网研究能力：尚未验证。当前 API Runtime 不会自动假定具备联网能力。";
          }
        }
      };
      agentSel.onchange = updateModels;
      updateModels();
    };
    sourceSel.onchange = populate;
    populate();
    return true;
  }

  function profileRuntimePayload(prefix) {
    return {
      runtime_source: $(`#${prefix}-source`).value || "local_cli",
      agent_id: $(`#${prefix}-agent`).value || "",
      model_id: $(`#${prefix}-model`).value || null,
      reasoning_effort: $(`#${prefix}-reasoning`).value || null
    };
  }

  function openProfileCreate() {
    const form = $("#form-profile-create");
    if (form) form.reset();
    const error = $("#profile-create-error");
    if (error) error.hidden = true;
    setupProfileRuntimeSelectors("profile-create");
    $("#dlg-profile-create").showModal();
    if (!state.agents.length) {
      hydrateRuntimeSelectors(() => setupProfileRuntimeSelectors("profile-create"));
    }
  }

  async function submitProfileCreate(event) {
    event.preventDefault();
    const error = $("#profile-create-error");
    if (error) error.hidden = true;
    const name = $("#profile-create-name").value.trim();
    if (!name) return;
    const runtime = profileRuntimePayload("profile-create");
    const payload = {
      profile_type: $("#profile-create-type").value,
      display_name: name,
      summary: $("#profile-create-summary").value.trim(),
      runtime,
      auto_enrich: Boolean(runtime.agent_id),
      requested_scope: "full_refresh",
      research_policy: { profile: "deep" }
    };
    const submit = $("#profile-create-submit");
    if (submit) { submit.disabled = true; submit.textContent = "正在创建…"; }
    try {
      const res = await api("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (res && res.ok) {
        $("#dlg-profile-create").close();
        toast(res.data && res.data.job ? "档案已创建，正在研究并编译" : "档案草稿已创建");
        await loadProfiles();
      } else if (error) {
        error.hidden = false;
        error.textContent = (res && res.error) || "档案创建失败";
      }
    } finally {
      if (submit) { submit.disabled = false; submit.textContent = "创建并研究"; }
    }
  }

  async function openProfileEnrichment(profileId) {
    const normalizedProfileId = typeof profileId === "string" ? profileId.trim() : "";
    if (!normalizedProfileId) throw new Error("缺少 Profile ID。");
    const dialog = $("#dlg-profile-enrich");
    if (!dialog) throw new Error("找不到升级档案对话框。");
    const error = $("#profile-enrich-error");
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
    const submitStatus = $("#profile-enrich-submit-status");
    if (submitStatus) {
      submitStatus.hidden = true;
      submitStatus.textContent = "";
    }
    const profile = state.profiles.find(item => item.id === normalizedProfileId) || {
      id: normalizedProfileId,
      display_name: normalizedProfileId,
      profile_type: "persona"
    };
    $("#profile-enrich-id").value = normalizedProfileId;
    $("#profile-enrich-title").textContent = profileActionLabel(profile);
    $("#profile-enrich-target").textContent = `${profile.display_name || normalizedProfileId} · ${profileTypeLabel(profile.profile_type)}`;
    $("#profile-enrich-materials").value = "";
    $("#profile-enrich-files").value = "";
    const inputMode = $("#profile-enrich-input-mode");
    if (inputMode) inputMode.value = "local_materials";
    $("#profile-enrich-remote-consent").checked = false;
    const resetupEnrichSelectors = () => {
      let loadError = "";
      try {
        const selectorReady = setupProfileRuntimeSelectors("profile-enrich");
        if (selectorReady === false) {
          throw new Error("Profile enrichment runtime selector elements are missing.");
        }
      } catch (err) {
        console.error("Profile enrichment runtime selector failed:", err);
        const submit = $("#profile-enrich-submit");
        if (submit) {
          submit.disabled = true;
          submit.title = "Runtime selector 初始化失败，请重试。";
          submit.setAttribute("aria-disabled", "true");
        }
        if (error) {
          error.hidden = false;
          error.textContent = "Runtime selector 初始化失败，请重试。";
        }
        return;
      }
      if (!state.agents.length) {
        loadError = "无法刷新 Runtime 列表，请前往智能体页面重新扫描。";
      }
      if (loadError && error && error.hidden) {
        error.hidden = false;
        error.textContent = loadError;
      }
    };
    resetupEnrichSelectors();
    dialog.showModal();
    if (!state.agents.length) {
      hydrateRuntimeSelectors(resetupEnrichSelectors);
    }
  }

  async function submitProfileEnrichment(event) {
    event.preventDefault();
    const profileId = $("#profile-enrich-id").value;
    if (!profileId) return;
    const error = $("#profile-enrich-error");
    const submit = $("#profile-enrich-submit");
    const submitStatus = $("#profile-enrich-submit-status");
    if (submit && submit.disabled) return;
    if (error) {
      error.hidden = true;
      error.textContent = "";
    }
    if (submit) {
      submit.disabled = true;
      submit.textContent = "正在创建升级任务…";
      submit.setAttribute("aria-busy", "true");
    }
    if (submitStatus) {
      submitStatus.hidden = false;
      submitStatus.textContent = "正在读取材料并创建升级任务，请稍候。";
    }
    try {
      const runtime = profileRuntimePayload("profile-enrich");
      if (!runtime.agent_id) {
        throw new Error("请选择 READY / Connected Agent 后再升级。");
      }
      const lines = $("#profile-enrich-materials").value.split(/\n+/).map(item => item.trim()).filter(Boolean);
      const files = Array.from($("#profile-enrich-files").files || []);
      const fileMaterials = [];
      for (const file of files) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = "";
        for (let index = 0; index < bytes.length; index += 0x8000) {
          binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
        }
        fileMaterials.push({
          id: `ui_file_${file.name}`,
          title: file.name,
          filename: file.name,
          content_base64: btoa(binary),
          source_type: "user_file"
        });
      }
      const res = await api(`/api/profiles/${encodeURIComponent(profileId)}/enrich`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          requested_scope: $("#profile-enrich-scope").value,
          runtime,
          materials: [
            ...lines.map((content, index) => ({ id: `ui_material_${index}`, content, source_type: "user_provided" })),
            ...fileMaterials
          ],
          remote_material_consent: $("#profile-enrich-remote-consent").checked,
          research_policy: { profile: "deep" },
          enrichment_input_mode: $("#profile-enrich-input-mode")?.value || "local_materials"
        })
      });
      if (!res || !res.ok) {
        throw new Error((res && res.error) || "升级任务创建失败");
      }
      $("#dlg-profile-enrich").close();
      toast("升级任务已创建，正在分析材料并重新编译；旧版本仍保留可追溯");
      openProfileEnrichmentProgress(res.data);
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      if (error) {
        error.hidden = false;
        error.textContent = `升级失败：${message}`;
      }
      if (submitStatus) {
        submitStatus.hidden = true;
        submitStatus.textContent = "";
      }
      console.error("Profile enrichment submit failed:", err);
    } finally {
      if (submit) {
        submit.disabled = false;
        submit.textContent = "开始升级";
        submit.removeAttribute("aria-busy");
      }
    }
  }

  function renderProfileEnrichmentProgress(job) {
    if (!job) return;
    state.profileEnrichmentJob = job;
    const progress = job.progress || {};
    const current = progress.persona_status || job.status || "created";
    const stage = progress.stage || current;
    const materialProgress = progress.material_progress || {};
    const calls = progress.agent_calls || {};
    const dimensions = progress.coverage && progress.coverage.dimension_coverage
      ? Object.keys(progress.coverage.dimension_coverage).filter(key => Number(progress.coverage.dimension_coverage[key] || 0) > 0).length
      : Object.keys(progress.dimension_progress || {}).length;
    const title = progress.display_name || job.target_profile_id || "Profile";
    const runtime = job.selected_runtime || {};
    const runtimeDiagnostics = backgroundAgentDiagnosticsText(job);
    const profileAgentCallCount = Math.max(
      Number(job.agent_call_count || 0),
      Number(progress.agent_call_count || 0),
      Number(progress.child_agent_call_count || 0),
    );
    $("#profile-enrich-progress-title").textContent = `${title} · 升级进度`;
    const childJobId = progress.child_job_id || progress.persona_job_id || job.persona_creation_job_id;
    const childJobText = childJobId ? ` · 内部执行 Job: ${childJobId}` : "";
    const workerText = [
      job.worker_state || progress.worker_state ? `Worker: ${job.worker_state || progress.worker_state}` : "",
      job.worker_heartbeat_at || progress.worker_heartbeat_at ? `Heartbeat: ${job.worker_heartbeat_at || progress.worker_heartbeat_at}` : "",
      `Agent calls: ${profileAgentCallCount}`,
    ].filter(Boolean).join(" · ");
    $("#profile-enrich-progress-runtime").textContent = `Runtime: ${runtime.agent_id || "未选择"} · ${runtime.model_id || "未报告模型"} · ${runtime.reasoning_effort || "Agent 默认 Reasoning"}${childJobText}${workerText ? ` · ${workerText}` : ""}${runtimeDiagnostics ? ` · ${runtimeDiagnostics}` : ""}`;
    $("#profile-enrich-progress-stage").textContent = `Stage: ${stage}`;
    $("#profile-enrich-progress-materials").textContent = `材料：${job.input_material_count || progress.material_count || 0} · Evidence Units：${materialProgress.evidence_unit_count || 0}`;
    $("#profile-enrich-progress-agents").textContent = `Agent 调用：材料 ${calls.material || 0} · 八维 ${calls.dimension || 0}`;
    $("#profile-enrich-progress-dimensions").textContent = `八维：${dimensions} / 8`;
    const percent = Number(progress.percent ?? (job.status === "completed" || job.status === "completed_with_gaps" ? 100 : 0));
    $("#profile-enrich-progress-bar").style.width = `${Math.min(100, Math.max(0, percent))}%`;
    $("#profile-enrich-progress-result").textContent = progress.result === "NO_NEW_INFORMATION"
      ? progress.message || "新材料与现有证据重复，本次未产生有效升级。"
      : ["completed", "completed_with_gaps"].includes(job.status)
        ? `升级完成${job.new_version ? ` · v${job.base_persona_version || 1} → v${job.new_version}` : ""}`
        : (job.failure_json ? `${job.failure_json.code || "任务失败"}：${job.failure_json.message || job.error || ""}` : job.error || "任务仍在运行，完成后才会生成新版本。");
    const retry = $("#profile-enrich-progress-retry");
    if (retry) {
      retry.hidden = !BACKGROUND_FAILED.has(job.status) || !(
        job.retry_available === true || (job.failure_json && job.failure_json.retriable === true)
      );
      retry.disabled = false;
    }
    const active = ["created", "planning", "researching", "ingesting", "ingesting_sources", "compiling", "extracting"].includes(job.status);
    const pause = $("#profile-enrich-progress-pause");
    const resume = $("#profile-enrich-progress-resume");
    const cancel = $("#profile-enrich-progress-cancel");
    if (pause) pause.hidden = !active;
    if (resume) resume.hidden = !["paused", "paused_runtime_unavailable"].includes(job.status);
    if (cancel) cancel.hidden = ["completed", "completed_with_gaps", "failed", "cancelled"].includes(job.status);
  }

  async function openProfileEnrichmentProgress(initialJob) {
    const dialog = $("#dlg-profile-enrich-progress");
    if (!dialog || !initialJob || !initialJob.id) return;
    renderProfileEnrichmentProgress(initialJob);
    if (!dialog.open) dialog.showModal();
    await pollProfileEnrichmentJob(initialJob.id, initialJob);
    await loadProfiles();
    await loadBackgroundJobs();
  }

  async function pollProfileEnrichmentJob(jobId, initialJob = null) {
    if (!jobId) return null;
    let snapshot = initialJob;
    const terminal = new Set(["completed", "completed_with_gaps", "failed", "cancelled"]);
    if (snapshot) renderProfileEnrichmentProgress(snapshot);
    let delay = 1000;
    while (true) {
      const response = await api(`/api/profile-enrichment/jobs/${encodeURIComponent(jobId)}`);
      if (response && response.ok) {
        snapshot = response.data;
        renderProfileEnrichmentProgress(snapshot);
        if (terminal.has(snapshot.status)) break;
        delay = snapshot.status === "paused" || snapshot.status === "paused_runtime_unavailable" ? 5000 : 1000;
      }
      await new Promise(resolve => setTimeout(resolve, delay));
    }
    return snapshot;
  }

  // ─── Persona Rename (display name only; persona_id stays stable) ────────
  async function renamePersonaAction(pid, currentName) {
    const input = window.prompt("新的展示名称（不影响 Persona ID、绑定与历史记录）：", currentName || "");
    if (input === null) return;
    const name = String(input).trim();
    if (!name) { toast("名称不能为空"); return; }
    if (name === currentName) return;
    try {
      const res = await api(`/api/personas/${encodeURIComponent(pid)}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: name })
      });
      if (res && res.ok) {
        toast(`已重命名为【${name}】`);
        await Promise.all([loadPersonas(), loadProfiles()]);
        renderAll();
      } else {
        toast(`重命名失败: ${(res && res.error) || "未知错误"}`);
      }
    } catch (err) {
      toast(`重命名出错: ${err.message || err}`);
    }
  }

  async function deletePersonaAction(pid, displayName, profileId = null) {
    if (!pid) return;
    const name = displayName || pid;
    const confirmMsg = state.lang === "zh-CN"
      ? `确定要删除数字人物【${name}】及其所有关联记忆与配置吗？此操作无法恢复。`
      : `Are you sure you want to delete persona [${name}] and its memories? This cannot be undone.`;

    confirmDlg(
      confirmMsg,
      t("delete") || "删除",
      async () => {
        try {
          const res = await api(`/api/personas/${encodeURIComponent(pid)}`, { method: "DELETE" });
          if (res && res.ok) {
            state.personas = state.personas.filter(p => p.id !== pid);
            state.profiles = state.profiles.filter(
              p => p.id !== (profileId || pid) && p.persona_id !== pid
            );
            renderPersonas();
            toast(state.lang === "zh-CN" ? `已成功删除人物【${name}】` : `Persona [${name}] deleted successfully`);
            await Promise.all([loadPersonas(), loadProfiles()]);
            renderAll();
          } else {
            const err = (res && res.error) || "删除失败";
            toast(`删除失败: ${err}`);
          }
        } catch (exc) {
          toast(`删除出错: ${exc.message}`);
        }
      },
      true
    );
  }

  // ─── Persona Creation Runtime ───────────────────────────────────────────
  function personaCreationTypeChanged() {
    const type = $("#pc-persona-type").value;
    const mode = $("#pc-creation-mode");
    const publicType = type.startsWith("public_");
    const privateType = type.startsWith("private_");
    const fictional = type === "fictional_or_synthetic_person";
    if (publicType) mode.value = "public_research";
    else if (fictional) mode.value = "fictional";
    else if (privateType && !["private_materials", "guided_interview"].includes(mode.value)) mode.value = "private_materials";
    mode.querySelectorAll("option").forEach(option => {
      option.hidden = (publicType && option.value !== "public_research")
        || (fictional && option.value !== "fictional")
        || (privateType && !["private_materials", "guided_interview"].includes(option.value));
    });
    $("#pc-policy-wrap").hidden = !publicType;
    // Private and fictional personas both use local Evidence materials;
    // only public research hides the materials uploader.
    $("#pc-materials-wrap").hidden = publicType;
  }

  function personaCreationPolicyChanged() {
    const policy = $("#pc-policy");
    const custom = $("#pc-custom-policy");
    if (custom) custom.hidden = !policy || policy.value !== "custom";
  }

  function setupPersonaCreationRuntimeSelectors() {
    const sourceSel = $("#pc-runtime-source");
    const agentSel = $("#pc-agent");
    const modelSel = $("#pc-model");
    const reasoningSel = $("#pc-reasoning");
    if (!sourceSel || !agentSel || !modelSel || !reasoningSel) return;
    const reasoningCapabilityEl = $("#pc-reasoning-capability");

    const populate = () => {
      const source = sourceSel.value || "local_cli";
      const agents = getSelectableAgentsForSource(source);
      if (!agents.length) {
        const scanning = state.agentsLoading;
        agentSel.innerHTML = `<option value="" disabled selected>${scanning ? "正在扫描 Runtime…" : "没有 READY / Connected Runtime"}</option>`;
        modelSel.innerHTML = `<option value="" disabled selected>${scanning ? "扫描结束后可选模型" : "没有模型能力报告"}</option>`;
        reasoningSel.innerHTML = `<option value="" disabled selected>${scanning ? "扫描结束后可选 Reasoning" : "没有 Reasoning 能力报告"}</option>`;
        reasoningSel.disabled = true;
        setReasoningCapabilityNotice(reasoningCapabilityEl, null, null, source);
        $("#pc-research-capability").textContent = scanning
          ? "正在扫描可用 Runtime…"
          : "当前来源没有可用 Runtime。请先在 Agent / API 页面完成连接。";
        return;
      }
      agentSel.innerHTML = agents.map(agent => `<option value="${esc(agent.id)}">${esc(agent.name)} [READY]</option>`).join("");
      const updateModels = () => {
        const agent = agents.find(item => item.id === agentSel.value) || agents[0];
        const models = getRuntimeModels(agent);
        modelSel.innerHTML = models.length
          ? models.map(model => `<option value="${esc(model.id)}">${esc(runtimeModelOptionLabel(agent, model))}</option>`).join("")
          : `<option value="" disabled selected>该 Runtime 未报告可选模型</option>`;
        const updateReasoning = () => {
          const model = models.find(item => item.id === modelSel.value) || models[0];
          const efforts = getRuntimeReasoningOptions(agent, model);
          const capability = getReasoningCapability(model);
          reasoningSel.innerHTML = efforts.length
            ? efforts.map(effort => {
              const selected = effort === (model && model.default_reasoning_effort)
                || effort === capability.default_effort;
              return `<option value="${esc(effort)}" ${selected ? "selected" : ""}>${esc(reasoningOptionLabel(effort))}</option>`;
            }).join("")
            : `<option value="" disabled selected>Agent 未报告可选 Reasoning</option>`;
          reasoningSel.disabled = !efforts.length;
          setReasoningCapabilityNotice(reasoningCapabilityEl, agent, model, source);
        };
        modelSel.onchange = updateReasoning;
        updateReasoning();
        const research = (agent && agent.research) || {};
        const capabilityEl = $("#pc-research-capability");
        if (capabilityEl) {
          const status = research.verification_status || "unknown";
          if (status === "verified") {
            capabilityEl.textContent = "联网研究：已验证 ✓；仍需通过来源与证据覆盖门禁。";
          } else if (status === "declared") {
            capabilityEl.textContent = "联网研究：运行时声明支持，开始创建时将先进行真实验证。";
          } else if (status === "blocked") {
            capabilityEl.textContent = `联网研究：被当前 Headless Tool Policy 阻止。${research.verification_error || "请调整权限后重新验证。"}`;
          } else if (status === "unavailable") {
            capabilityEl.textContent = `联网研究：验证不可用。${research.verification_error || "请更换 Runtime。"}`;
          } else if (source === "local_cli" && agent && agent.status === "ready") {
            capabilityEl.textContent = "联网研究：尚未验证；开始创建时将自动测试该 Agent 的原生 Web Research 能力。";
          } else {
            capabilityEl.textContent = "联网研究：尚未验证；API Runtime 不会自动假定具备联网能力。";
          }
        }
      };
      agentSel.onchange = updateModels;
      updateModels();
    };
    sourceSel.onchange = populate;
    populate();
  }

  function openPersonaCreation(existing = null) {
    const form = $("#form-persona-create");
    if (form) form.reset();
    const progressCard = $("#pc-progress");
    if (progressCard && progressCard.parentElement !== form) form.appendChild(progressCard);
    const progressDialog = $("#dlg-persona-creation-progress");
    if (progressDialog && progressDialog.open) progressDialog.close();
    state.personaCreationExistingId = existing && existing.id ? existing.id : null;
    state.personaCreationJob = null;
    if (state.personaCreationPoll) clearInterval(state.personaCreationPoll);
    state.personaCreationPoll = null;
    $("#pc-progress").hidden = true;
    $("#pc-error-form").hidden = true;
    $("#pc-start").disabled = false;
    $("#pc-start").hidden = false;
    personaCreationTypeChanged();
    personaCreationPolicyChanged();
    setupPersonaCreationRuntimeSelectors();
    if (existing) {
      $("#pc-display-name").value = existing.display_name || existing.id || "";
      $("#pc-aliases").value = (existing.aliases || []).join(", ");
      if (existing.persona_type) $("#pc-persona-type").value = existing.persona_type;
      personaCreationTypeChanged();
    }
    const dlg = $("#dlg-persona-create");
    if (dlg) dlg.showModal();
  }

  function openPersonaCreationProgressDialog(job) {
    const card = $("#pc-progress");
    const host = $("#pc-progress-host");
    const dialog = $("#dlg-persona-creation-progress");
    if (card && host && card.parentElement !== host) host.appendChild(card);
    renderPersonaCreationJob(job);
    if (dialog && !dialog.open) dialog.showModal();
  }

  function renderPersonaCreationJob(job) {
    if (job && ["completed", "completed_with_gaps"].includes(job.status)) {
      maybeReturnToNarrativeCast(job);
    }
    if (!job) return;
    const progress = $("#pc-progress");
    if (progress) progress.hidden = false;
    const jobProgress = job.progress || {};
    $("#pc-stage").textContent = job.status === "failed_quality_gate"
      ? "质量门禁未通过"
      : (jobProgress.label || job.current_stage || job.status || "—");
    $("#pc-job-status").textContent = job.status || "—";
    const jobPercent = Number(jobProgress.percent ?? (job.status === "completed" || job.status === "completed_with_gaps" ? 100 : 0));
    const creationBar = $("#pc-progress-bar");
    if (creationBar) creationBar.style.width = `${Math.max(0, Math.min(100, jobPercent))}%`;
    const policy = job.research_policy || {};
    const coverage = job.coverage || {};
    const rawSources = coverage.raw_source_count ?? job.source_count ?? 0;
    const independentSources = coverage.independent_sources ?? coverage.unique_sources ?? 0;
    const target = policy.preferred_source_target || policy.min_unique_sources || "—";
    $("#pc-sources").textContent = `Sources: ${rawSources}`;
    $("#pc-independent-sources").textContent = `Independent: ${independentSources} / ${policy.min_unique_sources || "—"}`;
    $("#pc-research-budget").textContent = `Budget: ${target}+ / ${policy.soft_max_sources || "—"} soft`;
    const dimensions = job.dimension_progress || {};
    const complete = Object.values(dimensions).filter(value => Number(value) >= Number(policy.min_sources_per_dimension || 1)).length;
    $("#pc-dimensions").textContent = `Dimensions: ${complete} / 8`;
    const artifacts = job.compilation_task_id && job.coverage && job.coverage.dimension_coverage
      ? Object.keys(job.coverage.dimension_coverage).filter(key => Number((job.coverage.dimension_coverage[key] || {}).claim_count || 0) > 0).length
      : Object.keys(dimensions).length;
    $("#pc-artifacts").textContent = `Artifacts: ${artifacts} / 8`;
    const lifeStages = coverage.life_stages || {};
    const lifeComplete = Object.values(lifeStages).filter(item => item && item.complete).length;
    $("#pc-life-stages").textContent = `Life stages: ${lifeComplete} / ${Object.keys(lifeStages).length || "—"}`;
    $("#pc-primary-secondary").textContent = `Primary / secondary: ${coverage.primary_source_count ?? "—"} / ${coverage.secondary_source_count ?? "—"}`;
    const gains = job.information_gain || [];
    const latestGain = gains.length ? Number((gains[gains.length - 1] || {}).marginal_gain_score || 0).toFixed(2) : "—";
    $("#pc-information-gain").textContent = `Recent gain: ${latestGain}`;
    const material = (job.coverage && job.coverage.material_layer)
      || (job.job_config && job.job_config.material_analysis && job.job_config.material_analysis.coverage)
      || {};
    $("#pc-evidence-units").textContent = `Evidence units: ${material.evidence_unit_count ?? 0}`;
    $("#pc-fused-evidence").textContent = `Fused: ${material.fused_evidence_count ?? 0}`;
    $("#pc-material-contradictions").textContent = `Contradictions: ${material.contradiction_count ?? 0}`;
    const materialProgress = (job.job_config && job.job_config.material_progress) || {};
    const materialStats = $("#pc-material-stats");
    if (materialStats) {
      materialStats.textContent = [
        `stage: ${materialProgress.status || "—"}`,
        `sources: ${material.source_count ?? 0}`,
        `units: ${material.evidence_unit_count ?? 0}`,
        `episodes: ${material.episode_count ?? 0}`,
        `fused evidence: ${material.fused_evidence_count ?? 0}`,
        `contradictions: ${material.contradiction_count ?? 0}`,
        `gaps: ${(material.high_priority_gaps || []).join(", ") || "—"}`,
      ].join("\n");
    }
    $("#pc-stop-reason").textContent = job.research_stop_reason
      ? `Stop reason: ${job.research_stop_reason}`
      : (coverage.passed ? t("coverageGatePassed") : "继续研究：正在补齐高优先级缺口")
    const gaps = job.research_gaps || [];
    $("#pc-gaps").textContent = gaps.length
      ? gaps.map(gap => `${gap.priority >= 80 ? "HIGH" : ""} ${gap.type || "gap"}: ${gap.target || "—"} (${gap.evidence_count || 0})`).join("\n")
      : "当前没有已记录的研究缺口。";
    // ── Execution target + extraction detail + checkpoints + history ──
    const jobConfig = job.job_config || {};
    const binding = jobConfig.runtime_binding_snapshot || {};
    const capabilities = jobConfig.effective_model_capabilities || {};
    const setTag = (id, value) => { const el = $(id); if (el) el.textContent = value; };
    setTag("#pc-runtime-agent", `Agent: ${job.agent_id || binding.agent_id || "—"}`);
    setTag("#pc-runtime-model", `Model: ${job.model_id || binding.model_id || "—"}`);
    setTag("#pc-runtime-reasoning", `Reasoning: ${job.reasoning_effort || binding.reasoning_effort || "default"}`);
    setTag("#pc-runtime-effective", `Effective: ${binding.effective_model || capabilities.effective_model || job.model_id || "—"}`);
    // Unknown context is never presented as a measured capability: a planning
    // fallback must not masquerade as "32K".
    setTag("#pc-runtime-context", formatContextTag(capabilities));
    const pcContextDetail = $("#pc-runtime-context-detail");
    if (pcContextDetail) pcContextDetail.textContent = formatContextDetail(capabilities);
    const pcContextTag = $("#pc-runtime-context");
    if (pcContextTag) {
      pcContextTag.title = formatContextDetail(capabilities);
      pcContextTag.classList.toggle("pc-context-unknown", capabilities.effective_context_window == null);
    }
    const dimensionProgress = job.dimension_progress || {};
    const doneDims = Object.keys(dimensionProgress).filter(key => Number(dimensionProgress[key]) > 0);
    const extractionEvents = (job.events || []).filter(e => e.event === "persona_extraction_progress");
    const latestExtraction = extractionEvents[extractionEvents.length - 1] || {};
    const batchEvents = (job.events || []).filter(e => e.event === "persona_dimension_batch_completed");
    const latestBatch = batchEvents[batchEvents.length - 1] || {};
    const checkpointSummary = jobConfig.dimension_batch_checkpoint_summary || {};
    const checkpointHits = Number(jobConfig.dimension_checkpoint_hit_count || 0);
    const batchParts = [];
    if (latestExtraction.current_dimension) batchParts.push(`当前: ${latestExtraction.current_dimension}`);
    if (latestBatch.event) batchParts.push(`Batch ${latestBatch.batch_index || "?"}${latestBatch.reused ? "（复用）" : ""}`);
    const dimensionDetail = $("#pc-dimension-detail");
    if (dimensionDetail) {
      const pausedFlag = jobConfig.pause_requested
        ? " · 暂停请求已提交：将在当前步骤完成后安全暂停"
        : "";
      dimensionDetail.textContent = `八维: ${latestExtraction.completed_dimensions ?? doneDims.length} / 8 完成`
        + (doneDims.length ? `（已完成: ${doneDims.join(", ")}）` : "")
        + (batchParts.length ? ` · ${batchParts.join(" · ")}` : "")
        + (checkpointHits ? ` · 批次复用 ${checkpointHits} 次` : "")
        + pausedFlag;
    }
    const checkpoint = $("#pc-checkpoint");
    if (checkpoint) {
      const lines = [];
      const lastCheckpoint = jobConfig.last_checkpoint;
      if (lastCheckpoint && lastCheckpoint.stage) {
        lines.push(`最后成功检查点: ${lastCheckpoint.stage} @ ${lastCheckpoint.timestamp || "—"}`);
      }
      const summaryEntries = Object.entries(checkpointSummary);
      if (summaryEntries.length) {
        lines.push(`批次检查点: ${summaryEntries.map(([dim, count]) => `${dim}×${count}`).join(", ")}`);
      }
      checkpoint.textContent = lines.join("\n") || "尚无检查点。";
    }
    const history = $("#pc-execution-history");
    if (history) {
      const entries = jobConfig.execution_history || [];
      history.textContent = entries.length
        ? entries.map(entry => `${entry.timestamp || ""} · ${entry.action || ""} · ${entry.agent_id || "—"} / ${entry.effective_model || entry.model_id || "—"} / ${entry.reasoning || "default"} · ${entry.status || ""}`).join("\n")
        : "尚无执行目标变更记录。";
    }
    const runtimeDiagnostics = backgroundAgentDiagnosticsText(job);
    $("#pc-error").textContent = job.failure_json
      ? `${job.failure_json.code || "任务失败"}：${job.failure_json.message || job.error || ""}${runtimeDiagnostics ? ` · ${runtimeDiagnostics}` : ""}`
      : (runtimeDiagnostics || job.error || "");
    const events = $("#pc-events");
    if (events) {
      events.innerHTML = (job.events || []).slice(-24).map(event =>
        `<div class="meta"><span class="num">${esc(event.timestamp || "")}</span> · ${esc(event.type || event.event || "event")} ${esc(event.dimension || event.title || "")}</div>`
      ).join("");
    }
    const latestQuestion = (job.interview_questions || []).slice(-1)[0];
    const interview = $("#pc-interview");
    if (interview) interview.hidden = !latestQuestion || !["waiting_for_materials", "completed_with_gaps"].includes(job.status);
    if (latestQuestion) $("#pc-interview-question").textContent = latestQuestion.question || "请补充资料";
    $("#pc-pause").hidden = !["planning", "researching", "ingesting_sources", "extracting", "compiling"].includes(job.status);
    $("#pc-resume").hidden = !["paused", "paused_runtime_unavailable", "waiting_for_materials"].includes(job.status);
    const resumeModel = $("#pc-resume-model");
    if (resumeModel) resumeModel.hidden = !["paused", "paused_runtime_unavailable"].includes(job.status);
    $("#pc-continue").hidden = !["completed", "completed_with_gaps"].includes(job.status);
    const retry = $("#pc-retry");
    if (retry) {
      retry.hidden = !BACKGROUND_FAILED.has(job.status) || !(
        job.retry_available === true || (job.failure_json && job.failure_json.retriable === true)
      );
      retry.disabled = false;
    }
    const retryModel = $("#pc-retry-model");
    if (retryModel) retryModel.hidden = !BACKGROUND_FAILED.has(job.status);
    $("#pc-cancel").hidden = BACKGROUND_TERMINAL.has(job.status);
    if (["completed", "completed_with_gaps"].includes(job.status)) {
      $("#pc-start").hidden = true;
      loadPersonas();
      loadProfiles();
    }
  }

  async function pollPersonaCreationJob(jobId) {
    const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(jobId)}`);
    if (!res || !res.ok) return;
    state.personaCreationJob = res.data;
    renderPersonaCreationJob(res.data);
    if (BACKGROUND_TERMINAL.has(res.data.status)) {
      if (state.personaCreationPoll) clearInterval(state.personaCreationPoll);
      state.personaCreationPoll = null;
      loadBackgroundJobs();
    }
  }

  // ─── Persona Creation: model switch on pause/failure ────────────────────
  // Arms the create-form runtime selectors so the next submit retargets the
  // paused/failed job instead of creating a new one.  Persisted evidence,
  // batch checkpoints, and completed dimensions are reused automatically.
  function armPersonaCreationModelSwitch(action) {
    const job = state.personaCreationJob;
    if (!job) return;
    state.personaCreationModelSwitch = { action, jobId: job.id };
    const progressDialog = $("#dlg-persona-creation-progress");
    if (progressDialog && progressDialog.open) progressDialog.close();
    const start = $("#pc-start");
    if (start) start.textContent = action === "retry" ? "更换模型后重试" : "更换模型并继续";
    const form = $("#form-persona-create");
    if (form) {
      const progressCard = $("#pc-progress");
      if (progressCard && progressCard.parentElement !== form) form.appendChild(progressCard);
      setupPersonaCreationRuntimeSelectors();
      const dlg = $("#dlg-persona-create");
      if (dlg && !dlg.open) dlg.showModal();
    }
  }

  async function submitPersonaCreation(e) {
    e.preventDefault();
    const error = $("#pc-error-form");
    const start = $("#pc-start");
    if (error) error.hidden = true;
    // A model-switch submit retargets the existing paused/failed job from the
    // runtime selectors instead of starting a new creation.
    const modelSwitch = state.personaCreationModelSwitch;
    if (modelSwitch && modelSwitch.jobId) {
      const runtime = {
        runtime_source: $("#pc-runtime-source").value || "local_cli",
        agent_id: $("#pc-agent").value || "",
        model_id: $("#pc-model").value || null,
        reasoning_effort: $("#pc-reasoning").value || null,
      };
      try {
        const endpoint = modelSwitch.action === "retry" ? "/retry" : "/resume";
        const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(modelSwitch.jobId)}${endpoint}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ runtime })
        });
        if (!res || !res.ok) throw new Error((res && res.error) || "更换执行目标失败");
        state.personaCreationModelSwitch = null;
        if (start) start.textContent = "开始创建";
        $("#dlg-persona-create").close();
        state.personaCreationJob = res.data;
        openPersonaCreationProgressDialog(res.data);
        if (state.personaCreationPoll) clearInterval(state.personaCreationPoll);
        state.personaCreationPoll = setInterval(() => pollPersonaCreationJob(res.data.id), 1000);
      } catch (err) {
        if (error) {
          error.textContent = `更换执行目标失败：${err.message || err}`;
          error.hidden = false;
        }
      }
      return;
    }
    const type = $("#pc-persona-type").value;
    const mode = $("#pc-creation-mode").value;
    const source = $("#pc-runtime-source").value;
    const agent = $("#pc-agent").value;
    const model = $("#pc-model").value;
    const reasoning = $("#pc-reasoning").value || null;
    const materialText = $("#pc-materials").value.trim();
    const materials = materialText ? materialText.split(/\n\s*\n/).filter(Boolean).map((content, index) => ({
      title: `User material ${index + 1}`,
      source_type: "user_provided",
      content
    })) : [];
    const files = Array.from($("#pc-material-files")?.files || []);
    for (const file of files) {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let index = 0; index < bytes.length; index += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
      }
      materials.push({
        title: file.name,
        filename: file.name,
        source_type: "user_file",
        content_base64: btoa(binary)
      });
    }
    const profile = $("#pc-policy").value;
    const researchPolicy = { profile };
    if (profile === "custom") {
      researchPolicy.min_unique_sources = Number($("#pc-custom-min").value || 30);
      researchPolicy.preferred_source_target = Number($("#pc-custom-target").value || 60);
      researchPolicy.soft_max_sources = Number($("#pc-custom-soft").value || 100);
      researchPolicy.hard_max_sources = Number($("#pc-custom-hard").value || 150);
      researchPolicy.min_source_categories = Number($("#pc-custom-categories").value || 6);
      researchPolicy.min_sources_per_dimension = Number($("#pc-custom-dimension").value || 4);
    }
    const payload = {
      display_name: $("#pc-display-name").value.trim(),
      aliases: $("#pc-aliases").value.split(",").map(value => value.trim()).filter(Boolean),
      persona_type: type,
      creation_mode: mode,
      birth_date: $("#pc-birth-date").value || null,
      death_date: $("#pc-death-date").value || null,
      data_cutoff_date: $("#pc-data-cutoff-date").value || null,
      runtime_source: source,
      agent_id: agent,
      model_id: model,
      reasoning_effort: reasoning,
      research_policy: researchPolicy,
      materials,
      remote_material_consent: $("#pc-remote-consent").checked,
      ...(state.personaCreationExistingId
        ? { existing_persona_id: state.personaCreationExistingId, duplicate_action: "enrich" }
        : {})
    };
    if (!payload.display_name || !agent || !model) {
      if (error) { error.hidden = false; error.textContent = "名称、READY Runtime 和模型能力均为必填。"; }
      return;
    }
    start.disabled = true;
    start.textContent = "正在创建任务…";
    let res = await api("/api/persona-creation/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (res && !res.ok && res.error === "persona_already_exists" && res.details && res.details.persona_id) {
      const enrich = window.confirm(`已存在 Persona：${res.details.display_name || res.details.persona_id}\n是否继续补充已有 Persona？`);
      if (enrich) {
        payload.existing_persona_id = res.details.persona_id;
        payload.duplicate_action = "enrich";
        res = await api("/api/persona-creation/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
      }
    }
    if (!res || !res.ok) {
      if (error) { error.hidden = false; error.textContent = (res && res.error) || "Persona Creation job 创建失败"; }
      start.disabled = false;
      start.textContent = "开始创建";
      return;
    }
    state.personaCreationJob = res.data;
    start.hidden = true;
    $("#dlg-persona-create").close();
    openPersonaCreationProgressDialog(res.data);
    if (state.personaCreationPoll) clearInterval(state.personaCreationPoll);
    state.personaCreationPoll = setInterval(() => pollPersonaCreationJob(res.data.id), 1000);
    toast("Persona Creation Job 已启动");
  }

  async function submitPersonaInterviewAnswer() {
    const job = state.personaCreationJob;
    const answer = $("#pc-interview-answer").value.trim();
    const question = (job && job.interview_questions || []).slice(-1)[0];
    if (!job || !answer) return;
    const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(job.id)}/interview-answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer, dimension: question && question.dimension })
    });
    if (res && res.ok) {
      $("#pc-interview-answer").value = "";
      state.personaCreationJob = res.data;
      renderPersonaCreationJob(res.data);
    } else toast((res && res.error) || "回答提交失败");
  }

  function renderAll() {
    renderRooms();
    renderWorlds();
    renderAgents();
    renderApiProfiles();
    renderPersonas();
    if (state.roomSub === "lobby") {
      renderSlots();
      renderBindingPreview();
    }
    if (state.narrProject) renderNarrativeHeader();
  }

  // ─── Confirmation Modal ──────────────────────────────────────────────────
  function confirmDlg(body, yesText, fn, isDanger = false) {
    const dlg = $("#dlg-confirm");
    if (!dlg) {
      if (window.confirm(body)) {
        Promise.resolve(fn()).catch(console.error);
      }
      return;
    }
    const titleEl = $("#confirm-title");
    const bodyEl = $("#confirm-body");
    const yesBtn = $("#confirm-yes");
    const noBtn = $("#confirm-no");

    if (titleEl) titleEl.textContent = t("confirm") || "确认";
    if (bodyEl) bodyEl.textContent = body;
    if (yesBtn) {
      yesBtn.textContent = yesText || t("confirm") || "确定";
      yesBtn.className = isDanger ? "btn btn-danger" : "btn btn-secondary";
      yesBtn.onclick = async (e) => {
        e.preventDefault();
        try { dlg.close(); } catch (_) {}
        try {
          await fn();
        } catch (err) {
          console.error("Confirm callback error:", err);
          toast("操作失败: " + err);
        }
      };
    }
    if (noBtn) {
      noBtn.onclick = (e) => {
        e.preventDefault();
        try { dlg.close(); } catch (_) {}
      };
    }
    try {
      if (dlg.open) dlg.close();
      dlg.showModal();
    } catch (err) {
      console.warn("dialog showModal failed, falling back to window.confirm", err);
      if (window.confirm(body)) {
        Promise.resolve(fn()).catch(console.error);
      }
    }
  }

  async function deleteWorldAction(wid) {
    if (!wid) return;
    toast("正在删除平行世界...");
    let res = await api(`/api/worlds/${wid}`, { method: "DELETE" });
    if (!res || !res.ok) {
      res = await api(`/api/worlds/${wid}/delete`, { method: "POST" });
    }
    if (res && res.ok) {
      state.worlds = state.worlds.filter((w) => w.id !== wid);
      if (state.currentWorld && state.currentWorld.id === wid) {
        state.currentWorld = null;
        state.currentBranchId = null;
        showWorldSub("list");
      }
      await loadWorlds();
      renderWorlds();
      toast(state.lang === "zh-CN" ? "平行世界推演已彻底删除" : "Parallel world simulation deleted successfully");
    } else {
      const errMsg = (res && res.error) || "网络或服务器错误";
      toast("删除失败: " + errMsg);
    }
  }


  // ─── Narrative Studio ────────────────────────────────────────────────────
  // Localized display names for runtime stages / writer-room roles. The raw
  // arrays above stay as canonical (English) identifiers for the data layer.
  const NARR_STAGE_I18N = {
    "Story Architect": "stageStoryArchitect", "Outline Writer": "stageOutlineWriter",
    "Forecast Simulator": "stageForecastSimulator", "Scene Actor": "stageSceneActor",
    "Screenwriter": "stageScreenwriter", "Reviewer": "stageReviewer",
    "Production Planner": "stageProductionPlanner", "Director": "stageDirector",
    "Shooting Agent": "stageShootingAgent",
  };
  const NARR_ROLE_I18N = {
    "Head Writer": "roleHeadWriter", "Story Architect": "stageStoryArchitect",
    "Character Editor": "roleCharacterEditor", "Mystery Editor": "roleMysteryEditor",
    "Continuity Editor": "roleContinuityEditor", "Commercial Editor": "roleCommercialEditor",
  };
  const i18nLabel = (map, label) => (map[label] && t(map[label])) || label;

  const NARRATIVE_RUNTIME_STAGES = [
    ["story_architect", "Story Architect"],
    ["outline_writer", "Outline Writer"],
    ["forecast_simulator", "Forecast Simulator"],
    ["scene_actor", "Scene Actor"],
    ["screenwriter", "Screenwriter"],
    ["reviewer", "Reviewer"],
    ["production_planner", "Production Planner"],
    ["director", "Director"],
    ["shooting_agent", "Shooting Agent"],
  ];
  const NARRATIVE_JOB_LABELS = {
    story_bible: "正在生成 Story Bible",
    outline: "正在生成全剧大纲",
    forecast: "正在推演 Forecast",
    simulation: "正在进行 Persona 排练",
    writer_room: "正在运行 Writer Room",
    screenwriter: "正在生成剧本",
    audit: "正在审核连续性",
    production_package: "正在生成制作包",
  };
  const NARRATIVE_TABS = ["settings", "bible", "cast", "outline", "episode", "continuity", "production"];
  const WRITER_ROOM_ROLES = [
    ["head_writer", "Head Writer"],
    ["story_architect", "Story Architect"],
    ["character_editor", "Character Editor"],
    ["mystery_editor", "Mystery Editor"],
    ["continuity_editor", "Continuity Editor"],
    ["commercial_editor", "Commercial Editor"],
  ];

  function csvList(value) {
    return String(value || "").split(/[,，]/).map(item => item.trim()).filter(Boolean);
  }

  function listCsv(items) {
    return (items || []).join(", ");
  }

  function linesList(value) {
    return String(value || "").split("\n").map(item => item.trim()).filter(Boolean);
  }

  function narrFormatLabel(format) {
    return ({
      micro_drama: "微短剧",
      series: "剧集",
      novel: "长篇",
      interactive_story: "互动故事",
      custom: "自定义",
    })[format] || format || "作品";
  }

  function narrAgentLabel(agentId) {
    const agent = (state.agents || []).find(item => item.id === agentId);
    return (agent && (agent.name || agent.id)) || agentId || "未设置";
  }

  function syncNarrAdvancedUi() {
    document.body.classList.toggle("narr-advanced", !!state.narrAdvanced);
    const box = $("#narr-advanced-mode");
    if (box) box.checked = !!state.narrAdvanced;
    const sel = $("#narr-generation-mode");
    if (!sel) return;
    let autoOpt = sel.querySelector("option[value='auto']");
    if (state.narrAdvanced && !autoOpt) {
      autoOpt = document.createElement("option");
      autoOpt.value = "auto";
      autoOpt.textContent = "AUTO（仅无配置时离线）";
      sel.appendChild(autoOpt);
    } else if (!state.narrAdvanced && autoOpt) {
      if (sel.value === "auto") sel.value = "agent";
      autoOpt.remove();
    }
  }

  function assignmentDefault() {
    return ((state.narrProject || {}).runtime_assignment || {}).default || {};
  }

  function narrativeGenerationMode() {
    const fromForm = ($("#narr-generation-mode") || {}).value;
    if (fromForm) return fromForm;
    const stored = ((state.narrProject || {}).runtime_assignment || {}).generation_mode;
    return stored || "agent";
  }

  function narrativeRuntime(stage) {
    const assignment = ((state.narrProject || {}).runtime_assignment || {});
    const override = assignment[stage] || {};
    const inherit = !override.agent_id && !override.agent;
    const cfg = inherit ? (assignment.default || {}) : override;
    const agent = (cfg.agent_id || cfg.agent || "").trim();
    const model = (cfg.model_id || cfg.model || "default").trim();
    const reasoning = (cfg.reasoning_effort || cfg.reasoning || "none").trim();
    return agent ? {
      runtime_source: runtimeSourceOf(cfg),
      agent_id: agent,
      model_id: model || "default",
      reasoning_effort: reasoning || "none",
    } : null;
  }

  function narrativePayload(stage, extra) {
    return { generation_mode: narrativeGenerationMode(), runtime: narrativeRuntime(stage), ...(extra || {}) };
  }

  function writerRoomParticipants() {
    const configured = ((((state.narrProject || {}).runtime_assignment || {}).writer_room) || {}).participants;
    return Array.isArray(configured) ? configured.filter(item => item && item.persona_id && item.role) : [];
  }

  function currentEpisode() {
    return (state.narrEpisodes || []).find(item => item.episode_number === state.narrEpNumber) || null;
  }

  function latestEpisodeVersion(ep) {
    const versions = (ep && ep.versions) || [];
    return versions[0] || null;
  }

  function fieldEl(id) {
    if (!id) return null;
    return $(String(id).charAt(0) === "#" ? id : `#${id}`);
  }

  function splitCsvField(id) {
    return csvList((fieldEl(id) || {}).value);
  }

  function setField(id, value) {
    const el = fieldEl(id);
    if (el) el.value = value == null ? "" : value;
  }

  function redactNarrText(value) {
    return String(value || "").replace(/(sk-|api[_-]?key|secret|token)[^\s"']*/gi, "[redacted]");
  }

  function parseNarrativeJobError(job) {
    if (!job || !job.error) return null;
    try {
      const parsed = JSON.parse(job.error);
      if (parsed && typeof parsed === "object") return parsed;
    } catch (_err) {}
    return { message: String(job.error) };
  }

  function runtimeSummary(runtime) {
    if (!runtime || !runtime.agent_id) return "未设置创作模型";
    return `${narrAgentLabel(runtime.agent_id)} · ${runtime.model_id || "default"}`;
  }

  function fillProjectSettingsForm(project, prefix) {
    if (!project) return;
    setField(`${prefix}-title`, project.title || "");
    setField(`${prefix}-logline`, project.logline || "");
    setField(`${prefix}-description`, project.description || "");
    setField(`${prefix}-format`, project.format || "micro_drama");
    setField(`${prefix}-genre`, listCsv(project.genre));
    setField(`${prefix}-tone`, listCsv(project.tone));
    setField(`${prefix}-audience`, project.target_audience || "");
    setField(`${prefix}-episodes`, project.planned_episode_count || 60);
    setField(`${prefix}-dur-min`, project.episode_duration_seconds_min || 90);
    setField(`${prefix}-dur-max`, project.episode_duration_seconds_max || 120);
  }

  function collectProjectSettings(prefix) {
    const valueOf = (suffix) => ((fieldEl(`${prefix}-${suffix}`) || {}).value || "").trim();
    return {
      title: valueOf("title"),
      logline: valueOf("logline"),
      description: valueOf("description"),
      format: valueOf("format") || "micro_drama",
      genre: splitCsvField(`${prefix}-genre`),
      tone: splitCsvField(`${prefix}-tone`),
      target_audience: valueOf("audience"),
      planned_episode_count: parseInt(valueOf("episodes") || "60", 10),
      episode_duration_seconds_min: parseInt(valueOf("dur-min") || "90", 10),
      episode_duration_seconds_max: parseInt(valueOf("dur-max") || "120", 10),
    };
  }

  function renderNarrativeHeader() {
    const project = state.narrProject;
    if (!project) return;
    const title = $("#narr-detail-title");
    const logline = $("#narr-detail-logline");
    const formatLine = $("#narr-detail-format");
    const eyebrow = $("#narr-detail-eyebrow");
    if (title) title.textContent = project.title || "—";
    if (logline) logline.textContent = project.logline || "";
    if (formatLine) {
      formatLine.textContent = `${narrFormatLabel(project.format)} · ${project.planned_episode_count || 0}集 · ${project.episode_duration_seconds_min || 90}–${project.episode_duration_seconds_max || 120}秒`;
    }
    if (eyebrow) eyebrow.textContent = t("narrStudioEyebrow");
    const bibleOk = !!(state.narrBible && (state.narrBible.premise || state.narrBible.version));
    const bound = (state.narrCast || []).filter(item => item.persona_id).length;
    const outlineOk = (state.narrEpisodes || []).length > 0;
    const canonCount = (state.narrEpisodes || []).filter(item => item.status === "canon" || ((item.versions || []).some(v => v.is_canon))).length;
    const planned = project.planned_episode_count || (state.narrEpisodes || []).length || 0;
    const chips = $("#narr-status-chips");
    if (chips) {
      chips.innerHTML = [
        `<span class="tag ${bibleOk ? "ok" : ""}">Story Bible ${bibleOk ? "✓" : "—"}</span>`,
        `<span class="tag ${bound ? "ok" : ""}">角色绑定 ${bound}</span>`,
        `<span class="tag ${outlineOk ? "ok" : ""}">Outline ${outlineOk ? "✓" : "—"}</span>`,
        `<span class="tag ${canonCount ? "ok" : ""}">Canon EP ${canonCount}/${planned || 0}</span>`,
      ].join("");
    }
    syncNarrAdvancedUi();
  }

  function showNarrativeSub(sub) {
    const list = $("#sub-narrative-list");
    const detail = $("#sub-narrative-detail");
    if (list) list.hidden = sub !== "list";
    if (detail) detail.hidden = sub !== "detail";
    if (sub === "list") {
      state.narrProject = null;
      closeDirectorPanel();
      closeShootingPanel();
    }
    // The Director / Shooting summon buttons float at the bottom-left so they
    // stay reachable anywhere inside a project, regardless of scroll position.
    const fab = $("#btn-narr-director");
    if (fab) fab.hidden = sub !== "detail";
    const shootFab = $("#btn-narr-shooting");
    if (shootFab) shootFab.hidden = sub !== "detail";
  }

  function showNarrativeTab(tab) {
    state.narrTab = tab;
    NARRATIVE_TABS.forEach(name => {
      const panel = $(`#narr-tab-${name}`);
      if (panel) panel.hidden = name !== tab;
    });
    $$("#narr-tabs button").forEach(btn => btn.classList.toggle("is-active", btn.dataset.narrTab === tab));
    if (tab === "episode") renderEpisodeWorkbench();
    if (tab === "settings") fillSettingsFromProject();
  }

  function fillSettingsFromProject() {
    fillProjectSettingsForm(state.narrProject, "narr-set");
  }

  function unmatchedBibleCharacters() {
    const bibleChars = ((state.narrBible || {}).characters) || [];
    const cast = state.narrCast || [];
    return bibleChars.filter(bibleChar => {
      const name = String(bibleChar.name || "").trim().toLowerCase();
      return !cast.some(character =>
        (bibleChar.id && character.id === bibleChar.id) ||
        String(character.name || "").trim().toLowerCase() === name
      );
    });
  }

  function renderBibleSyncBanner() {
    const host = $("#narr-bible-sync");
    if (!host) return;
    const unmatched = unmatchedBibleCharacters();
    if (!state.narrBible || !unmatched.length) {
      host.hidden = true;
      host.innerHTML = "";
      return;
    }
    host.hidden = false;
    host.innerHTML = `
      <div class="row-between" style="gap:10px;flex-wrap:wrap;">
        <div><strong>发现 ${unmatched.length} 个 Story Bible 角色</strong>
          <p class="meta" style="margin-top:4px;">同步到角色阵容时按名称 / ID 匹配，不会覆盖已有 Persona 绑定。</p></div>
        <div class="row" style="gap:8px;">
          <button class="btn btn-sm btn-secondary" id="btn-narr-sync-all" type="button">全选并同步</button>
        </div>
      </div>
      <div class="stack" style="gap:6px;margin-top:10px;">
        ${unmatched.map(character => `
          <label class="narr-check">
            <input type="checkbox" class="narr-sync-char" data-bible-id="${esc(character.id || "")}" data-name="${esc(character.name || "")}" checked />
            ${esc(character.name || "未命名")} <span class="meta">${esc(character.role || "")}</span>
          </label>`).join("")}
        <button class="btn btn-sm btn-primary" id="btn-narr-sync-selected" type="button">同步到角色阵容</button>
      </div>`;
    const syncSelected = async (all) => {
      const boxes = all ? $$(".narr-sync-char") : $$(".narr-sync-char:checked");
      const selected = boxes.map(box => unmatched.find(item =>
        (item.id && item.id === box.dataset.bibleId) || item.name === box.dataset.name
      )).filter(Boolean);
      await syncBibleCharactersToCast(selected);
    };
    onClick("#btn-narr-sync-all", () => syncSelected(true));
    onClick("#btn-narr-sync-selected", () => syncSelected(false));
  }

  async function syncBibleCharactersToCast(characters) {
    if (!state.narrProject || !characters.length) return;
    let created = 0;
    for (const character of characters) {
      const payload = {
        name: character.name,
        role: character.role || "",
        description: character.description || "",
      };
      if (character.id) payload.id = character.id;
      const res = await api(`/api/narratives/${state.narrProject.id}/characters`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res && res.ok) created += 1;
    }
    toast(created ? `已同步 ${created} 个角色` : "没有新角色需要同步");
    await loadNarrativeCast();
    renderBibleSyncBanner();
    renderNarrativeHeader();
  }

  function populateNarrativeRuntimeModal({ preserveDraft = true } = {}) {
    if (!state.narrProject) return;
    const assignment = state.narrProject.runtime_assignment || {};
    const savedDefault = assignment.default || {};
    const liveDefault = preserveDraft ? liveRuntimeDraft("narr-default") : null;
    bindSharedRuntimeSelector("narr-default", mergeRuntimeDraft(savedDefault, liveDefault));
    renderNarrativeRuntimeStages({ preserveDraft });
  }

  function refreshOpenNarrativeRuntimeModal() {
    const dlg = $("#dlg-narr-runtime");
    if (dlg && dlg.open) populateNarrativeRuntimeModal({ preserveDraft: true });
  }

  function renderNarrativeRuntimeStages(opts) {
    const preserveDraft = !!(opts && opts.preserveDraft);
    const host = $("#narr-runtime-stage-list");
    if (!host) return;
    const assignment = (state.narrProject || {}).runtime_assignment || {};
    const enabledStages = {};
    const liveByStage = {};
    $$(".narr-stage-enable").forEach(box => { enabledStages[box.dataset.stage] = box.checked; });
    NARRATIVE_RUNTIME_STAGES.forEach(([stage]) => {
      liveByStage[stage] = preserveDraft ? liveRuntimeDraft(`narr-${stage}`) : null;
    });
    host.innerHTML = NARRATIVE_RUNTIME_STAGES.map(([stage, label]) => {
      const cfg = assignment[stage] || {};
      const enabled = Object.prototype.hasOwnProperty.call(enabledStages, stage)
        ? enabledStages[stage]
        : !!(cfg.agent_id || cfg.agent);
      return `<div class="narr-stage-override" data-stage="${esc(stage)}">
        <label class="narr-check"><input type="checkbox" class="narr-stage-enable" data-stage="${esc(stage)}" ${enabled ? "checked" : ""} /> ${esc(i18nLabel(NARR_STAGE_I18N, label))} · ${t("narrStageOverride")}</label>
        <p class="meta" style="margin:4px 0 8px;">默认：继承默认创作模型</p>
        <div class="narr-stage-fields" data-stage="${esc(stage)}" ${enabled ? "" : "hidden"}>
          <div class="field"><label>来源</label>
            <div class="row" style="gap:12px;flex-wrap:wrap;">
              <label class="narr-radio"><input type="radio" name="narr-${stage}-source" value="local_cli" /> 本地 CLI</label>
              <label class="narr-radio"><input type="radio" name="narr-${stage}-source" value="api" /> API</label>
            </div>
          </div>
          <div class="grid-3 narr-form-grid">
            <div class="field"><label>Agent</label><select class="input" id="narr-${stage}-agent"></select></div>
            <div class="field"><label>Model</label><select class="input" id="narr-${stage}-model"></select></div>
            <div class="field"><label>Reasoning</label><select class="input" id="narr-${stage}-reasoning"></select></div>
          </div>
          <p class="meta" id="narr-${stage}-reasoning-notice"></p>
        </div>
      </div>`;
    }).join("");
    $$(".narr-stage-enable").forEach(box => {
      box.addEventListener("change", () => {
        const fields = $(`.narr-stage-fields[data-stage="${box.dataset.stage}"]`);
        if (fields) fields.hidden = !box.checked;
        if (box.checked) {
          bindSharedRuntimeSelector(
            `narr-${box.dataset.stage}`,
            mergeRuntimeDraft(assignment[box.dataset.stage] || assignmentDefault(), liveRuntimeDraft(`narr-${box.dataset.stage}`))
          );
        }
      });
    });
    NARRATIVE_RUNTIME_STAGES.forEach(([stage]) => {
      const cfg = assignment[stage] || {};
      const enabled = ($(`.narr-stage-enable[data-stage="${stage}"]`) || {}).checked;
      if (!enabled) return;
      bindSharedRuntimeSelector(
        `narr-${stage}`,
        mergeRuntimeDraft(cfg.agent_id || cfg.agent ? cfg : assignmentDefault(), liveByStage[stage])
      );
    });
  }

  function renderWriterRoomSetup() {
    const host = $("#narr-writer-setup");
    if (!host) return;
    const personas = state.personas || [];
    const current = writerRoomParticipants();
    const options = [`<option value="">未指定</option>`].concat(
      personas.map(persona => `<option value="${esc(persona.id)}">${esc(persona.display_name || persona.id)}</option>`)
    ).join("");
    host.innerHTML = WRITER_ROOM_ROLES.map(([role, label]) => {
      const selected = (current.find(item => item.role === role) || {}).persona_id || "";
      return `<div class="field">
        <label for="narr-writer-${role}">${esc(i18nLabel(NARR_ROLE_I18N, label))}</label>
        <select class="input narr-writer-role" id="narr-writer-${role}" data-role="${esc(role)}">${options}</select>
      </div>`;
    }).join("");
    WRITER_ROOM_ROLES.forEach(([role]) => {
      const sel = $(`#narr-writer-${role}`);
      const selected = (current.find(item => item.role === role) || {}).persona_id || "";
      if (sel && selected) sel.value = selected;
    });
  }

  function collectWriterRoomSetup() {
    return $$(".narr-writer-role").map(sel => {
      const persona = (state.personas || []).find(item => item.id === sel.value);
      return sel.value ? {
        role: sel.dataset.role,
        persona_id: sel.value,
        display_name: (persona && persona.display_name) || sel.dataset.role,
      } : null;
    }).filter(Boolean);
  }

  function openNarrativeRuntimeModal() {
    if (!state.narrProject) return;
    const assignment = state.narrProject.runtime_assignment || {};
    const dlg = $("#dlg-narr-runtime");
    const gen = $("#narr-generation-mode");
    if (gen) gen.value = assignment.generation_mode || "agent";
    syncNarrAdvancedUi();
    populateNarrativeRuntimeModal({ preserveDraft: false });
    renderWriterRoomSetup();
    const err = $("#narr-runtime-error");
    if (err) { err.hidden = true; err.textContent = ""; }
    if (dlg && dlg.showModal) dlg.showModal();
    hydrateRuntimeSelectors(() => {
      if (!dlg || !dlg.open) return;
      populateNarrativeRuntimeModal({ preserveDraft: true });
    });
  }

  async function rescanNarrativeRuntime() {
    const btn = $("#btn-narr-runtime-refresh");
    if (btn) { btn.disabled = true; btn.textContent = "正在刷新…"; }
    try {
      await loadAgents(true);
      populateNarrativeRuntimeModal({ preserveDraft: true });
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "↻ 刷新 Runtime"; }
    }
  }

  async function saveNarrativeRuntimeRouting(event) {
    if (event) event.preventDefault();
    if (!state.narrProject) return;
    const def = readSharedRuntime("narr-default");
    const err = $("#narr-runtime-error");
    if (!def || !def.agent_id) {
      if (err) { err.hidden = false; err.textContent = "请选择 READY / Connected 的默认 Agent。"; }
      return;
    }
    if (!def.model_id) def.model_id = "default";
    if (!def.reasoning_effort) def.reasoning_effort = "none";
    const runtime_assignment = {
      default: def,
      generation_mode: narrativeGenerationMode(),
      writer_room: { participants: collectWriterRoomSetup() },
    };
    NARRATIVE_RUNTIME_STAGES.forEach(([stage]) => {
      const enabled = ($(`.narr-stage-enable[data-stage="${stage}"]`) || {}).checked;
      if (!enabled) return;
      const cfg = readSharedRuntime(`narr-${stage}`);
      if (cfg) runtime_assignment[stage] = cfg;
    });
    const res = await api(`/api/narratives/${state.narrProject.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runtime_assignment }),
    });
    if (res && res.ok) {
      state.narrProject = res.data;
      toast("创作模型已保存");
      const dlg = $("#dlg-narr-runtime");
      if (dlg && dlg.open) dlg.close();
    } else if (err) {
      err.hidden = false;
      err.textContent = (res && res.error) || "保存失败";
    } else toast((res && res.error) || "保存失败");
  }

  function renderNarrativeJob(job) {
    const host = $("#narr-job-status");
    if (!host) return;
    if (!job || ["completed", "failed", "cancelled"].includes(job.status) && !job._keep) {
      if (!job || job.status === "completed") {
        host.hidden = true;
        host.innerHTML = "";
        return;
      }
    }
    if (!job) { host.hidden = true; return; }
    host.hidden = false;
    const progress = job.progress || {};
    const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
    const label = progress.label || NARRATIVE_JOB_LABELS[job.kind] || job.kind;
    const runtime = ((job.result || {}).execution || {}).runtime || ((job.payload || {}).runtime) || narrativeRuntime("default") || assignmentDefault();
    const failure = parseNarrativeJobError(job);
    const running = ["created", "running", "pause_requested"].includes(job.status);
    const paused = job.status === "paused";
    const failed = job.status === "failed";
    const actions = [];
    if (running) {
      actions.push(`<button class="btn btn-sm btn-ghost" data-narr-job="pause" type="button">暂停</button>`);
      actions.push(`<button class="btn btn-sm btn-ghost" data-narr-job="cancel" type="button">取消</button>`);
    }
    if (paused) {
      actions.push(`<button class="btn btn-sm btn-secondary" data-narr-job="resume" type="button">继续</button>`);
      actions.push(`<button class="btn btn-sm btn-ghost" data-narr-job="cancel" type="button">取消</button>`);
    }
    if (failed) actions.push(`<button class="btn btn-sm btn-primary" data-narr-job="retry" type="button">重试</button>`);
    host.innerHTML = `
      <div class="row-between" style="gap:10px;flex-wrap:wrap;">
        <div>
          <strong>${esc(failed && failure ? `${failure.stage || job.kind} 失败` : label)}</strong>
          <p class="meta">Runtime: ${esc(runtimeSummary(runtime))}${progress.current_subtask ? ` · ${esc(progress.current_subtask)}` : ""}</p>
        </div>
        <div class="row" style="gap:6px;">${actions.join("")}</div>
      </div>
      <div class="progress"><div class="progress-bar" style="width:${percent}%"></div></div>
      <p class="meta" style="margin-top:6px;">${percent}%</p>
      ${failure ? `<div class="narr-step-reason">${esc(redactNarrText(failure.message || job.error || ""))}
        ${failure.agent ? `<div class="meta">Agent: ${esc(narrAgentLabel(failure.agent))}</div>` : ""}
        ${failure.requested_model ? `<div class="meta">Model: ${esc(failure.requested_model)}</div>` : ""}
        ${failure.reasoning ? `<div class="meta">Reasoning: ${esc(failure.reasoning)}</div>` : ""}
      </div>` : ""}
      <details class="narr-tech" style="margin-top:8px;"><summary>查看任务详情</summary>
        <p class="meta">Job ${esc(job.id)} · ${esc(job.status)}</p>
      </details>`;
    host.querySelectorAll("[data-narr-job]").forEach(btn => {
      btn.addEventListener("click", () => controlNarrativeJob(btn.dataset.narrJob));
    });
  }

  async function controlNarrativeJob(action) {
    if (!state.narrJob) return;
    const res = await api(`/api/narrative-jobs/${state.narrJob.id}/${action}`, { method: "POST" });
    if (res && res.ok) {
      if (action !== "cancel") {
        state.narrJob = res.data;
        renderNarrativeJob(res.data);
      }
    } else toast((res && res.error) || "任务操作失败");
  }

  async function runNarrativeJob(kind, payload, label) {
    if (!state.narrProject) return null;
    const created = await api(`/api/narratives/${state.narrProject.id}/jobs`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, payload }),
    });
    if (!created || !created.ok) {
      toast((created && created.error) || `${label}任务创建失败`);
      return null;
    }
    let job = created.data;
    state.narrJob = job;
    renderNarrativeJob(job);
    while (!["completed", "failed", "cancelled"].includes(job.status)) {
      await new Promise(resolve => setTimeout(resolve, 800));
      const snapshot = await api(`/api/narrative-jobs/${job.id}`);
      if (!snapshot || !snapshot.ok) {
        toast((snapshot && snapshot.error) || `${label}状态读取失败`);
        return null;
      }
      job = snapshot.data;
      state.narrJob = job;
      renderNarrativeJob(job);
    }
    if (job.status !== "completed") {
      job._keep = true;
      renderNarrativeJob(job);
      return null;
    }
    renderNarrativeJob(null);
    const unresolved = (((job.result || {}).audit || {}).unresolved_after_repair) || [];
    toast(
      kind === "outline" && unresolved.length
        ? `${label}已完成，仍有 ${unresolved.length} 条连续性警告`
        : `${label}已完成`
    );
    return job;
  }

  function renderNarrativeDirections() {
    const host = $("#narr-forecast-directions");
    if (!host) return;
    state.narrDirections = state.narrDirections || [];
    host.innerHTML = state.narrDirections.length ? state.narrDirections.map((item, index) => {
      const label = item.label || String.fromCharCode(65 + index);
      const description = item.description || "";
      // Collapsible candidate route: once a description exists the item
      // starts collapsed to its title row so a long list stays scannable.
      const open = description ? "" : " open";
      return `
      <details class="narr-direction-item"${open}>
        <summary class="narr-direction-summary" title="点击展开 / 收起">
          <input class="input narr-direction-label" data-index="${index}" value="${esc(label)}" style="width:96px;flex:none;" />
          <span class="narr-direction-preview meta">${esc(description)}</span>
          <button class="btn btn-sm btn-ghost narr-direction-delete" data-index="${index}" type="button">删除</button>
        </summary>
        <div class="narr-direction-body">
          <input class="input narr-direction-description" data-index="${index}" value="${esc(description)}" style="width:100%;" placeholder="路线描述…" />
        </div>
      </details>`;
    }).join("") : `<p class="meta">先 AI 生成或手动添加 1-5 条候选路线。</p>`;
    $$(".narr-direction-delete").forEach(btn => btn.addEventListener("click", () => {
      state.narrDirections.splice(parseInt(btn.dataset.index, 10), 1);
      renderNarrativeDirections();
    }));
    // Clicking inputs/buttons inside the title row must not toggle collapse.
    $$("#narr-forecast-directions .narr-direction-summary").forEach(summary => {
      summary.addEventListener("click", (e) => {
        if (e.target.closest("input, button")) e.preventDefault();
      });
    });
    // Keep typed text in state and mirror the description into the title-row
    // preview so a collapsed route still shows what it is about.
    $$("#narr-forecast-directions .narr-direction-description").forEach(input => {
      input.addEventListener("input", () => {
        const idx = parseInt(input.dataset.index, 10);
        if (state.narrDirections[idx]) state.narrDirections[idx].description = input.value;
        const item = input.closest(".narr-direction-item");
        const preview = item && item.querySelector(".narr-direction-preview");
        if (preview) preview.textContent = input.value;
      });
    });
    $$("#narr-forecast-directions .narr-direction-label").forEach(input => {
      input.addEventListener("input", () => {
        const idx = parseInt(input.dataset.index, 10);
        if (state.narrDirections[idx]) state.narrDirections[idx].label = input.value;
      });
    });
  }

  function collectNarrativeDirections() {
    return $$("#narr-forecast-directions .narr-direction-label").map(input => {
      const index = parseInt(input.dataset.index, 10);
      const desc = $(`#narr-forecast-directions .narr-direction-description[data-index="${index}"]`);
      return { label: input.value.trim(), description: desc ? desc.value.trim() : "", beats: [] };
    }).filter(item => item.label && item.description);
  }

  function forecastChangeList(items, key) {
    const rows = (items || []).map(item => {
      if (item == null) return "";
      if (typeof item === "string") return item;
      return item[key] || item.summary || item.name || item.text || item.action || JSON.stringify(item);
    }).filter(Boolean);
    return rows.length ? `<ul>${rows.map(row => `<li>${esc(row)}</li>`).join("")}</ul>` : `<p class="meta">无</p>`;
  }

  function renderNarrativeForecast(forecast) {
    const host = $("#narr-forecast-results");
    if (!host) return;
    if (!forecast || !Array.isArray(forecast.directions)) { host.innerHTML = ""; return; }
    host.innerHTML = forecast.directions.map(direction => {
      const selected = forecast.selected_direction_id === direction.id;
      const evals = direction.evaluation || direction.scores || {};
      const evalRows = Object.keys(evals).map(key => `<li>${esc(key)}：${esc(evals[key])}</li>`).join("");
      const steps = direction.steps || [];
      // Default collapsed: only the title row is visible until clicked.
      return `<details class="card narr-forecast-card">
        <summary class="narr-forecast-summary" title="点击展开 / 收起">
          <strong>${t("narrOption")} ${esc(direction.label)}</strong>
          <span class="narr-noncanon">NON-CANON${selected ? ` · ${t("narrSelectedDir")}` : ""}</span>
        </summary>
        <p style="margin-top:6px;">${esc(direction.summary || direction.description || "")}</p>
        <div class="grid-2 narr-form-grid" style="margin-top:8px;">
          <div><h4>未来剧情</h4>${forecastChangeList(steps, "summary")}</div>
          <div><h4>人物变化</h4>${forecastChangeList(steps.flatMap(step => step.character_decisions || []), "summary")}</div>
          <div><h4>关系变化</h4>${forecastChangeList(steps.flatMap(step => step.relationship_changes || []), "summary")}</div>
          <div><h4>信息变化</h4>${forecastChangeList(steps.flatMap(step => step.knowledge_changes || []), "summary")}</div>
        </div>
        <p><strong>优势</strong>：${esc((direction.evaluation && direction.evaluation.strengths) || (direction.risks && direction.risks.length ? "见风险对照" : "待评估"))}</p>
        <p><strong>风险</strong></p>${forecastChangeList(direction.risks, "summary")}
        <details style="margin-top:8px;"><summary>${t("narrEvalSummary")}</summary>${evalRows ? `<ul>${evalRows}</ul>` : `<p class="meta">暂无评分</p>`}</details>
        <p class="meta">选择方向 ≠ 提交正史</p>
        <button class="btn btn-sm btn-secondary narr-forecast-select" data-forecast="${esc(forecast.id)}" data-direction="${esc(direction.id)}" type="button">选择作为创作方向</button>
      </details>`;
    }).join("");
    $$(".narr-forecast-select").forEach(btn => btn.addEventListener("click", async () => {
      const res = await api(`/api/narratives/${state.narrProject.id}/forecasts/${btn.dataset.forecast}/select`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction_id: btn.dataset.direction }),
      });
      if (res && res.ok) { state.narrForecast = res.data; renderNarrativeForecast(res.data); toast("方向已选择；尚未提交正史"); }
      else toast((res && res.error) || "选择失败");
    }));
  }

  async function generateNarrativeDirections() {
    if (!state.narrProject) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/episodes/${state.narrEpNumber}/forecast/directions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(narrativePayload("forecast_simulator", { count: 3 })),
    });
    if (res && res.ok) {
      state.narrDirections = res.data.directions || [];
      renderNarrativeDirections();
      toast("候选方向已生成");
    } else toast((res && res.error) || "候选路线生成失败");
  }

  async function runNarrativeForecast() {
    if (!state.narrProject) return;
    const directions = collectNarrativeDirections();
    if (!directions.length) { toast("请先添加候选路线"); return; }
    const horizon = parseInt(($("#narr-forecast-horizon") || {}).value || "3", 10);
    const job = await runNarrativeJob("forecast", narrativePayload("forecast_simulator", {
      episode_number: state.narrEpNumber, directions, horizon_episodes: horizon,
    }), "Forecast（NON-CANON）");
    if (!job) return;
    await loadNarrativeForecasts();
  }

  function rehearsalDefaults() {
    const boundIds = (state.narrCast || []).filter(item => item.persona_id).map(item => item.id);
    state.narrRehearsal = state.narrRehearsal || { location: "", background: "", goal: "", participantIds: null };
    if (!state.narrRehearsal.participantIds) state.narrRehearsal.participantIds = boundIds.slice();
    return state.narrRehearsal;
  }

  function captureRehearsalForm() {
    const form = rehearsalDefaults();
    form.location = (($("#narr-rehearse-location") || {}).value || "").trim();
    form.background = (($("#narr-rehearse-background") || {}).value || "").trim();
    form.goal = (($("#narr-rehearse-goal") || {}).value || "").trim();
    form.participantIds = $$(".narr-rehearse-actor:checked").map(box => box.value);
  }

  function renderRehearsalResults(scene) {
    const host = $("#narr-simulation-results");
    if (!host) return;
    if (!scene) { host.innerHTML = `<p class="meta">尚未排练。</p>`; return; }
    const dialogue = scene.dialogue || [];
    const decisions = scene.decisions || [];
    host.innerHTML = `
      <p class="narr-noncanon">NON-CANON · Persona 排练</p>
      <div class="narr-dialogue" style="margin-top:8px;">
        ${dialogue.length ? dialogue.map(line => `<div class="narr-line"><strong>${esc(line.speaker || line.name || "角色")}</strong><span>“${esc(line.text || line.line || "")}”</span></div>`).join("") : `<p class="meta">暂无对白</p>`}
      </div>
      <div class="grid-2 narr-form-grid" style="margin-top:10px;">
        <div><h4>关键决定</h4>${forecastChangeList(decisions, "decision")}</div>
        <div><h4>人物状态变化</h4>${forecastChangeList(scene.affect_delta, "summary")}</div>
        <div><h4>关系变化</h4>${forecastChangeList(scene.relationship_delta, "summary")}</div>
        <div><h4>${t("knowledgeDelta")}</h4>${forecastChangeList(scene.knowledge_delta, "summary")}</div>
      </div>
      <details class="narr-tech" style="margin-top:8px;"><summary>技术详情</summary><pre style="white-space:pre-wrap;font-size:11px;">${esc(JSON.stringify(scene, null, 2))}</pre></details>`;
  }

  async function runNarrativeSimulation() {
    if (!state.narrProject) return;
    captureRehearsalForm();
    const form = rehearsalDefaults();
    const selected = (state.narrCast || []).filter(item => item.persona_id && (form.participantIds || []).includes(item.id));
    if (!selected.length) { toast("Persona 排练仅允许已绑定 Persona 的角色"); return; }
    const ep = currentEpisode();
    const participants = selected.map(character => ({
      character_id: character.id, name: character.name, goal: form.goal || `推进 EP${state.narrEpNumber}`,
    }));
    const created = await api(`/api/narratives/${state.narrProject.id}/episodes/${state.narrEpNumber}/scenes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        episode_number: state.narrEpNumber,
        order: ((ep && ep.scenes) || []).length + 1,
        location: form.location || "待定场景",
        scene_goal: form.goal || (ep && ep.narrative_goal) || `EP${state.narrEpNumber}`,
        conflict: form.background || "",
        participants,
      }),
    });
    if (!created || !created.ok) { toast((created && created.error) || "场景创建失败"); return; }
    const scene = created.data;
    const selectedDir = state.narrForecast && state.narrForecast.directions
      ? state.narrForecast.directions.find(item => item.id === state.narrForecast.selected_direction_id)
      : null;
    const job = await runNarrativeJob("simulation", narrativePayload("scene_actor", {
      episode_number: state.narrEpNumber, scene_id: scene.id,
      branch_id: selectedDir ? selectedDir.world_branch_id : null,
    }), "Persona 排练（NON-CANON）");
    if (!job) return;
    await loadNarrativeEpisodes();
    const refreshed = currentEpisode();
    const simulatedId = (job.result && job.result.scene_id) || scene.id;
    const simulated = ((refreshed && refreshed.scenes) || []).find(item => item.id === simulatedId) || scene;
    renderRehearsalResults(simulated);
    renderEpisodeWorkbench();
  }

  async function runNarrativeWriterRoom() {
    if (!state.narrProject) return;
    const configured = writerRoomParticipants();
    if (!configured.some(item => item.role === "head_writer")) {
      toast("请先在创作模型设置中配置 Writer Personas / Writer Roles");
      return;
    }
    const runtime = narrativeRuntime("screenwriter") || narrativeRuntime("story_architect");
    const participants = configured.map(item => ({
      ...item,
      runtime_selection: (runtime && runtime.agent_id) || "",
      model_selection: (runtime && runtime.model_id) || "default",
      reasoning_selection: (runtime && runtime.reasoning_effort) || "none",
    }));
    const job = await runNarrativeJob("writer_room", {
      episode_number: state.narrEpNumber, participants, cross_review: true,
    }, "Writer Room");
    if (!job) return;
    const host = $("#narr-writer-room-results");
    if (host) {
      const synthesis = (job.result && job.result.synthesis) || {};
      host.innerHTML = `<div class="card"><strong>Head Writer Synthesis</strong>
        <p style="margin-top:6px;">${esc(synthesis.final_writer_instruction || synthesis.recommended_direction || "已完成")}</p>
        <details class="narr-tech"><summary>技术详情</summary><pre style="white-space:pre-wrap;font-size:11px;">${esc(JSON.stringify(job.result || {}, null, 2))}</pre></details></div>`;
    }
    await loadNarrativeEpisodes();
  }

  function pipelineState(ep) {
    const plan = ep;
    const version = latestEpisodeVersion(ep);
    const audit = (ep && ep.latest_audit) || null;
    const scenes = (ep && ep.scenes) || [];
    const rehearsed = scenes.some(scene => (scene.dialogue || []).length || scene.status === "completed");
    const forecast = state.narrForecast;
    const blocking = audit && (audit.blocking_count > 0 || audit.passed === false);
    const canon = !!(ep && (ep.status === "canon" || (version && version.is_canon)));
    const pkg = (state.narrProduction || []).find(item => item.episode_number === state.narrEpNumber);
    return {
      plan: { status: plan ? "done" : "todo", disabled: false },
      forecast: { status: forecast ? "done" : "optional", optional: true, disabled: !plan, reason: plan ? "" : "请先生成全剧大纲 / 剧集计划" },
      rehearsal: { status: rehearsed ? "done" : "optional", optional: true, disabled: !plan, reason: plan ? "" : "请先生成剧集计划" },
      draft: { status: version ? "done" : "todo", disabled: !plan, reason: "没有 EpisodePlan，无法生成剧本" },
      audit: { status: audit ? (blocking ? "needs" : "done") : "todo", disabled: !version, reason: "没有 Draft，无法审核" },
      commit: {
        status: canon ? "done" : (blocking ? "blocked" : (audit ? "todo" : "todo")),
        disabled: !version || !audit || blocking,
        reason: !version ? "请先生成剧本" : (!audit ? "请先完成连续性审核" : (blocking ? "Audit 存在 BLOCKING 问题" : "")),
      },
      production: {
        status: pkg ? "done" : "todo",
        disabled: false,
        warning: !canon,
        reason: canon ? "" : "尚未提交正史，制作包可能不是最终版本",
      },
    };
  }

  function statusBadge(step) {
    if (step.optional && step.status === "optional") return `<span class="tag">可选</span>`;
    if (step.status === "done") return `<span class="tag ok">完成</span>`;
    if (step.status === "blocked" || step.status === "needs") return `<span class="tag danger">需要处理</span>`;
    if (step.disabled) return `<span class="tag">未开始</span>`;
    return `<span class="tag">未开始</span>`;
  }

  function renderEpisodeRail() {
    const host = $("#narr-ep-rail");
    if (!host) return;
    const episodes = state.narrEpisodes || [];
    if (!episodes.length) {
      host.innerHTML = `<p class="meta" style="padding:8px;">暂无剧集，请先生成大纲。</p>`;
      return;
    }
    host.innerHTML = episodes.map(ep => `
      <button type="button" class="${ep.episode_number === state.narrEpNumber ? "is-active" : ""}" data-ep="${ep.episode_number}">
        EP${String(ep.episode_number).padStart(2, "0")}
      </button>`).join("");
    host.querySelectorAll("button[data-ep]").forEach(btn => btn.addEventListener("click", () => {
      state.narrEpNumber = parseInt(btn.dataset.ep, 10);
      const forecast = (state.narrForecasts || []).find(item => item.episode_number === state.narrEpNumber);
      state.narrForecast = forecast || null;
      renderEpisodeWorkbench();
    }));
  }

  function renderEpisodePlan() {
    const host = $("#narr-ep-plan");
    if (!host) return;
    const ep = currentEpisode();
    if (!ep) {
      host.innerHTML = `<h3>当前 Episode Plan</h3><p class="meta">还没有剧集计划。请先到「全剧大纲」生成。</p>`;
      return;
    }
    host.innerHTML = `
      <div class="row-between"><h3>EP${String(ep.episode_number).padStart(2, "0")} ${esc(ep.title || "")}</h3><span class="tag">${esc(ep.status || "planned")}</span></div>
      <p style="margin-top:8px;"><strong>目标</strong>：${esc(ep.narrative_goal || "—")}</p>
      <p><strong>钩子</strong>：${esc(ep.hook || "—")}</p>
      <p><strong>悬念</strong>：${esc(ep.cliffhanger || "—")}</p>`;
  }

  function renderEpisodeWorkbench() {
    renderEpisodeRail();
    renderEpisodePlan();
    const host = $("#narr-pipeline");
    if (!host) return;
    const ep = currentEpisode();
    const pipe = pipelineState(ep);
    const form = rehearsalDefaults();
    const version = latestEpisodeVersion(ep);
    const audit = (ep && ep.latest_audit) || null;
    const pkg = (state.narrProduction || []).find(item => item.episode_number === state.narrEpNumber);
    const bound = (state.narrCast || []).filter(item => item.persona_id);
    host.innerHTML = `
      <div class="card narr-step ${pipe.plan.disabled ? "is-disabled" : ""}">
        <div class="narr-step-head"><div><strong>1 剧集计划</strong>${statusBadge(pipe.plan)}</div></div>
        <p class="meta">${ep ? "已从全剧大纲载入当前集。" : "未开始：请先生成全剧大纲。"}</p>
      </div>
      <div class="card narr-step ${pipe.forecast.disabled ? "is-disabled" : ""}" id="narr-forecast-panel">
        <div class="narr-step-head"><div><strong>2 Forecast</strong>${statusBadge(pipe.forecast)} <span class="narr-noncanon">NON-CANON</span></div>
          <div class="row" style="gap:8px;flex-wrap:wrap;">
            <label class="meta" for="narr-forecast-horizon">${t("narrHorizon")}</label>
            <input class="input" id="narr-forecast-horizon" type="number" min="1" max="5" value="3" style="width:70px;" ${pipe.forecast.disabled ? "disabled" : ""} />
            <button class="btn btn-sm btn-secondary" id="btn-narr-forecast-generate" type="button" ${pipe.forecast.disabled ? "disabled" : ""}>AI生成候选方向</button>
            <button class="btn btn-sm btn-secondary" id="btn-narr-forecast-add" type="button" ${pipe.forecast.disabled ? "disabled" : ""}>手动方向</button>
            <button class="btn btn-sm btn-primary" id="btn-narr-forecast-run" type="button" ${pipe.forecast.disabled ? "disabled" : ""}>开始推演</button>
          </div>
        </div>
        ${pipe.forecast.reason ? `<p class="narr-step-reason">${esc(pipe.forecast.reason)}</p>` : ""}
        <p class="meta">选择方向 ≠ 提交正史</p>
        <div id="narr-forecast-directions" class="stack" style="gap:6px;margin-top:10px;"></div>
        <div id="narr-forecast-results" class="stack" style="gap:8px;margin-top:10px;"></div>
      </div>
      <div class="card narr-step ${pipe.rehearsal.disabled ? "is-disabled" : ""}">
        <div class="narr-step-head"><div><strong>3 Persona 排练</strong>${statusBadge(pipe.rehearsal)} <span class="narr-noncanon">NON-CANON</span></div>
          <button class="btn btn-sm btn-primary" id="btn-narr-simulate" type="button" ${pipe.rehearsal.disabled ? "disabled" : ""}>开始排练</button>
        </div>
        <p class="meta">让绑定的人格在 NON-CANON 场景中自由互动，作为编剧参考。</p>
        ${pipe.rehearsal.reason ? `<p class="narr-step-reason">${esc(pipe.rehearsal.reason)}</p>` : ""}
        <div class="grid-2 narr-form-grid" style="margin-top:8px;">
          <div class="field"><label for="narr-rehearse-location">场景地点</label><input class="input" id="narr-rehearse-location" value="${esc(form.location)}" ${pipe.rehearsal.disabled ? "disabled" : ""} /></div>
          <div class="field"><label for="narr-rehearse-goal">场景目标</label><input class="input" id="narr-rehearse-goal" value="${esc(form.goal)}" ${pipe.rehearsal.disabled ? "disabled" : ""} /></div>
        </div>
        <div class="field"><label for="narr-rehearse-background">场景背景</label><textarea class="textarea" id="narr-rehearse-background" rows="2" ${pipe.rehearsal.disabled ? "disabled" : ""}>${esc(form.background)}</textarea></div>
        <div class="field"><label>参与角色</label>
          <div class="stack" style="gap:6px;">${(state.narrCast || []).map(character => {
            const boundActor = !!character.persona_id;
            const checked = boundActor && (form.participantIds || []).includes(character.id);
            return `<label class="narr-check"><input type="checkbox" class="narr-rehearse-actor" value="${esc(character.id)}" ${checked ? "checked" : ""} ${pipe.rehearsal.disabled || !boundActor ? "disabled" : ""} /> ${esc(character.name)} ${boundActor ? "<span class='tag ok'>已绑定</span>" : "<span class='tag'>普通剧情角色 · 不参与 Persona 排练</span>"}</label>`;
          }).join("") || `<p class="meta">请先添加并绑定 Persona。</p>`}</div>
          <p class="meta">Persona 排练仅允许已绑定 Persona 的角色。普通剧情角色由 Screenwriter 处理。</p>
        </div>
        <div id="narr-simulation-results" class="stack" style="gap:8px;margin-top:8px;"></div>
      </div>
      <div class="card narr-step ${pipe.draft.disabled ? "is-disabled" : ""}">
        <div class="narr-step-head"><div><strong>4 剧本</strong>${statusBadge(pipe.draft)}</div>
          <button class="btn btn-sm btn-primary" id="btn-narr-draft" type="button" ${pipe.draft.disabled ? "disabled" : ""}>生成剧本</button></div>
        ${pipe.draft.reason && pipe.draft.disabled ? `<p class="narr-step-reason">${esc(pipe.draft.reason)}</p>` : ""}
        <div id="narr-draft-view">${version ? `<pre style="white-space:pre-wrap;font-size:13px;max-height:280px;overflow:auto;">${esc(version.screenplay || "")}</pre>` : `<p class="meta">尚未生成剧本。</p>`}</div>
      </div>
      <div class="card narr-step ${pipe.audit.disabled ? "is-disabled" : ""}">
        <div class="narr-step-head"><div><strong>5 连续性审核</strong>${statusBadge(pipe.audit)}</div>
          <button class="btn btn-sm btn-secondary" id="btn-narr-audit" type="button" ${pipe.audit.disabled ? "disabled" : ""}>审核</button></div>
        ${pipe.audit.reason && pipe.audit.disabled ? `<p class="narr-step-reason">${esc(pipe.audit.reason)}</p>` : ""}
        <div id="narr-audit-view">${audit ? `<p>${audit.passed ? "通过" : "未通过"} · ${t("auditBlocking")} ${audit.blocking_count || 0} · ${t("auditWarning")} ${audit.warning_count || 0}</p>
          <ul>${((audit.findings || []).map(item => `<li><strong>${esc(item.severity)}</strong> ${esc(item.message)}</li>`).join("")) || ""}</ul>` : `<p class="meta">尚未审核。</p>`}</div>
      </div>
      <div class="card narr-step ${pipe.commit.disabled ? "is-disabled" : ""}">
        <div class="narr-step-head"><div><strong>6 提交正史</strong>${statusBadge(pipe.commit)}</div>
          <button class="btn btn-sm btn-primary" id="btn-narr-commit" type="button" ${pipe.commit.disabled ? "disabled" : ""}>提交正史</button></div>
        ${pipe.commit.reason && pipe.commit.disabled ? `<p class="narr-step-reason">${esc(pipe.commit.reason)}</p>` : ""}
      </div>
      <div class="card narr-step">
        <div class="narr-step-head"><div><strong>7 制作</strong>${statusBadge(pipe.production)}</div>
          <button class="btn btn-sm btn-secondary" id="btn-narr-production" type="button">生成制作包</button></div>
        ${pipe.production.warning ? `<p class="narr-step-reason">${esc(pipe.production.reason)}</p>` : ""}
        <div id="narr-ep-prod-mini">${pkg ? `<p class="meta">${t("narrShotsPrompts").replace("{s}", (pkg.shot_list || []).length).replace("{v}", (pkg.video_generation_prompts || []).length)}</p>` : `<p class="meta">尚未生成。</p>`}</div>
      </div>`;
    const writerBtnHost = $("#narr-writer-config");
    if (writerBtnHost) {
      const ready = writerRoomParticipants().some(item => item.role === "head_writer");
      writerBtnHost.innerHTML = ready
        ? `<button class="btn btn-sm btn-secondary" id="btn-narr-writer-room" type="button">运行 Writer Room</button>`
        : `<p class="meta">尚未配置 Writer Personas，无法运行。</p>`;
    }
    renderNarrativeDirections();
    if (state.narrForecast) renderNarrativeForecast(state.narrForecast);
    const lastScene = ((ep && ep.scenes) || []).slice().reverse().find(scene => (scene.dialogue || []).length);
    if (lastScene) renderRehearsalResults(lastScene);
    onClick("#btn-narr-forecast-generate", generateNarrativeDirections);
    onClick("#btn-narr-forecast-add", () => {
      state.narrDirections = state.narrDirections || [];
      if (state.narrDirections.length >= 5) { toast("最多 5 条候选路线"); return; }
      state.narrDirections.push({ label: String.fromCharCode(65 + state.narrDirections.length), description: "" });
      renderNarrativeDirections();
    });
    onClick("#btn-narr-forecast-run", runNarrativeForecast);
    onClick("#btn-narr-simulate", runNarrativeSimulation);
    onClick("#btn-narr-draft", () => narrEpisodeAction("draft"));
    onClick("#btn-narr-audit", () => narrEpisodeAction("audit"));
    onClick("#btn-narr-commit", () => narrEpisodeAction("commit"));
    onClick("#btn-narr-production", () => narrEpisodeAction("production"));
    onClick("#btn-narr-writer-room", runNarrativeWriterRoom);
    $$(".narr-rehearse-actor").forEach(box => box.addEventListener("change", captureRehearsalForm));
  }

  async function loadNarrativeProjects() {
    const res = await api("/api/narratives");
    state.narrProjects = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
    renderNarrativeProjects();
  }

  function selectedNarrativeIds() {
    const known = new Set((state.narrProjects || []).map(item => item.id));
    state.narrSelectedIds = (state.narrSelectedIds || []).filter(id => known.has(id));
    return state.narrSelectedIds;
  }

  function syncNarrativeListSelection() {
    const ids = selectedNarrativeIds();
    const total = (state.narrProjects || []).length;
    const toolbar = $("#narr-list-toolbar");
    const selectAll = $("#narr-select-all");
    const countEl = $("#narr-selected-count");
    const deleteBtn = $("#btn-narr-delete-selected");
    if (toolbar) toolbar.hidden = total === 0;
    if (selectAll) {
      selectAll.checked = total > 0 && ids.length === total;
      selectAll.indeterminate = ids.length > 0 && ids.length < total;
    }
    if (countEl) countEl.textContent = ids.length ? `（${ids.length}）` : "";
    if (deleteBtn) deleteBtn.disabled = ids.length === 0;
    $$("#narr-project-list .narr-project-select").forEach(box => {
      box.checked = ids.includes(box.value);
    });
    $$("#narr-project-list .narr-project-card").forEach(card => {
      card.classList.toggle("is-selected", ids.includes(card.dataset.projectId));
    });
  }

  function toggleNarrativeProjectSelection(projectId, selected) {
    const ids = selectedNarrativeIds();
    const has = ids.includes(projectId);
    if (selected && !has) ids.push(projectId);
    if (!selected && has) state.narrSelectedIds = ids.filter(id => id !== projectId);
    syncNarrativeListSelection();
  }

  function renderNarrativeProjects() {
    const host = $("#narr-project-list");
    if (!host) return;
    if (!state.narrProjects.length) {
      state.narrSelectedIds = [];
      host.innerHTML = `<p class="meta">${t("narrNoProjects")}</p>`;
      syncNarrativeListSelection();
      return;
    }
    const selected = new Set(selectedNarrativeIds());
    host.innerHTML = state.narrProjects.map(p => `
      <div class="card narr-project-card interactive ${selected.has(p.id) ? "is-selected" : ""}" data-project-id="${esc(p.id)}">
        <div class="row-between" style="align-items:flex-start;gap:12px;">
          <label class="narr-check narr-project-check" onclick="event.stopPropagation();">
            <input type="checkbox" class="narr-project-select" value="${esc(p.id)}" ${selected.has(p.id) ? "checked" : ""} />
          </label>
          <div class="narr-project-body" style="min-width:0;flex:1;">
            <h3 style="margin:0 0 4px;">${esc(p.title)}</h3>
            <p class="meta" style="margin:0;">${esc(p.logline || "")}</p>
            <p class="meta" style="margin-top:6px;">${esc(narrFormatLabel(p.format))} · ${p.planned_episode_count || 0}集 · ${p.episode_duration_seconds_min || 90}–${p.episode_duration_seconds_max || 120}秒</p>
          </div>
          <span class="tag ${p.status === "active" ? "solid" : ""}">${esc(p.status)}</span>
        </div>
      </div>`).join("");
    $$("#narr-project-list .narr-project-card").forEach(card => {
      card.addEventListener("click", (event) => {
        if (event.target.closest(".narr-project-check")) return;
        openNarrativeProject(card.dataset.projectId);
      });
    });
    $$("#narr-project-list .narr-project-select").forEach(box => {
      box.addEventListener("click", (event) => event.stopPropagation());
      box.addEventListener("change", () => toggleNarrativeProjectSelection(box.value, box.checked));
    });
    syncNarrativeListSelection();
  }

  function deleteSelectedNarrativeProjects() {
    const ids = selectedNarrativeIds();
    if (!ids.length) return;
    const titles = (state.narrProjects || []).filter(item => ids.includes(item.id)).map(item => item.title);
    const preview = titles.slice(0, 8).map(title => `「${title}」`).join("、");
    const extra = titles.length > 8 ? "…" : "";
    const body = ids.length === 1
      ? `确定删除${preview}？此操作不可恢复。`
      : `确定删除已选的 ${ids.length} 个作品（${preview}${extra}）？此操作不可恢复。`;
    confirmDlg(body, t("narrDeleteSelected") || "删除所选", async () => {
      let ok = 0;
      let fail = 0;
      for (const id of ids) {
        const res = await api(`/api/narratives/${id}`, { method: "DELETE" });
        if (res && res.ok) ok += 1;
        else fail += 1;
      }
      state.narrSelectedIds = [];
      await loadNarrativeProjects();
      toast(fail ? `已删除 ${ok} 个，失败 ${fail} 个` : `已删除 ${ok} 个作品`);
    }, true);
  }

  async function openNarrativeProject(projectId) {
    const res = await api(`/api/narratives/${projectId}`);
    if (!res || !res.ok) { toast((res && res.error) || "加载作品失败"); return; }
    state.narrProject = res.data;
    state.narrDirections = [];
    state.narrBibleEditing = false;
    state.narrRehearsal = { location: "", background: "", goal: "", participantIds: null };
    // Shooting sessions are project-scoped; drop any cached session id.
    stopShootingPolling();
    state.shooting.sessionId = null;
    state.shooting.seen = new Set();
    showNarrativeSub("detail");
    fillSettingsFromProject();
    showNarrativeTab("settings");
    renderNarrativeHeader();
    await refreshNarrativeAll();
  }

  async function loadNarrativeBible() {
    if (!state.narrProject) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/bible`);
    state.narrBible = (res && res.ok) ? res.data : null;
    renderNarrativeBible();
    renderBibleSyncBanner();
  }

  function bibleSection(title, body) {
    return `<section class="narr-bible-section"><h4>${esc(title)}</h4>${body}</section>`;
  }

  function renderNarrativeBible() {
    const host = $("#narr-bible-view");
    if (!host) return;
    if (!state.narrBible) {
      host.innerHTML = `<p class="meta">尚无故事圣经 — 先填写作品设定，再点击「AI 重新生成」。</p>`;
      return;
    }
    const b = state.narrBible;
    if (state.narrBibleEditing) {
      host.innerHTML = `
        ${bibleSection("核心故事", `
          <div class="field"><label>${t("biblePremise")}</label><textarea class="textarea" id="bible-premise" rows="3">${esc(b.premise || "")}</textarea></div>
          <div class="field"><label>${t("bibleQuestion")}</label><textarea class="textarea" id="bible-question" rows="2">${esc(b.core_question || "")}</textarea></div>
          <div class="field"><label>${t("bibleTheme")}</label><textarea class="textarea" id="bible-theme" rows="2">${esc(b.theme || "")}</textarea></div>`)}
        ${bibleSection("世界规则", `<textarea class="textarea" id="bible-rules" rows="4">${esc((b.world_rules || []).join("\n"))}</textarea>`)}
        ${bibleSection("主要角色", `<textarea class="textarea" id="bible-characters" rows="6">${esc((b.characters || []).map(c => [c.id, c.name, c.role, c.description].filter(Boolean).join(" | ")).join("\n"))}</textarea><p class="meta">每行：id | 名称 | 角色 | 简介</p>`)}
        ${bibleSection("关键地点", `<textarea class="textarea" id="bible-locations" rows="4">${esc((b.locations || []).map(c => [c.id, c.name, c.description].filter(Boolean).join(" | ")).join("\n"))}</textarea>`)}
        ${bibleSection("全剧时间线", `<textarea class="textarea" id="bible-timeline" rows="4">${esc((b.master_timeline || []).map(c => `${c.episode} | ${c.summary || ""}`).join("\n"))}</textarea>`)}
        <details class="narr-author-only" open>
          <summary><span class="narr-author-badge">AUTHOR ONLY · 仅作者可见</span></summary>
          <p class="meta">不会作为角色知识注入 Persona</p>
          <textarea class="textarea" id="bible-truth" rows="4">${esc((b.final_truth || []).join("\n"))}</textarea>
        </details>`;
      return;
    }
    host.innerHTML = `
      ${bibleSection("核心故事", `<p><strong>Premise</strong>：${esc(b.premise || "—")}</p><p><strong>Core Question</strong>：${esc(b.core_question || "—")}</p><p><strong>Theme</strong>：${esc(b.theme || "—")}</p>`)}
      ${bibleSection("世界规则", `<ul>${(b.world_rules || []).map(item => `<li>${esc(item)}</li>`).join("") || "<li class='meta'>—</li>"}</ul>`)}
      ${bibleSection("主要角色", (b.characters || []).length ? (b.characters || []).map(c => `<div class="card" style="padding:10px;margin-bottom:8px;"><strong>${esc(c.name)}</strong> <span class="meta">${esc(c.role || "")}</span><p class="meta">${esc(c.description || "")}</p></div>`).join("") : `<p class="meta">—</p>`)}
      ${bibleSection("关键地点", (b.locations || []).length ? (b.locations || []).map(c => `<p><strong>${esc(c.name)}</strong> ${esc(c.description || "")}</p>`).join("") : `<p class="meta">—</p>`)}
      ${bibleSection("全剧时间线", (b.master_timeline || []).length ? `<ol>${(b.master_timeline || []).map(c => `<li>EP${c.episode} ${esc(c.summary || "")}</li>`).join("")}</ol>` : `<p class="meta">—</p>`)}
      <details class="narr-author-only">
        <summary><span class="narr-author-badge">AUTHOR ONLY · 仅作者可见</span> 作者秘密 / Final Truth</summary>
        <p class="meta">不会作为角色知识注入 Persona</p>
        <ul>${(b.final_truth || []).map(item => `<li>${esc(item)}</li>`).join("") || "<li class='meta'>—</li>"}</ul>
      </details>
      <p class="meta narr-tech">Version ${b.version}</p>`;
  }

  function collectBibleEdits() {
    const parseRows = (text, keys) => linesList(text).map(line => {
      const parts = line.split("|").map(item => item.trim());
      const row = {};
      keys.forEach((key, index) => { row[key] = parts[index] || ""; });
      return row;
    }).filter(row => Object.values(row).some(Boolean));
    return {
      premise: (($("#bible-premise") || {}).value || "").trim(),
      core_question: (($("#bible-question") || {}).value || "").trim(),
      theme: (($("#bible-theme") || {}).value || "").trim(),
      world_rules: linesList(($("#bible-rules") || {}).value),
      characters: parseRows(($("#bible-characters") || {}).value, ["id", "name", "role", "description"]).map(row => ({
        id: row.id || row.name, name: row.name || row.id, role: row.role, description: row.description,
      })),
      locations: parseRows(($("#bible-locations") || {}).value, ["id", "name", "description"]).map(row => ({
        id: row.id || row.name, name: row.name || row.id, description: row.description,
      })),
      master_timeline: parseRows(($("#bible-timeline") || {}).value, ["episode", "summary"]).map(row => ({
        episode: parseInt(row.episode, 10) || 0, summary: row.summary,
      })),
      final_truth: linesList(($("#bible-truth") || {}).value),
    };
  }

  async function loadNarrativeCast() {
    if (!state.narrProject) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/characters`);
    state.narrCast = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
    renderNarrativeCast();
  }

  function personaOptions(selectedId) {
    const personas = state.personas || [];
    return [`<option value="">未绑定</option>`].concat(personas.map(persona =>
      `<option value="${esc(persona.id)}" ${persona.id === selectedId ? "selected" : ""}>${esc(persona.display_name || persona.id)}</option>`
    )).join("");
  }

  function suggestedPersonaForName(name) {
    const needle = String(name || "").trim().toLowerCase();
    if (!needle) return null;
    return (state.personas || []).find(item => String(item.display_name || "").trim().toLowerCase() === needle) || null;
  }

  function renderNarrativeCast() {
    const host = $("#narr-cast-view");
    if (!host) return;
    if (!state.narrCast.length) {
      host.innerHTML = `<p class="meta">暂无角色。可从人格库添加，或从故事圣经同步。</p>`;
      return;
    }
    host.innerHTML = state.narrCast.map(character => {
      const persona = (state.personas || []).find(item => item.id === character.persona_id);
      const bound = !!character.persona_id;
      const suggestion = !bound ? suggestedPersonaForName(character.name) : null;
      return `<div class="card narr-cast-card">
        <strong>${esc(character.name)}</strong>
        <p class="meta">${esc(character.role || (bound ? "剧情角色" : "功能角色"))}</p>
        <p style="margin-top:6px;">${esc(character.description || "")}</p>
        <div style="margin-top:10px;">
          ${bound ? `<p>Persona<br>✓ ${esc((persona && persona.display_name) || "已绑定")} · 完整人格</p>
            <div class="row" style="gap:6px;margin-top:6px;flex-wrap:wrap;">
              <button class="btn btn-sm btn-secondary narr-persona-view" data-persona="${esc(character.persona_id)}" type="button">查看人格</button>
              <button class="btn btn-sm btn-ghost narr-persona-rebind" data-id="${esc(character.id)}" type="button">更换</button>
              <button class="btn btn-sm btn-ghost narr-persona-unbind" data-id="${esc(character.id)}" type="button">解除绑定</button>
            </div>` : `<p>Persona<br>○ 未绑定<br><span class="meta">普通剧情角色</span></p>
            ${suggestion ? `<p class="meta" style="margin-top:8px;">发现可能的人格：${esc(suggestion.display_name)} <button class="btn btn-sm btn-secondary narr-persona-suggest" data-id="${esc(character.id)}" data-persona="${esc(suggestion.id)}" type="button">绑定</button></p>` : ""}
            <select class="input narr-persona-bind" data-id="${esc(character.id)}" style="margin-top:8px;">${personaOptions("")}</select>`}
        </div>
      </div>`;
    }).join("");
    $$(".narr-persona-bind").forEach(sel => sel.addEventListener("change", () => bindNarrativePersona(sel.dataset.id, sel.value)));
    $$(".narr-persona-rebind").forEach(btn => btn.addEventListener("click", () => {
      const card = btn.closest(".narr-cast-card");
      if (!card) return;
      card.insertAdjacentHTML("beforeend", `<select class="input narr-persona-bind" data-id="${esc(btn.dataset.id)}" style="margin-top:8px;">${personaOptions("")}</select>`);
      const sel = card.querySelector(".narr-persona-bind:last-of-type");
      sel.addEventListener("change", () => bindNarrativePersona(sel.dataset.id, sel.value));
    }));
    $$(".narr-persona-unbind").forEach(btn => btn.addEventListener("click", () => unbindNarrativePersona(btn.dataset.id)));
    $$(".narr-persona-suggest").forEach(btn => btn.addEventListener("click", () => bindNarrativePersona(btn.dataset.id, btn.dataset.persona)));
    $$(".narr-persona-view").forEach(btn => btn.addEventListener("click", () => {
      if (typeof showProfileDetail === "function") showProfileDetail(btn.dataset.persona);
      else showView("personas");
    }));
  }

  async function bindNarrativePersona(characterId, personaId) {
    if (!state.narrProject || !personaId) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/characters/bind`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character_id: characterId, persona_id: personaId }),
    });
    if (res && res.ok) {
      toast("已绑定 Persona");
      await loadNarrativeCast();
      renderNarrativeHeader();
    } else toast((res && res.error) || "绑定失败");
  }

  async function unbindNarrativePersona(characterId) {
    if (!state.narrProject || !characterId) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/characters/bind`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character_id: characterId, unbind: true }),
    });
    if (res && res.ok) {
      toast("已解除绑定");
      await loadNarrativeCast();
      renderNarrativeHeader();
    } else toast((res && res.error) || "解除绑定失败");
  }

  function narrativeCharMode() {
    return (($("input[name='narr-char-mode']:checked") || {}).value || "library");
  }

  function syncNarrativeCharMode() {
    const mode = narrativeCharMode();
    const library = $("#narr-char-library-panel");
    const extra = $("#narr-char-extra-panel");
    const submit = $("#narr-char-submit");
    if (library) library.hidden = mode !== "library";
    if (extra) extra.hidden = mode !== "extra";
    if (submit) submit.textContent = mode === "library" ? "添加并绑定" : "添加普通角色";
    const name = $("#narr-char-name");
    if (name) name.required = mode === "extra";
  }

  function selectedNarrativeLibraryPersona() {
    const id = (($("input[name='narr-char-persona']:checked") || {}).value || "").trim();
    return (state.personas || []).find(item => item.id === id) || null;
  }

  function renderNarrativePersonaPicker(query) {
    const host = $("#narr-char-persona-list");
    if (!host) return;
    const q = String(query || "").trim().toLowerCase();
    const personas = (state.personas || []).filter(persona => {
      const blob = `${persona.display_name || ""} ${persona.summary || ""} ${persona.id || ""}`.toLowerCase();
      return !q || blob.includes(q);
    });
    if (!personas.length) {
      host.innerHTML = `<p class="meta">没有需要的人格？</p>
        <button class="btn btn-sm btn-secondary" id="btn-narr-char-create-persona" type="button">创建完整 Persona</button>`;
      onClick("#btn-narr-char-create-persona", startNarrativePersonaCreation);
      renderNarrativePersonaPreview(null);
      return;
    }
    const selected = (($("input[name='narr-char-persona']:checked") || {}).value || "");
    host.innerHTML = personas.map(persona => `
      <label class="narr-persona-option">
        <input type="radio" name="narr-char-persona" value="${esc(persona.id)}" ${persona.id === selected ? "checked" : ""} />
        <span><strong>${esc(persona.display_name || persona.id)}</strong>
          <span class="meta">完整 Persona</span></span>
      </label>`).join("");
    $$("input[name='narr-char-persona']").forEach(radio => radio.addEventListener("change", () => {
      const persona = selectedNarrativeLibraryPersona();
      renderNarrativePersonaPreview(persona);
      if (persona && !(($("#narr-char-role") || {}).value || "").trim()) {
        /* name is bound from display_name on submit; role stays author-authored */
      }
    }));
    if (!selected && personas[0]) {
      const first = host.querySelector("input[name='narr-char-persona']");
      if (first) { first.checked = true; renderNarrativePersonaPreview(personas[0]); }
    } else renderNarrativePersonaPreview(selectedNarrativeLibraryPersona());
  }

  function renderNarrativePersonaPreview(persona) {
    const host = $("#narr-char-persona-preview");
    if (!host) return;
    if (!persona) { host.hidden = true; host.innerHTML = ""; return; }
    const profile = (state.profiles || []).find(item => item.id === persona.id || item.persona_id === persona.id);
    host.hidden = false;
    host.innerHTML = `
      <p><strong>${esc(persona.display_name || persona.id)}</strong></p>
      <p>${esc(persona.summary || (profile && profile.summary) || "暂无简介")}</p>
      <p class="meta">类型：${esc(persona.persona_type || (profile && profile.profile_type) || "—")}</p>
      <p class="meta">编译状态：${esc(persona.compile_state || (profile && profile.status) || "—")}</p>
      <details class="narr-tech"><summary>技术详情</summary><p class="meta">${esc(persona.id)}</p></details>`;
  }

  async function openNarrativeAddCharacterModal() {
    const dlg = $("#dlg-narr-char");
    const err = $("#narr-char-error");
    if (err) { err.hidden = true; err.textContent = ""; }
    setField("narr-char-name", "");
    setField("narr-char-role", "");
    setField("narr-char-desc", "");
    setField("narr-char-extra-role", "");
    setField("narr-char-extra-desc", "");
    setField("narr-char-search", "");
    const libraryRadio = $("input[name='narr-char-mode'][value='library']");
    if (libraryRadio) libraryRadio.checked = true;
    syncNarrativeCharMode();
    if (dlg && dlg.showModal) dlg.showModal();
    await loadPersonas();
    renderNarrativePersonaPicker(($("#narr-char-search") || {}).value);
  }

  function startNarrativePersonaCreation() {
    if (state.narrProject) state.narrReturnToCast = state.narrProject.id;
    const dlg = $("#dlg-narr-char");
    if (dlg && dlg.open) dlg.close();
    showView("personas");
    openPersonaCreation();
  }

  async function maybeReturnToNarrativeCast(job) {
    if (!state.narrReturnToCast) return;
    if (!job || !["completed", "completed_with_gaps"].includes(job.status)) return;
    const projectId = state.narrReturnToCast;
    state.narrReturnToCast = null;
    await loadPersonas();
    showView("narrative");
    if (!state.narrProject || state.narrProject.id !== projectId) {
      await openNarrativeProject(projectId);
    }
    showNarrativeTab("cast");
    await openNarrativeAddCharacterModal();
    toast("新人格已可添加到角色");
  }

  async function submitNarrativeCharacter(event) {
    event.preventDefault();
    if (!state.narrProject) return;
    const err = $("#narr-char-error");
    const mode = narrativeCharMode();
    let payload;
    if (mode === "library") {
      const persona = selectedNarrativeLibraryPersona();
      if (!persona) {
        if (err) { err.hidden = false; err.textContent = "请选择要绑定的人格。"; }
        return;
      }
      payload = {
        name: persona.display_name || persona.id,
        role: (($("#narr-char-role") || {}).value || "").trim(),
        description: (($("#narr-char-desc") || {}).value || "").trim(),
        persona_id: persona.id,
      };
    } else {
      const name = (($("#narr-char-name") || {}).value || "").trim();
      if (!name) {
        if (err) { err.hidden = false; err.textContent = "请填写角色名称。"; }
        return;
      }
      payload = {
        name,
        role: (($("#narr-char-extra-role") || {}).value || "").trim(),
        description: (($("#narr-char-extra-desc") || {}).value || "").trim(),
      };
    }
    const res = await api(`/api/narratives/${state.narrProject.id}/characters`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res && res.ok) {
      const dlg = $("#dlg-narr-char");
      if (dlg) dlg.close();
      toast(mode === "library" ? "已添加并绑定 Persona" : "已添加普通剧情角色");
      await loadNarrativeCast();
      renderNarrativeHeader();
    } else if (err) {
      err.hidden = false;
      err.textContent = (res && res.error) || "添加失败";
    } else toast((res && res.error) || "添加失败");
  }

  async function loadNarrativeEpisodes() {
    if (!state.narrProject) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/episodes`);
    state.narrEpisodes = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
    if (state.narrEpisodes.length && !state.narrEpisodes.some(item => item.episode_number === state.narrEpNumber)) {
      state.narrEpNumber = state.narrEpisodes[0].episode_number;
    }
    renderNarrativeOutline();
    if (state.narrTab === "episode") renderEpisodeWorkbench();
  }

  function outlineAuditFromEpisodes(episodes) {
    for (const episode of episodes || []) {
      const audit = ((episode.runtime_trace || {}).global_audit) || {};
      const unresolved = audit.unresolved_after_repair || [];
      if (unresolved.length || (audit.warnings || []).length) return audit;
    }
    return null;
  }

  function renderNarrativeOutline() {
    const host = $("#narr-outline-view");
    if (!host) return;
    if (!state.narrEpisodes.length) {
      host.innerHTML = `<p class="meta">暂无大纲 — 点击「AI 生成 / 重新生成」。</p>`;
      return;
    }
    const audit = outlineAuditFromEpisodes(state.narrEpisodes);
    const unresolved = (audit && audit.unresolved_after_repair) || [];
    const banner = unresolved.length
      ? `<div class="narr-step-reason">连续性警告（大纲已保存，可继续改稿）：${esc(unresolved.map(item => redactNarrText(item)).join("；"))}</div>`
      : "";
    host.innerHTML = banner + state.narrEpisodes.map(ep => `
      <button type="button" class="card narr-outline-card" data-ep="${ep.episode_number}">
        <div class="row-between"><strong>EP${String(ep.episode_number).padStart(2, "0")}</strong><span class="tag">${esc(ep.status || "planned")}</span></div>
        <p style="margin-top:6px;">${esc(ep.title || "")}</p>
        <p class="meta">目标：${esc(ep.narrative_goal || "—")}</p>
        <p class="meta">钩子：${esc(ep.hook || "—")}</p>
        <p class="meta">悬念：${esc(ep.cliffhanger || "—")}</p>
      </button>`).join("");
    $$("#narr-outline-view [data-ep]").forEach(btn => btn.addEventListener("click", () => {
      state.narrEpNumber = parseInt(btn.dataset.ep, 10);
      showNarrativeTab("episode");
    }));
  }

  async function loadNarrativeForecasts() {
    if (!state.narrProject) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/forecasts`);
    state.narrForecasts = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
    const forecast = state.narrForecasts.find(item => item.episode_number === state.narrEpNumber);
    state.narrForecast = forecast || null;
    if (forecast) renderNarrativeForecast(forecast);
  }

  async function loadNarrativeKnowledge() {
    if (!state.narrProject) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/knowledge`);
    state.narrKnowledge = (res && res.ok) ? res.data : null;
    renderNarrativeKnowledge();
  }

  function renderNarrativeKnowledge() {
    const host = $("#narr-knowledge-view");
    if (!host) return;
    const matrix = state.narrKnowledge;
    if (!matrix || !matrix.facts || !matrix.facts.length) { host.innerHTML = `<p class="meta">尚无 Story Truth 事实。</p>`; return; }
    const chars = matrix.characters || [];
    const head = `<tr><th>事实</th>${chars.map(c => `<th>${esc(c.name)}</th>`).join("")}<th>观众</th></tr>`;
    const rows = matrix.facts.map(fact => {
      const kmap = (matrix.character_knowledge || {})[fact.id] || {};
      const aud = (matrix.audience_knowledge || {})[fact.id] || "hidden";
      return `<tr><td>${esc(fact.text)}</td>${chars.map(c => `<td>${esc(kmap[c.id] || "unknown")}</td>`).join("")}<td><strong>${esc(aud)}</strong></td></tr>`;
    }).join("");
    host.innerHTML = `<div style="overflow-x:auto;"><table>${head}${rows}</table></div>`;
  }

  async function loadNarrativeClues() {
    if (!state.narrProject) return;
    const res = await api(`/api/narratives/${state.narrProject.id}/clues`);
    state.narrClues = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
    const host = $("#narr-clues-view");
    if (!host) return;
    host.innerHTML = state.narrClues.length ? state.narrClues.map(clue => `
      <div class="card" style="padding:10px 12px;">
        <div class="row-between"><div><strong>${esc(clue.title)}</strong>
          <span class="meta">${esc(clue.type)} · 埋设 EP${clue.introduced_episode ?? "—"} · 回收 EP${clue.planned_reveal_episode ?? "—"}</span></div>
          <span class="tag">${esc(clue.status)}</span></div>
        <p class="meta narr-tech">${esc(clue.id || "")}</p>
      </div>`).join("") : `<p class="meta">暂无伏笔。</p>`;
  }

  async function loadNarrativeContinuityExtras() {
    if (!state.narrProject) return;
    const [threads, arcs, canon] = await Promise.all([
      api(`/api/narratives/${state.narrProject.id}/threads`),
      api(`/api/narratives/${state.narrProject.id}/arcs`),
      api(`/api/narratives/${state.narrProject.id}/canon`),
    ]);
    state.narrThreads = (threads && threads.ok && Array.isArray(threads.data)) ? threads.data : [];
    state.narrArcs = (arcs && arcs.ok && Array.isArray(arcs.data)) ? arcs.data : [];
    state.narrCanon = (canon && canon.ok && Array.isArray(canon.data)) ? canon.data : [];
    const threadHost = $("#narr-threads-view");
    if (threadHost) {
      const threadCards = state.narrThreads.map(thread => `<div class="card" style="padding:10px;"><strong>${esc(thread.title)}</strong> <span class="tag">${esc(thread.status)}</span><p class="meta">${esc(thread.description || "")}</p></div>`);
      const arcCards = state.narrArcs.map(arc => {
        const character = (state.narrCast || []).find(item => item.id === arc.character_id);
        return `<div class="card" style="padding:10px;"><strong>${esc((character && character.name) || "角色弧")}</strong>
          <p class="meta">${esc(arc.start_state || "—")} → ${esc(arc.target_state || "—")}</p></div>`;
      });
      threadHost.innerHTML = (threadCards.concat(arcCards).join("")) || `<p class="meta">暂无角色弧或剧情线。</p>`;
    }
    const canonHost = $("#narr-canon-view");
    if (canonHost) {
      const committed = (state.narrEpisodes || []).filter(item => item.status === "canon").length;
      canonHost.innerHTML = `<p>已提交正史 ${committed} / ${state.narrProject.planned_episode_count || 0} 集</p>
        ${(state.narrCanon || []).length ? `<ul>${state.narrCanon.slice(0, 12).map(entry => `<li>EP${entry.episode_number ?? "—"} ${esc(entry.text)}</li>`).join("")}</ul>` : `<p class="meta">尚未提交 Canon。</p>`}`;
    }
  }

  function productionBlock(title, content) {
    return `<details class="narr-prod-block"><summary>${esc(title)}</summary>${content}</details>`;
  }

  async function loadNarrativeProduction() {
    if (!state.narrProject) return;
    const [res, assetsRes] = await Promise.all([
      api(`/api/narratives/${state.narrProject.id}/production`),
      api(`/api/narratives/${state.narrProject.id}/production-assets`),
    ]);
    state.narrProduction = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
    state.narrProductionAssets = (assetsRes && assetsRes.ok && Array.isArray(assetsRes.data)) ? assetsRes.data : [];
    // Third layer data: prefetch the latest guide of every production master.
    await Promise.all(state.narrProduction.map(pkg => loadNarrativeGuide(pkg.id)));
    await renderNarrativeProductionLayers();
  }

  // ─── Production page: master layer + video generation plan (Task D) ────
  const NARR_VP_STAGE_LABELS = {
    queued: "ppStageQueued", created: "ppStageQueued",
    planning_clips: "ppStagePlanningClips", planning_assets: "ppStagePlanningAssets",
    compiling_prompts: "ppStageCompilingPrompts", validating: "ppStageValidating",
  };
  const NARR_VP_MODE_I18N = {
    auto: "modeAuto", text_to_video: "modeTextToVideo", image_to_video: "modeImageToVideo",
    first_frame: "modeFirstFrame", first_last_frame: "modeFirstLastFrame",
    reference_conditioned: "modeReferenceConditioned",
  };
  const NARR_VP_ASSET_TYPE_I18N = {
    character_reference: "assetTypeCharacter", location_reference: "assetTypeLocation",
    prop_reference: "assetTypeProp", start_frame: "assetTypeStart",
    end_frame: "assetTypeEnd", style_reference: "assetTypeStyle", other: "assetTypeOther",
  };
  const NARR_VP_STATUS = {
    clip_planned: { key: "prodStatusClipPlanned", cls: "" },
    compiling: { key: "prodStatusCompiling", cls: "warn" },
    ready: { key: "prodStatusReady", cls: "ok" },
    failed: { key: "prodStatusFailed", cls: "danger" },
  };
  // Full production guide (third layer): job stages mirror the backend
  // GUIDE_STAGE_LABELS so the UI never invents progress wording.
  const NARR_GUIDE_STAGE_LABELS = {
    queued: "prodGuideCreating", created: "prodGuideCreating",
    loading_source: "prodGuideStageLoadingSource",
    analyzing_assets: "prodGuideStageAnalyzingAssets",
    compiling_asset_prompts: "prodGuideStageCompilingAssetPrompts",
    compiling_clip_prompts: "prodGuideStageCompilingClipPrompts",
    rendering_guide: "prodGuideStageRenderingGuide",
  };

  function narrVpModeLabel(mode) {
    return NARR_VP_MODE_I18N[mode] ? t(NARR_VP_MODE_I18N[mode]) : String(mode || "auto");
  }

  function narrVpAssetTypeLabel(assetType) {
    return NARR_VP_ASSET_TYPE_I18N[assetType]
      ? t(NARR_VP_ASSET_TYPE_I18N[assetType])
      : String(assetType || "other");
  }

  function narrVpShotRanges(numbers) {
    const sorted = [...new Set((numbers || []).map(n => Number(n)).filter(n => !Number.isNaN(n)))]
      .sort((a, b) => a - b);
    const ranges = [];
    let start = null;
    let prev = null;
    sorted.forEach(n => {
      if (start === null) { start = n; prev = n; return; }
      if (n === prev + 1) { prev = n; return; }
      ranges.push(start === prev ? `${start}` : `${start}–${prev}`);
      start = n; prev = n;
    });
    if (start !== null) ranges.push(start === prev ? `${start}` : `${start}–${prev}`);
    return ranges.join(", ");
  }

  function narrVpAssetsById() {
    return new Map((state.narrProductionAssets || []).map(asset => [asset.id, asset]));
  }

  function narrVpClipRefNames(ids) {
    const list = ids || [];
    if (!list.length) return esc(t("clipRefsNone"));
    const byId = narrVpAssetsById();
    return list.map(id => {
      const asset = byId.get(id);
      if (!asset) return `○ ${esc(id)}（未登记）`;
      return `${(asset.source_uri || asset.local_path) ? "✓" : "○"} ${esc(asset.name)}`;
    }).join("、");
  }

  function narrVpProfileById(profileId) {
    return (state.narrVideoProfiles || []).find(item => item.id === profileId) || null;
  }

  function narrVpProfileOptionLabel(profile) {
    const status = profile.verification_status;
    return `${profile.display_name || profile.id}${status && status !== "verified" ? `（${status}）` : ""}`;
  }

  async function ensureNarrativeVideoProfiles() {
    if (state.narrVideoProfilesLoaded) return state.narrVideoProfiles;
    const res = await api("/api/narratives/video-model-profiles");
    state.narrVideoProfiles = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
    state.narrVideoProfilesLoaded = true;
    return state.narrVideoProfiles;
  }

  async function loadNarrativePromptPlans(prodPkgId) {
    const res = await api(`/api/narratives/${state.narrProject.id}/production/${prodPkgId}/prompt-packages`);
    state.narrPromptPlans[prodPkgId] = (res && res.ok && Array.isArray(res.data)) ? res.data : [];
  }

  async function loadNarrativeGuide(prodPkgId) {
    // Latest guide per production master; 404 (none yet) simply stores null.
    const res = await api(`/api/narratives/${state.narrProject.id}/production/${prodPkgId}/production-guide`);
    state.narrGuides[prodPkgId] = (res && res.ok && res.data) ? res.data : null;
    return state.narrGuides[prodPkgId];
  }

  async function copyNarrText(text) {
    const value = String(text == null ? "" : text);
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
        toast(t("toastCopied"));
        return;
      }
    } catch (_err) { /* fall through to the legacy path */ }
    try {
      const area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      toast(ok ? t("toastCopied") : t("toastCopyFailed"));
    } catch (_err) {
      toast(t("toastCopyFailed"));
    }
  }

  // Layer 1: production master (canon / preview badges, stale banner, legacy
  // model-agnostic video prompts collapsed away from the main reading path).
  function narrVpBibleLine(item) {
    if (!item || typeof item !== "object") return String(item || "—");
    return String(item.name || item.title || item.summary || item.description || JSON.stringify(item)).slice(0, 120);
  }

  function narrProductionPackageCard(pkg) {
    const canon = !pkg.is_preview;
    const badges = canon
      ? `<span class="tag ok">${t("prodCanonBadge")}</span>`
      : `<span class="tag danger">${t("prodPreviewBadge")}</span>`;
    return `
      <div class="card narr-prod-master-card">
        <div class="row-between" style="gap:8px;flex-wrap:wrap;">
          <strong>EP${String(pkg.episode_number).padStart(2, "0")}</strong>
          <span class="row" style="gap:6px;flex-wrap:wrap;"><span class="tag">${esc(pkg.format || "")}</span>${badges}</span>
        </div>
        ${canon ? "" : `<p class="meta">${t("prodPreviewHelp")}</p>`}
        ${pkg.stale ? `<div class="narr-prod-stale">${t("prodStaleBanner")}</div>` : ""}
        ${productionBlock("Screenplay", `<pre style="white-space:pre-wrap;font-size:13px;">${esc(pkg.screenplay || "—")}</pre>`)}
        ${productionBlock("Shot List", `<ol>${(pkg.shot_list || []).map(shot => `<li>${esc(shot.action || shot.visual_prompt || `Shot ${shot.shot_number}`)}</li>`).join("") || "<li class='meta'>—</li>"}</ol>`)}
        ${productionBlock("Image Prompt", `<ul>${(pkg.image_generation_prompts || []).map(item => `<li>${esc(item)}</li>`).join("") || "<li class='meta'>—</li>"}</ul>`)}
        ${(pkg.character_visual_bible || []).length || (pkg.location_visual_bible || []).length || (pkg.prop_visual_bible || []).length ? `<details class="narr-advanced-block"><summary>Visual Bibles</summary><ul>${[
          ...(pkg.character_visual_bible || []).map(item => `<li>Character · ${esc(narrVpBibleLine(item))}</li>`),
          ...(pkg.location_visual_bible || []).map(item => `<li>Location · ${esc(narrVpBibleLine(item))}</li>`),
          ...(pkg.prop_visual_bible || []).map(item => `<li>Prop · ${esc(narrVpBibleLine(item))}</li>`),
        ].join("")}</ul></details>` : ""}
        <details class="narr-advanced-block narr-vp-legacy">
          <summary>${t("prodVideoPromptAdvanced")}</summary>
          <p class="meta">${t("prodVideoPromptDisclaimer")}</p>
          <ul>${(pkg.video_generation_prompts || []).map(item => `<li>${esc(item)}</li>`).join("") || "<li class='meta'>—</li>"}</ul>
        </details>
        ${productionBlock("Dialogue Timing", `<ul>${(pkg.dialogue_track || []).map(item => `<li>${esc(item.speaker || "")} ${esc(item.text || item.dialogue || "")} ${esc(item.start_time || "")}</li>`).join("") || "<li class='meta'>—</li>"}</ul>`)}
        ${productionBlock("Subtitle", `<ul>${(pkg.subtitle_track || []).map(item => `<li>${esc(item.text || "")}</li>`).join("") || "<li class='meta'>—</li>"}</ul>`)}
        ${productionBlock("SFX", `<ul>${(pkg.sound_effect_plan || []).map(item => `<li>${esc(item.name || item.description || JSON.stringify(item))}</li>`).join("") || "<li class='meta'>—</li>"}</ul>`)}
        ${productionBlock("BGM", `<p>${esc(pkg.bgm_direction || "—")}</p>`)}
      </div>`;
  }

  function narrVpFormState(prodPkgId) {
    if (!state.narrPlanForms[prodPkgId]) {
      state.narrPlanForms[prodPkgId] = {
        open: false, profile_id: "", generation_strategy: "auto", aspect_ratio: "",
        quality_priority: "balanced", continuity_strategy: "auto", prompt_language: "auto",
      };
    }
    return state.narrPlanForms[prodPkgId];
  }

  function narrVpAudioHint(profile) {
    const value = profile && profile.capabilities ? profile.capabilities.supports_audio : undefined;
    if (value === true) return t("prodAudioHintYes");
    if (value === false) return t("prodAudioHintNo");
    return t("prodAudioHintUnknown");
  }

  function narrVpPlanFormHtml(pkg) {
    const st = narrVpFormState(pkg.id);
    const profile = narrVpProfileById(st.profile_id);
    const modes = (profile && Array.isArray(profile.modes)) ? profile.modes : [];
    const ratios = (profile && Array.isArray(profile.aspect_ratios)) ? profile.aspect_ratios : [];
    const continuity = [];
    if (profile && profile.capabilities) {
      if (profile.capabilities.supports_first_last_frame === true) continuity.push(["first_last_frame", t("prodContinuityFirstLast")]);
      if (profile.capabilities.supports_reference_images === true) continuity.push(["reference_conditioned", t("prodContinuityReference")]);
    }
    const aspectOptions = ratios.length ? ratios : ["16:9"];
    if (!st.aspect_ratio) st.aspect_ratio = aspectOptions[0];
    return `
    <form class="narr-vp-create stack" id="narr-vp-create-${esc(pkg.id)}" data-narr-vp-form="plan" data-prod-pkg="${esc(pkg.id)}" ${st.open ? "" : "hidden"}>
      <div class="field">
        <label>${t("prodTargetModel")}</label>
        <select class="input" data-narr-vp-change="profile" data-prod-pkg="${esc(pkg.id)}">
          <option value="">—</option>
          ${(state.narrVideoProfiles || []).map(p => `<option value="${esc(p.id)}" ${p.id === st.profile_id ? "selected" : ""}>${esc(narrVpProfileOptionLabel(p))}</option>`).join("")}
        </select>
        <p class="meta">${t("shootTargetModelHelp")}</p>
      </div>
      <div class="grid-2 narr-form-grid">
        <div class="field"><label>${t("prodGenMode")}</label>
          <select class="input" data-narr-vp-change="mode" data-prod-pkg="${esc(pkg.id)}">
            <option value="auto" ${st.generation_strategy === "auto" ? "selected" : ""}>${t("modeAuto")}</option>
            ${modes.map(m => `<option value="${esc(m)}" ${m === st.generation_strategy ? "selected" : ""}>${esc(narrVpModeLabel(m))}</option>`).join("")}
          </select>
        </div>
        <div class="field"><label>${t("prodAspect")}</label>
          <select class="input" data-narr-vp-change="aspect" data-prod-pkg="${esc(pkg.id)}">
            ${aspectOptions.map(r => `<option value="${esc(r)}" ${r === st.aspect_ratio ? "selected" : ""}>${esc(r)}</option>`).join("")}
          </select>
        </div>
      </div>
      <div class="field"><label>${t("prodQuality")}</label>
        <div class="row" style="gap:12px;flex-wrap:wrap;">
          <label class="narr-radio" style="cursor:pointer;"><input type="radio" name="narr-vp-quality-${esc(pkg.id)}" value="quality" data-narr-vp-change="quality" data-prod-pkg="${esc(pkg.id)}" ${st.quality_priority === "quality" ? "checked" : ""} /> ${t("prodQualityQuality")}</label>
          <label class="narr-radio" style="cursor:pointer;"><input type="radio" name="narr-vp-quality-${esc(pkg.id)}" value="balanced" data-narr-vp-change="quality" data-prod-pkg="${esc(pkg.id)}" ${st.quality_priority === "balanced" ? "checked" : ""} /> ${t("prodQualityBalanced")}</label>
          <label class="narr-radio" style="cursor:pointer;"><input type="radio" name="narr-vp-quality-${esc(pkg.id)}" value="fast" data-narr-vp-change="quality" data-prod-pkg="${esc(pkg.id)}" ${st.quality_priority === "fast" ? "checked" : ""} /> ${t("prodQualityFast")}</label>
        </div>
      </div>
      <div class="field"><label>${t("prodAudio")}</label>
        <select class="input" disabled><option value="auto">${t("prodAudioAuto")}</option></select>
        <p class="meta">${narrVpAudioHint(profile)}</p>
      </div>
      <div class="field"><label>${t("prodContinuity")}</label>
        <select class="input" data-narr-vp-change="continuity" data-prod-pkg="${esc(pkg.id)}">
          <option value="auto" ${st.continuity_strategy === "auto" ? "selected" : ""}>${t("prodContinuityAuto")}</option>
          ${continuity.map(([value, label]) => `<option value="${esc(value)}" ${value === st.continuity_strategy ? "selected" : ""}>${esc(label)}</option>`).join("")}
        </select>
      </div>
      <div class="field"><label>${t("prodPromptLang")}</label>
        <select class="input" data-narr-vp-change="lang" data-prod-pkg="${esc(pkg.id)}">
          <option value="auto" ${st.prompt_language === "auto" ? "selected" : ""}>${t("prodLangAuto")}</option>
          <option value="en" ${st.prompt_language === "en" ? "selected" : ""}>English</option>
          <option value="zh" ${st.prompt_language === "zh" ? "selected" : ""}>中文</option>
        </select>
      </div>
      <div class="row" style="gap:8px;flex-wrap:wrap;">
        <div><button class="btn btn-sm btn-primary" type="submit">${t("prodPlanSubmit")}</button></div>
        <div><button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="one-shot" data-prod-pkg="${esc(pkg.id)}" title="${t("prodOneShotSubmit")}">${t("prodOneShotSubmit")}</button></div>
      </div>
    </form>`;
  }

  function narrVpPlanSection(pkg) {
    const st = narrVpFormState(pkg.id);
    const plans = state.narrPromptPlans[pkg.id] || [];
    return `
    <section class="card narr-vp-plan-section" data-prod-pkg="${esc(pkg.id)}">
      <div class="row-between" style="gap:8px;flex-wrap:wrap;">
        <strong>${t("prodLayerPlan")} · EP${String(pkg.episode_number).padStart(2, "0")}</strong>
        <span class="row" style="gap:8px;flex-wrap:wrap;">
          ${plans.some(plan => (plan.clips || []).length) ? `<button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="export-zip" data-prod-pkg="${esc(pkg.id)}" title="${t("prodExportZipHelp")}">${t("prodExportZip")}</button>` : ""}
          <button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="toggle-form" data-prod-pkg="${esc(pkg.id)}">${st.open ? t("prodPlanCancel") : t("prodCreatePlan")}</button>
        </span>
      </div>
      ${narrVpPlanFormHtml(pkg)}
      <div id="narr-vp-job-${esc(pkg.id)}"></div>
      <div class="stack" style="gap:12px;margin-top:10px;">
        ${plans.length ? plans.map(plan => narrVpPlanCard(pkg, plan)).join("") : `<p class="meta">${t("prodPlanEmpty")}</p>`}
      </div>
      ${narrVpAssetsSection(pkg)}
    </section>`;
  }

  function narrVpPlanCard(pkg, plan) {
    const status = NARR_VP_STATUS[plan.status] || { key: null, cls: "" };
    const statusLabel = status.key ? t(status.key) : String(plan.status || "");
    const profile = narrVpProfileById(plan.target_profile_id);
    const clips = plan.clips || [];
    return `
    <div class="card narr-vp-pkg">
      <div class="row-between" style="gap:8px;flex-wrap:wrap;">
        <strong>${esc(plan.target_video_model_display_name || plan.target_profile_id)}</strong>
        <span class="row" style="gap:6px;flex-wrap:wrap;">
          <span class="tag">${t("prodProfileVersionLabel")} v${esc(plan.target_profile_version)}</span>
          <span class="tag">${esc(plan.aspect_ratio)}</span>
          <span class="tag ${status.cls}">${esc(statusLabel)}</span>
        </span>
      </div>
      ${plan.profile_update_available ? `<div class="narr-prod-stale warn"><div class="row-between" style="gap:8px;"><span>${t("prodProfileUpdateBanner")}</span><button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="pkg-recompile" data-prod-pkg="${esc(pkg.id)}" data-plan-id="${esc(plan.id)}">${t("prodRecompile")}</button></div></div>` : ""}
      ${plan.stale && !plan.profile_update_available ? `<div class="narr-prod-stale">${t("prodStalePlanBanner")}</div>` : ""}
      ${plan.status === "clip_planned" ? `
      <div class="row-between narr-vp-review-bar" style="gap:8px;margin-top:10px;flex-wrap:wrap;">
        <span class="meta">${t("prodClipPlanReviewHint")}</span>
        <button class="btn btn-sm btn-primary" type="button" data-narr-vp-action="compile-prompts" data-prod-pkg="${esc(pkg.id)}" data-plan-id="${esc(plan.id)}">${t("prodCompilePrompts")}</button>
      </div>` : ""}
      <div class="stack" style="gap:10px;margin-top:10px;">
        ${clips.map(clip => narrVpClipCard(plan, clip, profile)).join("") || `<p class="meta">—</p>`}
      </div>
    </div>`;
  }

  function narrVpClipCard(plan, clip, profile) {
    const prodPkgId = plan.production_package_id;
    const ready = plan.status === "ready";
    const clipMeta = `data-prod-pkg="${esc(prodPkgId)}" data-plan-id="${esc(plan.id)}" data-clip-id="${esc(clip.id)}"`;
    const durations = profile && profile.durations ? profile.durations : null;
    const supported = durations && Array.isArray(durations.supported) ? durations.supported : null;
    const modes = profile && Array.isArray(profile.modes) ? profile.modes : [];
    const durationControl = supported && supported.length
      ? `<select class="input narr-vp-duration" data-narr-vp-change="clip-duration" ${clipMeta}>
          ${supported.map(d => `<option value="${esc(d)}" ${Number(d) === Number(clip.duration_seconds) ? "selected" : ""}>${esc(d)}s</option>`).join("")}
        </select>`
      : `<input class="input narr-vp-duration" type="number" step="1" value="${esc(clip.duration_seconds)}"${durations && durations.min != null ? ` min="${esc(durations.min)}"` : ""}${durations && durations.max != null ? ` max="${esc(durations.max)}"` : ""} data-narr-vp-change="clip-duration" ${clipMeta} />`;
    const shots = narrVpShotRanges(clip.source_shot_numbers);
    return `
    <div class="narr-vp-clip" data-clip-id="${esc(clip.id)}">
      <div class="row-between" style="gap:8px;flex-wrap:wrap;">
        <strong>Clip ${esc(clip.clip_number)} · ${esc(clip.duration_seconds)}s</strong>
        ${shots ? `<span class="meta">${t("clipSourceShots")}：${esc(shots)}</span>` : ""}
      </div>
      ${clip.purpose ? `<p class="meta">${t("clipPurpose")}：${esc(clip.purpose)}</p>` : ""}
      <p class="meta">${t("clipGenMode")}：${esc(narrVpModeLabel(clip.generation_mode || "auto"))}</p>
      <p class="meta">${t("clipRefs")}：${narrVpClipRefNames(clip.reference_asset_ids)}</p>
      ${(clip.continuity_constraints || []).length ? `<p class="meta">${t("clipContinuity")}：${esc(clip.continuity_constraints.join("；"))}</p>` : ""}
      <div class="row narr-vp-clip-edit" style="gap:8px;flex-wrap:wrap;align-items:center;">
        <span class="meta">${t("clipEditDuration")}</span>${durationControl}
        <span class="meta">${t("clipEditMode")}</span>
        <select class="input" data-narr-vp-change="clip-mode" ${clipMeta}>
          <option value="auto" ${(clip.generation_mode || "auto") === "auto" ? "selected" : ""}>${t("modeAuto")}</option>
          ${modes.map(m => `<option value="${esc(m)}" ${m === clip.generation_mode ? "selected" : ""}>${esc(narrVpModeLabel(m))}</option>`).join("")}
        </select>
      </div>
      <div class="row" style="gap:8px;flex-wrap:wrap;margin-top:6px;">
        <button class="btn btn-sm btn-ghost" type="button" data-narr-vp-action="split" data-clip-number="${esc(clip.clip_number)}" ${clipMeta}>${t("clipSplit")}</button>
        <button class="btn btn-sm btn-ghost" type="button" data-narr-vp-action="merge" data-clip-number="${esc(clip.clip_number)}" ${clipMeta} ${Number(clip.clip_number) <= 1 ? "disabled" : ""}>${t("clipMerge")}</button>
      </div>
      ${ready ? narrVpClipPromptBlock(plan, clip, clipMeta) : ""}
    </div>`;
  }

  function narrVpClipPromptBlock(plan, clip, clipMeta) {
    const settings = clip.recommended_settings && typeof clip.recommended_settings === "object"
      ? Object.entries(clip.recommended_settings)
        .map(([key, value]) => `<li>${esc(key)}：${esc(typeof value === "object" ? JSON.stringify(value) : String(value))}</li>`)
        .join("")
      : "";
    return `
      <div class="narr-vp-prompt-block">
        <p class="narr-vp-prompt-label">${t("clipPrompt")}</p>
        <pre class="narr-vp-prompt-text">${esc(clip.prompt || "—")}</pre>
        ${clip.audio_prompt ? `<p class="narr-vp-prompt-label">${t("clipAudioPrompt")}</p><pre class="narr-vp-prompt-text">${esc(clip.audio_prompt)}</pre>` : ""}
        ${clip.negative_prompt ? `<p class="narr-vp-prompt-label">${t("clipNegativePrompt")}</p><pre class="narr-vp-prompt-text">${esc(clip.negative_prompt)}</pre>` : ""}
        ${(clip.continuity_constraints || []).length ? `<p class="narr-vp-prompt-label">${t("clipContinuity")}</p><ul>${clip.continuity_constraints.map(item => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
        ${settings ? `<p class="narr-vp-prompt-label">${t("clipRecommended")}</p><ul>${settings}</ul>` : ""}
        <div class="row" style="gap:8px;flex-wrap:wrap;margin-top:8px;">
          <button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="copy-prompt" ${clipMeta}>${t("clipCopyPrompt")}</button>
          <button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="copy-all" ${clipMeta}>${t("clipCopyAll")}</button>
          <button class="btn btn-sm btn-ghost" type="button" data-narr-vp-action="clip-recompile" data-clip-number="${esc(clip.clip_number)}" ${clipMeta}>${t("clipRecompile")}</button>
          <button class="btn btn-sm btn-ghost" type="button" data-narr-vp-action="clip-ask" data-clip-number="${esc(clip.clip_number)}" ${clipMeta}>${t("clipAskAgent")}</button>
        </div>
      </div>`;
  }

  function narrVpCopyAllText(plan, clip) {
    const byId = narrVpAssetsById();
    const refs = (clip.reference_asset_ids || []).map(id => {
      const asset = byId.get(id);
      return asset ? `${asset.name}${(asset.source_uri || asset.local_path) ? "" : ` ${t("clipRefsNone")}`}` : id;
    });
    const rows = [
      `${t("prodTargetModel")}：${plan.target_video_model_display_name || plan.target_profile_id}`,
      `${t("clipGenMode")}：${narrVpModeLabel(clip.generation_mode || "auto")}`,
      `${t("clipDuration")}：${clip.duration_seconds}s`,
      `${t("prodAspect")}：${clip.aspect_ratio || plan.aspect_ratio}`,
      `${t("clipRefs")}：${refs.length ? refs.join("、") : t("clipRefsNone")}`,
      `${t("clipPrompt")}：\n${clip.prompt || "—"}`,
    ];
    if (clip.audio_prompt) rows.push(`${t("clipAudioPrompt")}：\n${clip.audio_prompt}`);
    if (clip.negative_prompt) rows.push(`${t("clipNegativePrompt")}：\n${clip.negative_prompt}`);
    if (clip.recommended_settings && Object.keys(clip.recommended_settings).length) {
      const rec = Object.entries(clip.recommended_settings)
        .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`)
        .join("; ");
      rows.push(`${t("clipRecommended")}：${rec}`);
    }
    return rows.join("\n\n");
  }

  function narrVpPackageAssets(pkg) {
    return (state.narrProductionAssets || []).filter(asset => {
      const boundPkg = asset.metadata && asset.metadata.production_package_id;
      if (boundPkg && boundPkg !== pkg.id) return false;
      if (asset.episode_number != null && pkg.episode_number != null && asset.episode_number !== pkg.episode_number) return false;
      return true;
    });
  }

  function narrVpAssetsSection(pkg) {
    const assets = narrVpPackageAssets(pkg);
    const typeOptions = Object.keys(NARR_VP_ASSET_TYPE_I18N)
      .map(value => `<option value="${esc(value)}">${esc(narrVpAssetTypeLabel(value))}</option>`)
      .join("");
    return `
    <details class="narr-advanced-block narr-vp-assets">
      <summary>${t("assetsTitle")}（${assets.length}）</summary>
      <ul class="narr-vp-asset-list">
        ${assets.map(asset => `<li>${(asset.source_uri || asset.local_path) ? "✓" : "○"} ${esc(asset.name)} <span class="meta">${esc(narrVpAssetTypeLabel(asset.asset_type))}</span></li>`).join("") || `<li class="meta">—</li>`}
      </ul>
      <form class="row" style="gap:8px;flex-wrap:wrap;margin-top:8px;" data-narr-vp-form="asset" data-prod-pkg="${esc(pkg.id)}">
        <input class="input" id="narr-vp-asset-name-${esc(pkg.id)}" placeholder="${t("assetName")}" required style="flex:1;min-width:120px;" />
        <select class="input" id="narr-vp-asset-type-${esc(pkg.id)}">${typeOptions}</select>
        <input class="input" id="narr-vp-asset-uri-${esc(pkg.id)}" placeholder="${t("assetUri")}" required style="flex:1;min-width:160px;" />
        <button class="btn btn-sm btn-secondary" type="submit">${t("assetAdd")}</button>
      </form>
      <p class="meta">○ = ${t("assetUnbound")}</p>
    </details>`;
  }

  // ─── Layer 3: full production guide (deliverable, not the editor) ────
  function narrVpGuideSummaryCard(pkg, guide) {
    const assets = guide.required_assets || [];
    const countByType = type => assets.filter(asset => asset.asset_type === type).length;
    const charCount = countByType("character");
    const locCount = countByType("location");
    const propCount = countByType("prop");
    const otherCount = assets.length - charCount - locCount - propCount;
    const clips = guide.clip_workflows || [];
    const totalDuration = clips.reduce((sum, clip) => sum + (Number(clip.duration_seconds) || 0), 0);
    return `
    <div class="narr-guide-summary">
      <div class="row-between" style="gap:8px;flex-wrap:wrap;">
        <strong>${esc(guide.title || t("prodLayerGuide"))}</strong>
        <span class="row" style="gap:6px;flex-wrap:wrap;">
          <span class="tag">${t("prodTargetModel")}：${esc(guide.target_video_model_display_name || guide.target_profile_id)}</span>
          <span class="tag">${esc(guide.aspect_ratio)}</span>
        </span>
      </div>
      <p class="meta">${t("prodGuideAssetsNeeded")}：${charCount} ${t("prodGuideAssetChar")} · ${locCount} ${t("prodGuideAssetLoc")} · ${propCount} ${t("prodGuideAssetProp")}${otherCount > 0 ? ` · ${otherCount} ${t("prodGuideAssetOther")}` : ""}</p>
      <p class="meta">${t("prodGuideClips")}：${clips.length} · ${t("prodGuideTotalDuration")}：${totalDuration}s</p>
      ${guide.stale ? `<div class="narr-prod-stale"><div class="row-between" style="gap:8px;"><span>${t("prodGuideStaleBanner")}</span><button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="guide-create" data-prod-pkg="${esc(pkg.id)}">${t("prodGuideRegenerate")}</button></div></div>` : ""}
      ${guide.profile_update_available && !guide.stale ? `<div class="narr-prod-stale warn"><div class="row-between" style="gap:8px;"><span>${t("prodProfileUpdateBanner")}</span><button class="btn btn-sm btn-secondary" type="button" data-narr-vp-action="guide-create" data-prod-pkg="${esc(pkg.id)}">${t("prodGuideRegenerate")}</button></div></div>` : ""}
      <div class="row" style="gap:8px;flex-wrap:wrap;">
        <button class="btn btn-sm btn-primary" type="button" data-narr-vp-action="guide-open" data-prod-pkg="${esc(pkg.id)}">${t("prodGuideOpen")}</button>
      </div>
    </div>`;
  }

  function narrVpGuideSection(pkg) {
    const guide = state.narrGuides[pkg.id] || null;
    return `
    <section class="card narr-vp-guide-section" data-prod-pkg="${esc(pkg.id)}">
      <div class="row-between" style="gap:8px;flex-wrap:wrap;">
        <strong>${t("prodLayerGuide")} · EP${String(pkg.episode_number).padStart(2, "0")}</strong>
        <button class="btn btn-sm ${guide ? "btn-secondary" : "btn-primary"}" type="button" data-narr-vp-action="guide-create" data-prod-pkg="${esc(pkg.id)}">${guide ? t("prodGuideRegenerate") : t("prodGuideCreate")}</button>
      </div>
      <div id="narr-guide-job-${esc(pkg.id)}"></div>
      ${guide ? narrVpGuideSummaryCard(pkg, guide) : `<p class="meta">${t("prodGuideEmpty")}</p>`}
    </section>`;
  }

  async function renderNarrativeProductionLayers() {
    const host = $("#narr-production-view");
    if (!host) return;
    await ensureNarrativeVideoProfiles();
    for (const pkg of state.narrProduction) {
      if (!Array.isArray(state.narrPromptPlans[pkg.id])) await loadNarrativePromptPlans(pkg.id);
    }
    host.innerHTML = `
      <div class="narr-prod-layers">
        <section class="narr-prod-layer" id="narr-vp-master-layer">
          <h4 class="narr-prod-layer-title">${t("prodLayerMaster")}</h4>
          <p class="meta">${t("prodLayerMasterHelp")}</p>
          ${state.narrProduction.length ? state.narrProduction.map(pkg => narrProductionPackageCard(pkg)).join("") : `<p class="meta">暂无制作包 — 在单集创作中生成。</p>`}
        </section>
        <section class="narr-prod-layer" id="narr-vp-plan-layer">
          <h4 class="narr-prod-layer-title">${t("prodLayerPlan")}</h4>
          <p class="meta">${t("prodLayerPlanHelp")}</p>
          ${state.narrProduction.length ? state.narrProduction.map(pkg => narrVpPlanSection(pkg)).join("") : `<p class="meta">暂无制作包 — 生成制作包后可创建视频生成方案。</p>`}
        </section>
        <section class="narr-prod-layer" id="narr-vp-guide-layer">
          <h4 class="narr-prod-layer-title">${t("prodLayerGuide")}</h4>
          <p class="meta">${t("prodLayerGuideHelp")}</p>
          ${state.narrProduction.length ? state.narrProduction.map(pkg => narrVpGuideSection(pkg)).join("") : `<p class="meta">暂无制作包 — 生成制作包后可产出完整制作手册。</p>`}
        </section>
      </div>`;
    bindNarrativeProductionEvents();
  }

  function bindNarrativeProductionEvents() {
    const host = $("#narr-production-view");
    if (!host || host._narrVpDelegated) return;
    host._narrVpDelegated = true;
    host.addEventListener("click", event => {
      const btn = event.target.closest("[data-narr-vp-action]");
      if (btn && host.contains(btn)) handleNarrativeVpAction(btn);
    });
    host.addEventListener("change", event => {
      const el = event.target.closest("[data-narr-vp-change]");
      if (el && host.contains(el)) handleNarrativeVpChange(el);
    });
    host.addEventListener("submit", event => {
      const form = event.target.closest("form[data-narr-vp-form]");
      if (form && host.contains(form)) {
        event.preventDefault();
        if (form.dataset.narrVpForm === "plan") createNarrativeClipPlanJob(form.dataset.prodPkg);
        else submitNarrativeProductionAsset(form.dataset.prodPkg);
      }
    });
  }

  function renderNarrativePlanJob(prodPkgId, progress, error) {
    const host = document.getElementById(`narr-vp-job-${prodPkgId}`);
    if (!host) return;
    if (error) { host.innerHTML = `<div class="narr-prod-stale danger">${esc(error)}</div>`; return; }
    if (!progress) { host.innerHTML = ""; return; }
    // Real stage labels + real counts only; never a fake percentage.
    const label = (progress.stage && NARR_VP_STAGE_LABELS[progress.stage])
      ? t(NARR_VP_STAGE_LABELS[progress.stage])
      : (progress.label || progress.stage || "");
    const total = Number(progress.total);
    const completed = Number(progress.completed || 0);
    const counts = Number.isFinite(total) && total > 0 ? `（${completed}/${total}）` : "";

    let percent = 5;
    if (progress.stage === "planning_clips") percent = 20;
    else if (progress.stage === "planning_assets") percent = 40;
    else if (progress.stage === "compiling_prompts") {
      percent = Number.isFinite(total) && total > 0 ? Math.min(90, 40 + Math.round((completed / total) * 50)) : 65;
    } else if (progress.stage === "validating") percent = 95;
    else if (progress.stage === "completed") percent = 100;
    if (progress.percent != null && Number.isFinite(Number(progress.percent))) {
      percent = Math.max(percent, Math.min(100, Number(progress.percent)));
    }

    host.innerHTML = `
      <div class="card narr-vp-job" style="background:var(--surface);border:1px solid var(--border-soft);padding:12px 14px;border-radius:var(--radius-sm);margin:10px 0;display:flex;flex-direction:column;gap:8px;">
        <div class="row-between" style="gap:8px;align-items:center;">
          <div class="row" style="gap:8px;align-items:center;">
            <span class="spinner"></span>
            <strong>${esc(label)}${esc(counts)}</strong>
          </div>
          <span class="meta" style="font-weight:700;font-size:12px;color:var(--accent);">${percent}%</span>
        </div>
        <div class="progress" style="height:6px;background:color-mix(in oklch, var(--text) 10%, var(--bg));border-radius:980px;overflow:hidden;">
          <div class="progress-bar" style="height:100%;width:${percent}%;background:var(--accent);border-radius:980px;transition:width 0.3s ease;"></div>
        </div>
        <div class="meta" style="font-size:12px;display:flex;justify-content:space-between;">
          <span>${t("prodJobRunningHint")}</span>
          ${Number.isFinite(total) && total > 0 ? `<span>${completed} / ${total}</span>` : ""}
        </div>
      </div>`;
  }

  async function pollNarrativePlanJob(prodPkgId, jobId) {
    state.narrPlanJobs[prodPkgId] = jobId;
    renderNarrativePlanJob(prodPkgId, { stage: "queued" });
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 800));
      const snap = await api(`/api/narrative-jobs/${jobId}`);
      if (!snap || !snap.ok) {
        renderNarrativePlanJob(prodPkgId, null, redactNarrText((snap && snap.error) || "任务状态读取失败"));
        return;
      }
      const job = snap.data;
      if (!job || !["completed", "failed", "cancelled"].includes(job.status)) {
        renderNarrativePlanJob(prodPkgId, (job && job.progress) || {});
        continue;
      }
      state.narrPlanJobs[prodPkgId] = null;
      if (job.status !== "completed") {
        const failure = parseNarrativeJobError(job);
        renderNarrativePlanJob(prodPkgId, null, redactNarrText((failure && failure.message) || job.error || "任务失败"));
        return;
      }
      renderNarrativePlanJob(prodPkgId, null);
      await loadNarrativePromptPlans(prodPkgId);
      await renderNarrativeProductionLayers();
      toast(t("toastPlanReady"));
      return;
    }
  }

  async function submitNarrativePlanJob(prodPkgId, url, body) {
    if (!state.narrProject) return;
    renderNarrativePlanJob(prodPkgId, { stage: "queued" });
    const res = await api(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res || !res.ok) {
      renderNarrativePlanJob(prodPkgId, null, redactNarrText((res && res.error) || "任务创建失败"));
      toast(redactNarrText((res && res.error) || "任务创建失败"));
      return;
    }
    await pollNarrativePlanJob(prodPkgId, res.data.id);
  }

  async function submitNarrativePromptPackageJob(prodPkgId, body) {
    await submitNarrativePlanJob(
      prodPkgId,
      `/api/narratives/${state.narrProject.id}/production/${prodPkgId}/prompt-packages`,
      body,
    );
  }

  function renderNarrativeGuideJob(prodPkgId, progress, error) {
    const host = document.getElementById(`narr-guide-job-${prodPkgId}`);
    if (!host) return;
    if (error) { host.innerHTML = `<div class="narr-prod-stale danger">${esc(error)}</div>`; return; }
    if (!progress) { host.innerHTML = ""; return; }
    // Real stage labels + real counts only; never a fake percentage.
    const label = (progress.stage && NARR_GUIDE_STAGE_LABELS[progress.stage])
      ? t(NARR_GUIDE_STAGE_LABELS[progress.stage])
      : (progress.label || progress.stage || "");
    const total = Number(progress.total);
    const completed = Number(progress.completed || 0);
    const counts = Number.isFinite(total) && total > 0 ? `（${completed}/${total}）` : "";

    let percent = 5;
    if (progress.stage === "loading_source") percent = 15;
    else if (progress.stage === "analyzing_assets") percent = 35;
    else if (progress.stage === "compiling_asset_prompts") percent = 55;
    else if (progress.stage === "compiling_clip_prompts") percent = 75;
    else if (progress.stage === "rendering_guide") percent = 90;
    else if (progress.stage === "completed") percent = 100;
    if (progress.percent != null && Number.isFinite(Number(progress.percent))) {
      percent = Math.max(percent, Math.min(100, Number(progress.percent)));
    }

    host.innerHTML = `
      <div class="card narr-vp-job" style="background:var(--surface);border:1px solid var(--border-soft);padding:12px 14px;border-radius:var(--radius-sm);margin:10px 0;display:flex;flex-direction:column;gap:8px;">
        <div class="row-between" style="gap:8px;align-items:center;">
          <div class="row" style="gap:8px;align-items:center;">
            <span class="spinner"></span>
            <strong>${esc(label)}${esc(counts)}</strong>
          </div>
          <span class="meta" style="font-weight:700;font-size:12px;color:var(--accent);">${percent}%</span>
        </div>
        <div class="progress" style="height:6px;background:color-mix(in oklch, var(--text) 10%, var(--bg));border-radius:980px;overflow:hidden;">
          <div class="progress-bar" style="height:100%;width:${percent}%;background:var(--accent);border-radius:980px;transition:width 0.3s ease;"></div>
        </div>
        <div class="meta" style="font-size:12px;display:flex;justify-content:space-between;">
          <span>${t("prodJobRunningHint")}</span>
          ${Number.isFinite(total) && total > 0 ? `<span>${completed} / ${total}</span>` : ""}
        </div>
      </div>`;
  }

  async function pollNarrativeGuideJob(prodPkgId, jobId) {
    state.narrGuideJobs[prodPkgId] = jobId;
    renderNarrativeGuideJob(prodPkgId, { stage: "created" });
    for (;;) {
      await new Promise(resolve => setTimeout(resolve, 800));
      const snap = await api(`/api/narrative-jobs/${jobId}`);
      if (!snap || !snap.ok) {
        renderNarrativeGuideJob(prodPkgId, null, redactNarrText((snap && snap.error) || "任务状态读取失败"));
        return;
      }
      const job = snap.data;
      if (!job || !["completed", "failed", "cancelled"].includes(job.status)) {
        renderNarrativeGuideJob(prodPkgId, (job && job.progress) || {});
        continue;
      }
      state.narrGuideJobs[prodPkgId] = null;
      if (job.status !== "completed") {
        const failure = parseNarrativeJobError(job);
        renderNarrativeGuideJob(prodPkgId, null, redactNarrText((failure && failure.message) || job.error || "任务失败"));
        return;
      }
      renderNarrativeGuideJob(prodPkgId, null);
      await loadNarrativeGuide(prodPkgId);
      await renderNarrativeProductionLayers();
      toast(t("prodGuideReady"));
      return;
    }
  }

  async function createNarrativeGuideJob(prodPkgId) {
    if (!state.narrProject) return;
    renderNarrativeGuideJob(prodPkgId, { stage: "created" });
    const res = await api(`/api/narratives/${state.narrProject.id}/production/${prodPkgId}/production-guide`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res || !res.ok) {
      renderNarrativeGuideJob(prodPkgId, null, redactNarrText((res && res.error) || "任务创建失败"));
      toast(redactNarrText((res && res.error) || "任务创建失败"));
      return;
    }
    await pollNarrativeGuideJob(prodPkgId, res.data.id);
  }

  async function createNarrativeClipPlanJob(prodPkgId) {
    const st = narrVpFormState(prodPkgId);
    if (!st.profile_id) { toast(t("toastSelectTargetModel")); return; }
    const profile = narrVpProfileById(st.profile_id);
    renderNarrativePlanJob(prodPkgId, { stage: "queued" });
    await submitNarrativePlanJob(
      prodPkgId,
      `/api/narratives/${state.narrProject.id}/production/${prodPkgId}/clip-plan`,
      {
        profile_id: st.profile_id,
        aspect_ratio: st.aspect_ratio || (profile && profile.aspect_ratios && profile.aspect_ratios[0]) || "16:9",
        quality_priority: st.quality_priority || "balanced",
        generation_strategy: st.generation_strategy || "auto",
        continuity_strategy: st.continuity_strategy || "auto",
        audio_strategy: "auto",
        prompt_language: st.prompt_language || "auto",
      },
    );
  }

  async function compileNarrativePromptPackage(prodPkgId, plan) {
    renderNarrativePlanJob(prodPkgId, { stage: "queued" });
    await submitNarrativePlanJob(
      prodPkgId,
      `/api/narratives/${state.narrProject.id}/prompt-packages/${plan.id}/compile-prompts`,
      {
        profile_id: plan.target_profile_id,
        aspect_ratio: plan.aspect_ratio || "16:9",
        quality_priority: plan.quality_priority || "balanced",
        generation_strategy: plan.generation_strategy || "auto",
        continuity_strategy: plan.continuity_strategy || "auto",
        audio_strategy: plan.audio_strategy || "auto",
        prompt_language: plan.prompt_language || "auto",
      },
    );
  }

  async function createNarrativePromptPackageJob(prodPkgId) {
    const st = narrVpFormState(prodPkgId);
    if (!st.profile_id) { toast(t("toastSelectTargetModel")); return; }
    const profile = narrVpProfileById(st.profile_id);
    renderNarrativePlanJob(prodPkgId, { stage: "queued" });
    await submitNarrativePromptPackageJob(prodPkgId, {
      profile_id: st.profile_id,
      aspect_ratio: st.aspect_ratio || (profile && profile.aspect_ratios && profile.aspect_ratios[0]) || "16:9",
      quality_priority: st.quality_priority || "balanced",
      generation_strategy: st.generation_strategy || "auto",
      continuity_strategy: st.continuity_strategy || "auto",
      audio_strategy: "auto",
      prompt_language: st.prompt_language || "auto",
    });
  }

  async function recompileNarrativePromptPackage(prodPkgId, plan) {
    renderNarrativePlanJob(prodPkgId, { stage: "queued" });
    await submitNarrativePromptPackageJob(prodPkgId, {
      profile_id: plan.target_profile_id,
      aspect_ratio: plan.aspect_ratio || "16:9",
      quality_priority: plan.quality_priority || "balanced",
      generation_strategy: plan.generation_strategy || "auto",
      continuity_strategy: plan.continuity_strategy || "auto",
      audio_strategy: plan.audio_strategy || "auto",
      prompt_language: plan.prompt_language || "auto",
    });
  }

  async function patchNarrativeClip(plan, clip, patch) {
    const res = await api(`/api/narratives/${state.narrProject.id}/prompt-packages/${plan.id}/clips/${clip.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (res && res.ok) {
      toast(t("toastClipSaved"));
    } else {
      toast(redactNarrText((res && res.error) || "更新失败"));
    }
    await loadNarrativePromptPlans(plan.production_package_id);
    await renderNarrativeProductionLayers();
  }

  async function submitNarrativeProductionAsset(prodPkgId) {
    const name = (($(`#narr-vp-asset-name-${prodPkgId}`) || {}).value || "").trim();
    const uri = (($(`#narr-vp-asset-uri-${prodPkgId}`) || {}).value || "").trim();
    const assetType = (($(`#narr-vp-asset-type-${prodPkgId}`) || {}).value || "other");
    const pkg = (state.narrProduction || []).find(item => item.id === prodPkgId);
    if (!name) { toast(t("toastNeedAssetName")); return; }
    if (!uri) { toast(t("toastNeedAssetUri")); return; }
    const res = await api(`/api/narratives/${state.narrProject.id}/production-assets`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        asset_type: assetType,
        name,
        source_uri: uri,
        episode_number: pkg ? pkg.episode_number : undefined,
        production_package_id: prodPkgId,
      }),
    });
    if (res && res.ok) {
      toast(t("toastAssetCreated"));
      await loadNarrativeProduction();
    } else toast(redactNarrText((res && res.error) || "登记失败"));
  }

  function shootClipModifyText(plan, clip) {
    if (!clip) return t("shootClipModifyInstruction").replace("{n}", "?").replace("{d}", "?").replace("{m}", "-").replace("{p}", "-");
    return t("shootClipModifyInstruction")
      .replace("{n}", String(clip.clip_number))
      .replace("{d}", String(clip.duration_seconds))
      .replace("{m}", narrVpModeLabel(clip.generation_mode || "auto"))
      .replace("{p}", clip.purpose || "—");
  }

  async function handleNarrativeVpAction(btn) {
    const action = btn.dataset.narrVpAction;
    const prodPkgId = btn.dataset.prodPkg;
    if (action === "toggle-form") {
      const st = narrVpFormState(prodPkgId);
      st.open = !st.open;
      const form = document.getElementById(`narr-vp-create-${prodPkgId}`);
      if (form) form.hidden = !st.open;
      btn.textContent = st.open ? t("prodPlanCancel") : t("prodCreatePlan");
      return;
    }
    const plan = (state.narrPromptPlans[prodPkgId] || []).find(item => item.id === btn.dataset.planId);
    const clip = plan && (plan.clips || []).find(item => item.id === btn.dataset.clipId);
    if (action === "split" || action === "merge" || action === "clip-recompile" || action === "clip-ask") {
      const number = btn.dataset.clipNumber;
      let instruction = "";
      if (action === "split") instruction = t("shootSplitInstruction").replace("{n}", number);
      else if (action === "merge") instruction = t("shootMergeInstruction").replace("{a}", String(Number(number) - 1)).replace("{b}", number);
      else if (action === "clip-recompile") instruction = t("shootRecompileInstruction").replace("{n}", number);
      else instruction = shootClipModifyText(plan, clip);
      await openShootingPanelWithInstruction(instruction);
      return;
    }
    if (action === "copy-prompt" && clip) { await copyNarrText(clip.prompt || ""); return; }
    if (action === "copy-all" && clip) { await copyNarrText(narrVpCopyAllText(plan, clip)); return; }
    if (action === "guide-create" && prodPkgId && state.narrProject) { await createNarrativeGuideJob(prodPkgId); return; }
    if (action === "guide-open" && prodPkgId) { openNarrativeGuideDrawer(prodPkgId); return; }
    if (action === "compile-prompts" && plan) { await compileNarrativePromptPackage(prodPkgId, plan); return; }
    if (action === "one-shot") { await createNarrativePromptPackageJob(prodPkgId); return; }
    if (action === "pkg-recompile" && plan) { await recompileNarrativePromptPackage(prodPkgId, plan); }
    if (action === "export-zip" && prodPkgId && state.narrProject) {
      // File download bypasses the JSON api() helper: direct navigation to
      // the zip endpoint (server sets Content-Disposition: attachment).
      const link = document.createElement("a");
      link.href = `/api/narratives/${state.narrProject.id}/production/${prodPkgId}/prompt-packages/export`;
      link.download = "";
      document.body.appendChild(link);
      link.click();
      link.remove();
    }
  }

  async function handleNarrativeVpChange(el) {
    const kind = el.dataset.narrVpChange;
    const prodPkgId = el.dataset.prodPkg;
    const st = narrVpFormState(prodPkgId);
    if (kind === "profile") {
      st.profile_id = el.value;
      const profile = narrVpProfileById(el.value);
      if (profile) {
        if (Array.isArray(profile.modes) && profile.modes.length && st.generation_strategy !== "auto" && !profile.modes.includes(st.generation_strategy)) {
          st.generation_strategy = "auto";
        }
        if (Array.isArray(profile.aspect_ratios) && profile.aspect_ratios.length) {
          st.aspect_ratio = profile.aspect_ratios.includes(st.aspect_ratio) ? st.aspect_ratio : profile.aspect_ratios[0];
        }
      }
      renderNarrativePlanForm(prodPkgId);
      return;
    }
    if (kind === "mode") { st.generation_strategy = el.value; return; }
    if (kind === "aspect") { st.aspect_ratio = el.value; return; }
    if (kind === "quality") { st.quality_priority = el.value; return; }
    if (kind === "continuity") { st.continuity_strategy = el.value; return; }
    if (kind === "lang") { st.prompt_language = el.value; return; }
    if (kind === "clip-duration" || kind === "clip-mode") {
      const plan = (state.narrPromptPlans[prodPkgId] || []).find(item => item.id === el.dataset.planId);
      const clip = plan && (plan.clips || []).find(item => item.id === el.dataset.clipId);
      if (!plan || !clip) return;
      const patch = kind === "clip-duration"
        ? { duration_seconds: Number(el.value) }
        : { generation_mode: el.value };
      await patchNarrativeClip(plan, clip, patch);
    }
  }

  // ─── Full production guide drawer (safe markdown + per-item copy) ────
  function ensureNarrativeGuideDrawer() {
    let drawer = document.getElementById("narr-guide-drawer");
    if (drawer) return drawer;
    drawer = document.createElement("div");
    drawer.id = "narr-guide-drawer";
    drawer.className = "narr-guide-drawer";
    drawer.hidden = true;
    document.body.appendChild(drawer);
    // Delegated clicks survive every re-render of the drawer content.
    drawer.addEventListener("click", event => {
      const btn = event.target.closest("[data-narr-guide-action]");
      if (btn) handleNarrativeGuideDrawerAction(btn);
    });
    return drawer;
  }

  function narrGuideAssetItem(asset) {
    return `
    <div class="narr-guide-asset">
      <div class="row-between" style="gap:8px;flex-wrap:wrap;">
        <strong>${esc(asset.asset_key || asset.name)}</strong>
        <span class="row" style="gap:6px;flex-wrap:wrap;">
          <span class="tag">${esc(asset.asset_type)}</span>
          <span class="tag">${esc(asset.necessity)}</span>
          <span class="tag">${esc(asset.status)}</span>
        </span>
      </div>
      <p class="meta">${esc(asset.name)}${asset.purpose ? ` · ${esc(asset.purpose)}` : ""}</p>
      ${asset.generation_prompt ? `
      <pre class="narr-vp-prompt-text">${esc(asset.generation_prompt)}</pre>
      <div class="row" style="gap:8px;margin-top:6px;">
        <button class="btn btn-sm btn-secondary" type="button" data-narr-guide-action="copy-asset" data-asset-id="${esc(asset.id)}">${t("prodGuideCopyAssetPrompt")}</button>
      </div>` : ""}
      ${(asset.notes || []).length ? `<ul class="meta">${asset.notes.map(note => `<li>${esc(note)}</li>`).join("")}</ul>` : ""}
    </div>`;
  }

  function narrGuideClipItem(clip) {
    const shots = narrVpShotRanges(clip.source_shot_numbers);
    const settings = clip.recommended_settings && typeof clip.recommended_settings === "object"
      ? Object.entries(clip.recommended_settings)
        .map(([key, value]) => `<li>${esc(key)}：${esc(typeof value === "object" ? JSON.stringify(value) : String(value))}</li>`)
        .join("")
      : "";
    return `
    <div class="narr-guide-clip">
      <div class="row-between" style="gap:8px;flex-wrap:wrap;">
        <strong>Clip ${esc(clip.clip_number)} · ${esc(clip.duration_seconds)}s</strong>
        ${shots ? `<span class="meta">${t("clipSourceShots")}：${esc(shots)}</span>` : ""}
      </div>
      ${clip.purpose ? `<p class="meta">${t("clipPurpose")}：${esc(clip.purpose)}</p>` : ""}
      <p class="meta">${t("clipGenMode")}：${esc(narrVpModeLabel(clip.generation_mode || "auto"))}</p>
      ${(clip.continuity_constraints || []).length ? `<p class="meta">${t("clipContinuity")}：${esc(clip.continuity_constraints.join("；"))}</p>` : ""}
      <pre class="narr-vp-prompt-text">${esc(clip.copy_ready_prompt || clip.prompt || "—")}</pre>
      ${settings ? `<p class="narr-vp-prompt-label">${t("clipRecommended")}</p><ul>${settings}</ul>` : ""}
      <div class="row" style="gap:8px;margin-top:6px;">
        <button class="btn btn-sm btn-secondary" type="button" data-narr-guide-action="copy-clip" data-clip-id="${esc(clip.id)}">${t("prodGuideCopyClipPrompt")}</button>
      </div>
    </div>`;
  }

  function openNarrativeGuideDrawer(prodPkgId) {
    const guide = state.narrGuides[prodPkgId];
    if (!guide || !state.narrProject) return;
    const drawer = ensureNarrativeGuideDrawer();
    state.narrGuideDrawerPkg = prodPkgId;
    const assets = guide.required_assets || [];
    const clips = guide.clip_workflows || [];
    // The markdown body goes through the shared safe-rendering bridge;
    // every interpolated field outside it is escaped with esc().
    drawer.innerHTML = `
      <div class="narr-guide-drawer-backdrop" data-narr-guide-action="close"></div>
      <div class="narr-guide-drawer-panel" role="dialog" aria-modal="true">
        <div class="narr-guide-drawer-head">
          <strong>${esc(guide.title || t("prodLayerGuide"))} · EP${String(guide.episode_number).padStart(2, "0")}</strong>
          <span class="row" style="gap:8px;flex-wrap:wrap;">
            <button class="btn btn-sm btn-secondary" type="button" data-narr-guide-action="copy-all">${t("prodGuideCopyAll")}</button>
            <button class="btn btn-sm btn-secondary" type="button" data-narr-guide-action="export-md">${t("prodGuideExportMd")}</button>
            <button class="btn btn-sm btn-ghost" type="button" data-narr-guide-action="close">✕ ${t("prodGuideClose")}</button>
          </span>
        </div>
        <div class="narr-guide-drawer-body">
          <p class="meta">${t("prodGuideCopyHint")}</p>
          <div class="narr-guide-markdown">${renderMessageBody(guide.markdown_document || "")}</div>
          <h4>${t("prodGuideAssetsNeeded")}（${assets.length}）</h4>
          <div class="stack" style="gap:10px;">${assets.map(narrGuideAssetItem).join("") || `<p class="meta">—</p>`}</div>
          <h4>${t("prodGuideClips")}（${clips.length}）</h4>
          <div class="stack" style="gap:10px;">${clips.map(narrGuideClipItem).join("") || `<p class="meta">—</p>`}</div>
        </div>
      </div>`;
    drawer.hidden = false;
  }

  function handleNarrativeGuideDrawerAction(btn) {
    const action = btn.dataset.narrGuideAction;
    const prodPkgId = state.narrGuideDrawerPkg;
    const guide = prodPkgId ? state.narrGuides[prodPkgId] : null;
    if (!guide) return;
    if (action === "close") {
      const drawer = document.getElementById("narr-guide-drawer");
      if (drawer) drawer.hidden = true;
      state.narrGuideDrawerPkg = null;
      return;
    }
    if (action === "copy-all") { copyNarrText(guide.markdown_document || ""); return; }
    if (action === "copy-asset") {
      const asset = (guide.required_assets || []).find(item => item.id === btn.dataset.assetId);
      if (asset) copyNarrText(asset.generation_prompt || "");
      return;
    }
    if (action === "copy-clip") {
      const clip = (guide.clip_workflows || []).find(item => item.id === btn.dataset.clipId);
      if (clip) copyNarrText(clip.copy_ready_prompt || clip.prompt || "");
      return;
    }
    if (action === "export-md" && state.narrProject) {
      // File download bypasses the JSON api() helper: direct navigation to
      // the markdown endpoint (server sets Content-Disposition: attachment).
      const link = document.createElement("a");
      link.href = `/api/narratives/${state.narrProject.id}/production-guides/${guide.id}/export`;
      link.download = "";
      document.body.appendChild(link);
      link.click();
      link.remove();
    }
  }

  function renderNarrativePlanForm(prodPkgId) {
    const host = document.getElementById(`narr-vp-create-${prodPkgId}`);
    const pkg = (state.narrProduction || []).find(item => item.id === prodPkgId);
    if (!host || !pkg) return;
    host.outerHTML = narrVpPlanFormHtml(pkg);
  }

  function openNarrativeCreateModal() {
    const dlg = $("#dlg-narr-new");
    const err = $("#narr-new-error");
    if (err) { err.hidden = true; err.textContent = ""; }
    setField("narr-new-title", "");
    setField("narr-new-logline", "");
    setField("narr-new-description", "");
    setField("narr-new-format", "micro_drama");
    setField("narr-new-genre", "");
    setField("narr-new-tone", "");
    setField("narr-new-audience", "");
    setField("narr-new-episodes", 60);
    setField("narr-new-dur-min", 90);
    setField("narr-new-dur-max", 120);
    if (dlg && dlg.showModal) dlg.showModal();
  }

  async function submitNarrativeCreate(event) {
    event.preventDefault();
    const payload = collectProjectSettings("narr-new");
    const err = $("#narr-new-error");
    if (!payload.title || !payload.logline) {
      if (err) { err.hidden = false; err.textContent = "标题和 Logline 为必填。"; }
      return;
    }
    const res = await api("/api/narratives", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res && res.ok) {
      const dlg = $("#dlg-narr-new");
      if (dlg && dlg.open) dlg.close();
      toast("作品已创建");
      await loadNarrativeProjects();
      await openNarrativeProject(res.data.id);
    } else if (err) {
      err.hidden = false;
      err.textContent = (res && res.error) || "创建失败";
    } else toast((res && res.error) || "创建失败");
  }

  async function saveNarrativeSettings() {
    if (!state.narrProject) return;
    const payload = collectProjectSettings("narr-set");
    if (!payload.title || !payload.logline) { toast("标题和 Logline 为必填"); return; }
    const res = await api(`/api/narratives/${state.narrProject.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res && res.ok) {
      state.narrProject = res.data;
      renderNarrativeHeader();
      toast("作品设定已保存");
    } else toast((res && res.error) || "保存失败");
  }

  function deleteNarrativeProject() {
    if (!state.narrProject) return;
    confirmDlg(`确定删除「${state.narrProject.title}」？此操作不可恢复。`, "删除作品", async () => {
      const res = await api(`/api/narratives/${state.narrProject.id}`, { method: "DELETE" });
      if (res && res.ok) {
        toast("作品已删除");
        showNarrativeSub("list");
        await loadNarrativeProjects();
      } else toast((res && res.error) || "删除失败");
    }, true);
  }

  async function narrEpisodeAction(action) {
    if (!state.narrProject) return;
    const epNum = state.narrEpNumber;
    const ep = currentEpisode();
    const pipe = pipelineState(ep);
    if (action === "draft" && pipe.draft.disabled) { toast(pipe.draft.reason); return; }
    if (action === "audit" && pipe.audit.disabled) { toast(pipe.audit.reason); return; }
    if (action === "commit" && pipe.commit.disabled) { toast(pipe.commit.reason); return; }
    const pid = state.narrProject.id;
    let res;
    if (action === "outline") {
      const job = await runNarrativeJob("outline", narrativePayload("outline_writer", {
        episode_count: state.narrProject.planned_episode_count,
      }), "Outline");
      if (!job) return;
      res = { ok: true, data: job.result };
    } else if (action === "draft") {
      const job = await runNarrativeJob("screenwriter", narrativePayload("screenwriter", {
        episode_number: epNum,
      }), "Screenwriter");
      if (!job) return;
      res = { ok: true, data: job.result };
    } else if (action === "audit") {
      const version = latestEpisodeVersion(ep);
      const job = await runNarrativeJob("audit", narrativePayload("reviewer", {
        episode_number: epNum, version_id: version.id,
      }), "Audit");
      if (!job) return;
      res = { ok: true, data: job.result };
    } else if (action === "commit") {
      const version = latestEpisodeVersion(ep);
      res = await api(`/api/narratives/${pid}/episodes/${epNum}/commit`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version_id: version.id }),
      });
    } else if (action === "production") {
      const job = await runNarrativeJob("production_package", narrativePayload("production_planner", {
        episode_number: epNum,
      }), "Production Planner");
      if (!job) return;
      res = { ok: true, data: job.result };
    }
    if (!res) return;
    if (res.ok) {
      toast("完成");
      await refreshNarrativeAll();
    } else toast((res && res.error) || "操作失败");
  }

  async function refreshNarrativeAll() {
    await Promise.all([
      loadNarrativeBible(),
      loadNarrativeCast(),
      loadNarrativeEpisodes(),
      loadNarrativeKnowledge(),
      loadNarrativeClues(),
      loadNarrativeForecasts(),
      loadNarrativeProduction(),
      loadNarrativeContinuityExtras(),
    ]);
    renderNarrativeHeader();
  }

  function initNarrativeUI() {
    onClick("#btn-narr-new", openNarrativeCreateModal);
    onClick("#narr-new-cancel", () => { const dlg = $("#dlg-narr-new"); if (dlg) dlg.close(); });
    const newForm = $("#form-narr-new");
    if (newForm) newForm.addEventListener("submit", submitNarrativeCreate);
    onClick("#btn-narr-refresh", loadNarrativeProjects);
    onClick("#btn-narr-delete-selected", deleteSelectedNarrativeProjects);
    const selectAll = $("#narr-select-all");
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        state.narrSelectedIds = selectAll.checked
          ? (state.narrProjects || []).map(item => item.id)
          : [];
        renderNarrativeProjects();
      });
    }
    onClick("#btn-narr-back", () => { showNarrativeSub("list"); loadNarrativeProjects(); });
    $$("#narr-tabs button").forEach(btn => btn.addEventListener("click", () => showNarrativeTab(btn.dataset.narrTab)));
    onClick("#btn-narr-runtime", openNarrativeRuntimeModal);
    onClick("#btn-narr-runtime-refresh", rescanNarrativeRuntime);
    onClick("#narr-runtime-cancel", () => { const dlg = $("#dlg-narr-runtime"); if (dlg) dlg.close(); });
    const runtimeForm = $("#form-narr-runtime");
    if (runtimeForm) runtimeForm.addEventListener("submit", saveNarrativeRuntimeRouting);
    const advanced = $("#narr-advanced-mode");
    if (advanced) advanced.addEventListener("change", () => {
      state.narrAdvanced = advanced.checked;
      localStorage.setItem("pc-narr-advanced", advanced.checked ? "1" : "0");
      syncNarrAdvancedUi();
    });
    onClick("#btn-narr-settings-save", saveNarrativeSettings);
    onClick("#btn-narr-delete", deleteNarrativeProject);
    onClick("#btn-narr-bible-generate", async () => {
      if (!state.narrProject) return;
      const job = await runNarrativeJob("story_bible", narrativePayload("story_architect"), "Story Bible");
      if (job) await loadNarrativeBible();
      renderNarrativeHeader();
    });
    onClick("#btn-narr-bible-edit", () => {
      state.narrBibleEditing = true;
      $("#btn-narr-bible-save").hidden = false;
      $("#btn-narr-bible-cancel").hidden = false;
      renderNarrativeBible();
    });
    onClick("#btn-narr-bible-cancel", () => {
      state.narrBibleEditing = false;
      $("#btn-narr-bible-save").hidden = true;
      $("#btn-narr-bible-cancel").hidden = true;
      renderNarrativeBible();
    });
    onClick("#btn-narr-bible-save", async () => {
      if (!state.narrProject) return;
      const res = await api(`/api/narratives/${state.narrProject.id}/bible`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectBibleEdits()),
      });
      if (res && res.ok) {
        state.narrBible = res.data;
        state.narrBibleEditing = false;
        $("#btn-narr-bible-save").hidden = true;
        $("#btn-narr-bible-cancel").hidden = true;
        renderNarrativeBible();
        renderBibleSyncBanner();
        toast("已保存新版本");
      } else toast((res && res.error) || "保存失败");
    });
    onClick("#btn-narr-bible-versions", async () => {
      if (!state.narrProject) return;
      const host = $("#narr-bible-versions");
      const res = await api(`/api/narratives/${state.narrProject.id}/bible/versions`);
      if (!res || !res.ok) { toast((res && res.error) || "无法读取版本"); return; }
      host.hidden = false;
      host.innerHTML = (res.data || []).map(item =>
        `<button class="btn btn-sm btn-ghost narr-bible-ver" data-version="${item.version}" type="button">v${item.version}</button>`
      ).join("") || `<p class="meta">暂无版本</p>`;
      $$(".narr-bible-ver").forEach(btn => btn.addEventListener("click", async () => {
        const loaded = await api(`/api/narratives/${state.narrProject.id}/bible?version=${btn.dataset.version}`);
        if (loaded && loaded.ok) { state.narrBible = loaded.data; renderNarrativeBible(); }
      }));
    });
    onClick("#btn-narr-char-add", () => openNarrativeAddCharacterModal());
    onClick("#narr-char-cancel", () => { const dlg = $("#dlg-narr-char"); if (dlg) dlg.close(); });
    $$("input[name='narr-char-mode']").forEach(radio => radio.addEventListener("change", syncNarrativeCharMode));
    const search = $("#narr-char-search");
    if (search) search.addEventListener("input", () => renderNarrativePersonaPicker(search.value));
    const charForm = $("#form-narr-char");
    if (charForm) charForm.addEventListener("submit", submitNarrativeCharacter);
    onClick("#btn-narr-char-create-missing", async () => {
      if (!state.narrProject) return;
      const res = await api(`/api/narratives/${state.narrProject.id}/characters/create-missing`, { method: "POST" });
      if (res && res.ok) { toast("已创建轻量虚构 Persona"); await loadNarrativeCast(); renderNarrativeHeader(); }
      else toast((res && res.error) || "创建失败");
    });
    onClick("#btn-narr-outline", () => narrEpisodeAction("outline"));
    syncNarrAdvancedUi();
  }

  // ─── Narrative Director Agent Panel ───────────────────────────────────
  const DIRECTOR_ACTION_LABELS = {
    "zh-CN": {
      get_project: "读取作品", get_story_bible: "读取故事圣经", get_story_bible_versions: "读取圣经版本",
      get_characters: "读取角色", get_character_bindings: "读取角色绑定", get_episode_plan: "读取剧集计划",
      get_episode_plans: "读取全剧计划", get_episode_versions: "读取剧本版本", get_episode_version: "读取剧本版本",
      get_episode_audits: "读取审核记录", get_forecasts: "读取 Forecast", get_selected_forecast: "读取选定方向",
      get_scenes: "读取场景", get_rehearsal_results: "读取排练结果", get_knowledge_matrix: "读取信息差",
      get_audience_knowledge: "读取观众认知", get_clues: "读取伏笔", get_plot_threads: "读取情节线",
      get_character_arcs: "读取角色弧", get_canon: "读取正史", get_pipeline_state: "读取流水线状态",
      patch_episode_plan: "修改剧集计划", generate_forecast_directions: "生成候选方向",
      run_forecast: "推演 Forecast（NON-CANON）", select_forecast_direction: "选择创作方向",
      run_persona_rehearsal: "Persona 排练（NON-CANON）", generate_episode_draft: "生成剧本",
      revise_episode_draft: "修订剧本", audit_episode: "连续性审核",
      patch_story_bible: "修改 Story Bible（高影响）", change_project_settings: "修改作品设定（高影响）",
    },
    en: {
      get_project: "Read project", get_story_bible: "Read story bible", get_story_bible_versions: "Read bible versions",
      get_characters: "Read characters", get_character_bindings: "Read bindings", get_episode_plan: "Read episode plan",
      get_episode_plans: "Read episode plans", get_episode_versions: "Read draft versions", get_episode_version: "Read draft version",
      get_episode_audits: "Read audits", get_forecasts: "Read forecasts", get_selected_forecast: "Read selected direction",
      get_scenes: "Read scenes", get_rehearsal_results: "Read rehearsals", get_knowledge_matrix: "Read knowledge matrix",
      get_audience_knowledge: "Read audience knowledge", get_clues: "Read clues", get_plot_threads: "Read plot threads",
      get_character_arcs: "Read arcs", get_canon: "Read canon", get_pipeline_state: "Read pipeline state",
      patch_episode_plan: "Patch episode plan", generate_forecast_directions: "Generate forecast directions",
      run_forecast: "Run forecast (NON-CANON)", select_forecast_direction: "Select direction",
      run_persona_rehearsal: "Persona rehearsal (NON-CANON)", generate_episode_draft: "Generate draft",
      revise_episode_draft: "Revise draft", audit_episode: "Continuity audit",
      patch_story_bible: "Patch story bible (high impact)", change_project_settings: "Change project settings (high impact)",
    },
  };
  const DIRECTOR_STATUS_TEXT = {
    active: "directorActive", running: "directorRunning", paused: "directorPaused",
    waiting_for_user: "directorWaitingUser", waiting_for_canon_approval: "directorWaitingCanon",
    needs_human_guidance: "directorNeedsGuidance", completed: "directorCompleted",
    failed: "directorFailed", cancelled: "directorCancelled",
  };

  function directorActionLabel(action) {
    const dict = DIRECTOR_ACTION_LABELS[state.lang] || DIRECTOR_ACTION_LABELS.en;
    return dict[action] || action;
  }

  function directorSessionUrl() {
    const pid = state.narrProject && state.narrProject.id;
    return pid ? `/api/narratives/${pid}/director/sessions` : null;
  }

  function stopDirectorPolling() {
    if (state.director.poll) { clearInterval(state.director.poll); state.director.poll = null; }
  }

  async function ensureDirectorSession() {
    const base = directorSessionUrl();
    if (!base) return null;
    const ep = state.narrEpNumber;
    const list = await api(base);
    if (list && list.ok) {
      const found = (list.data || []).find(s => s.episode_number === ep && s.status !== "cancelled" && s.status !== "failed");
      if (found) { state.director.mode = found.mode; return found; }
    }
    const created = await api(base, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_number: ep, mode: state.director.mode }),
    });
    return (created && created.ok) ? created.data : null;
  }

  function renderDirectorPanel(snapshot) {
    const panel = $("#narr-director-panel");
    if (!panel || !snapshot || !snapshot.session) return;
    const s = snapshot.session;
    state.director.sessionId = s.id;
    $$("#narr-director-panel [data-dir-mode]").forEach(b => b.classList.toggle("is-active", b.dataset.dirMode === s.mode));

    const banner = $("#narr-dir-banner");
    if (banner) {
      if (s.pending_action && s.pending_action.action) {
        banner.hidden = false;
        banner.className = "narr-dir-banner warn";
        banner.textContent = `${t("directorConfirmPending")}（${directorActionLabel(s.pending_action.action)}）`;
      } else if (DIRECTOR_STATUS_TEXT[s.status]) {
        const cls = s.status === "waiting_for_canon_approval" ? "ok"
          : (s.status === "failed" || s.status === "needs_human_guidance") ? "danger" : "";
        banner.hidden = false;
        banner.className = `narr-dir-banner ${cls}`.trim();
        banner.textContent = t(DIRECTOR_STATUS_TEXT[s.status]);
      } else {
        banner.hidden = true;
      }
    }

    const stopBtn = $("#btn-narr-dir-stop");
    if (stopBtn) stopBtn.hidden = s.status !== "running";

    const items = [
      ...(snapshot.messages || []).map(m => ({ kind: "msg", at: m.created_at, ...m })),
      ...(snapshot.actions || []).filter(a => a.action && a.status !== "pending").map(a => ({ kind: "action", at: a.started_at, ...a })),
    ].sort((x, y) => String(x.at).localeCompare(String(y.at)));

    const thread = $("#narr-dir-thread");
    if (!thread) return;
    if (!items.length) {
      thread.innerHTML = `<p class="meta">${t("directorEmptyHint")}</p>`;
      return;
    }
    thread.innerHTML = items.map(item => {
      if (item.kind === "msg") {
        if (item.role === "system") return `<div class="narr-dir-msg system">${esc(item.content)}</div>`;
        return `<div class="narr-dir-msg ${esc(item.role)}"><span>${esc(item.content)}</span></div>`;
      }
      const ok = item.status === "succeeded";
      const icon = ok ? "✓" : (item.status === "failed" ? "✗" : "…");
      const detail = Object.keys(item.result && item.result.artifacts || {}).length
        ? `<pre class="narr-tech-json">${esc(JSON.stringify(item.result, null, 2))}</pre>`
        : "";
      return `<details class="narr-dir-action ${ok ? "ok" : (item.status === "failed" || item.status === "rejected") ? "danger" : ""}">
        <summary>${icon} ${esc(directorActionLabel(item.action))}${item.result && item.result.summary ? ` · ${esc(String(item.result.summary).split("\n")[0]).slice(0, 80)}` : ""}</summary>
        ${item.result && item.result.summary ? `<p class="narr-dir-action-summary">${esc(item.result.summary)}</p>` : ""}
        <div class="narr-tech">${detail || `<p class="meta">${esc(item.status)}</p>`}</div>
      </details>`;
    }).join("");
    thread.scrollTop = thread.scrollHeight;
  }

  async function pollDirectorSession() {
    const base = directorSessionUrl();
    const sid = state.director.sessionId;
    if (!base || !sid) return;
    const res = await api(`${base}/${sid}`);
    if (!res || !res.ok) return;
    renderDirectorPanel(res.data);
    const status = res.data.session && res.data.session.status;
    if (status === "running") {
      if (!state.director.poll) state.director.poll = setInterval(pollDirectorSession, 1500);
      return;
    }
    stopDirectorPolling();
    // Auto-refresh the workbench once per newly succeeded action.
    const fresh = (res.data.actions || []).filter(a => a.status === "succeeded" && !state.director.seen.has(a.id));
    if (fresh.length) {
      fresh.forEach(a => state.director.seen.add(a.id));
      await refreshNarrativeAll();
    }
  }

  async function openDirectorPanel() {
    const panel = $("#narr-director-panel");
    if (!panel || !state.narrProject || state.director.loading) return;
    state.director.loading = true;
    panel.hidden = false;
    try {
      const assignment = ((state.narrProject || {}).runtime_assignment || {});
      bindSharedRuntimeSelector("dir", assignment.director || {});
      const session = await ensureDirectorSession();
      if (!session) { toast("无法创建 Director 会话"); return; }
      const res = await api(`${directorSessionUrl()}/${session.id}`);
      if (res && res.ok) renderDirectorPanel(res.data);
      if (session.status === "running") pollDirectorSession();
    } finally {
      state.director.loading = false;
    }
  }

  async function saveDirectorRuntime() {
    if (!state.narrProject) return;
    const cfg = readSharedRuntime("dir");
    if (!cfg) { toast("请选择 READY / Connected 的导演 Agent"); return; }
    const res = await api(`/api/narratives/${state.narrProject.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        runtime_assignment: {
          ...((state.narrProject || {}).runtime_assignment || {}),
          director: cfg,
        },
      }),
    });
    if (res && res.ok) {
      state.narrProject = res.data;
      toast(t("directorRuntimeSaved"));
    } else {
      toast((res && res.error) || "保存失败");
    }
  }

  function closeDirectorPanel() {
    const panel = $("#narr-director-panel");
    if (panel) panel.hidden = true;
    stopDirectorPolling();
  }

  async function sendDirectorMessage() {
    const input = $("#narr-dir-input");
    const content = ((input || {}).value || "").trim();
    const sid = state.director.sessionId;
    if (!content || !sid) return;
    const sendBtn = $("#btn-narr-dir-send");
    if (sendBtn) sendBtn.disabled = true;
    try {
      const res = await api(`${directorSessionUrl()}/${sid}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, runtime: readSharedRuntime("dir") || undefined }),
      });
      if (res && res.ok) {
        input.value = "";
        renderDirectorPanel(res.data);
        stopDirectorPolling();
        state.director.poll = setInterval(pollDirectorSession, 1500);
      } else {
        // Keep the draft: a failed send must never swallow the author's text.
        toast((res && res.error) || "发送失败");
      }
    } finally {
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  async function setDirectorMode(mode) {
    state.director.mode = mode;
    const sid = state.director.sessionId;
    if (!sid) return;
    await api(`${directorSessionUrl()}/${sid}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    pollDirectorSession();
  }

  async function cancelDirectorLoop() {
    const sid = state.director.sessionId;
    if (!sid) return;
    await api(`${directorSessionUrl()}/${sid}/cancel`, { method: "POST" });
    stopDirectorPolling();
    pollDirectorSession();
  }

  // ─── Narrative Shooting Agent Panel ─────────────────────────────────
  const SHOOTING_ACTION_LABELS = {
    "zh-CN": {
      get_production_package: "读取制作母版", get_shot_list: "读取分镜表",
      get_character_visual_bible: "读取角色视觉圣经", get_location_visual_bible: "读取场景视觉圣经",
      get_prop_visual_bible: "读取道具视觉圣经", get_dialogue_track: "读取台词轨",
      get_subtitle_track: "读取字幕轨", get_sfx_plan: "读取音效计划", get_bgm_direction: "读取 BGM 指导",
      get_continuity_notes: "读取连续性笔记", get_video_model_profiles: "读取视频模型 Profile",
      get_model_prompt_packages: "读取视频生成方案", get_production_assets: "读取参考素材",
      get_canon_episode: "读取正剧本集",
      create_clip_plan: "创建 Clip Plan", revise_clip_plan: "修订 Clip Plan",
      create_model_prompt_package: "编译模型 Prompt 包", revise_generation_clip_prompt: "修订 Clip Prompt",
      set_clip_reference_assets: "绑定 Clip 参考素材", set_generation_mode: "设置生成模式",
      set_target_video_model: "设置目标视频模型", set_continuity_strategy: "设置连续性策略",
    },
    en: {
      get_production_package: "Read production master", get_shot_list: "Read shot list",
      get_character_visual_bible: "Read character visual bible", get_location_visual_bible: "Read location visual bible",
      get_prop_visual_bible: "Read prop visual bible", get_dialogue_track: "Read dialogue track",
      get_subtitle_track: "Read subtitle track", get_sfx_plan: "Read SFX plan", get_bgm_direction: "Read BGM direction",
      get_continuity_notes: "Read continuity notes", get_video_model_profiles: "Read video model profiles",
      get_model_prompt_packages: "Read prompt packages", get_production_assets: "Read reference assets",
      get_canon_episode: "Read canon episode",
      create_clip_plan: "Create clip plan", revise_clip_plan: "Revise clip plan",
      create_model_prompt_package: "Compile model prompt package", revise_generation_clip_prompt: "Revise clip prompt",
      set_clip_reference_assets: "Bind clip reference assets", set_generation_mode: "Set generation mode",
      set_target_video_model: "Set target video model", set_continuity_strategy: "Set continuity strategy",
    },
  };
  const SHOOTING_STATUS_TEXT = {
    active: "shootActive", running: "shootRunning", paused: "shootPaused",
    waiting_for_user: "shootWaitingUser", waiting_for_canon_approval: "shootWaitingCanon",
    needs_human_guidance: "shootNeedsGuidance", completed: "shootCompleted",
    failed: "shootFailed", cancelled: "shootCancelled",
  };

  function shootingActionLabel(action) {
    const dict = SHOOTING_ACTION_LABELS[state.lang] || SHOOTING_ACTION_LABELS.en;
    return dict[action] || action;
  }

  function shootingSessionUrl() {
    const pid = state.narrProject && state.narrProject.id;
    return pid ? `/api/narratives/${pid}/shooting/sessions` : null;
  }

  function stopShootingPolling() {
    if (state.shooting.poll) { clearInterval(state.shooting.poll); state.shooting.poll = null; }
  }

  async function ensureShootingSession() {
    const base = shootingSessionUrl();
    if (!base) return null;
    const ep = state.narrEpNumber;
    const list = await api(base);
    if (list && list.ok) {
      const found = (list.data || []).find(s => s.episode_number === ep && s.status !== "cancelled" && s.status !== "failed");
      if (found) { state.shooting.mode = found.mode; return found; }
    }
    const created = await api(base, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_number: ep, mode: state.shooting.mode }),
    });
    return (created && created.ok) ? created.data : null;
  }

  function populateShootingTargetModel() {
    const sel = $("#shoot-target-model");
    if (!sel) return;
    const pid = state.narrProject && state.narrProject.id;
    let stored = "";
    if (pid) { try { stored = localStorage.getItem(`pc-shoot-model-${pid}`) || ""; } catch (_) {} }
    const profiles = state.narrVideoProfiles || [];
    sel.innerHTML = profiles.map(p => {
      const draft = p.verification_status && p.verification_status !== "verified" ? `（${p.verification_status}）` : "";
      return `<option value="${esc(p.id)}">${esc(p.display_name || p.id)}${esc(draft)}</option>`;
    }).join("") || `<option value="">—</option>`;
    sel.value = profiles.some(p => p.id === stored) ? stored : (profiles[0] ? profiles[0].id : "");
  }

  function renderShootingPanel(snapshot) {
    const panel = $("#narr-shooting-panel");
    if (!panel || !snapshot || !snapshot.session) return;
    const s = snapshot.session;
    state.shooting.sessionId = s.id;
    $$("#narr-shooting-panel [data-shoot-mode]").forEach(b => b.classList.toggle("is-active", b.dataset.shootMode === s.mode));

    const banner = $("#narr-shoot-banner");
    if (banner) {
      if (s.pending_action && s.pending_action.action) {
        banner.hidden = false;
        banner.className = "narr-dir-banner warn";
        banner.textContent = `${t("directorConfirmPending")}（${shootingActionLabel(s.pending_action.action)}）`;
      } else if (SHOOTING_STATUS_TEXT[s.status]) {
        const cls = s.status === "waiting_for_canon_approval" ? "ok"
          : (s.status === "failed" || s.status === "needs_human_guidance") ? "danger" : "";
        banner.hidden = false;
        banner.className = `narr-dir-banner ${cls}`.trim();
        banner.textContent = t(SHOOTING_STATUS_TEXT[s.status]);
      } else {
        banner.hidden = true;
      }
    }

    const stopBtn = $("#btn-narr-shoot-stop");
    if (stopBtn) stopBtn.hidden = s.status !== "running";

    const items = [
      ...(snapshot.messages || []).map(m => ({ kind: "msg", at: m.created_at, ...m })),
      ...(snapshot.actions || []).filter(a => a.action && a.status !== "pending").map(a => ({ kind: "action", at: a.started_at, ...a })),
    ].sort((x, y) => String(x.at).localeCompare(String(y.at)));

    const thread = $("#narr-shoot-thread");
    if (!thread) return;
    if (!items.length) {
      thread.innerHTML = `<p class="meta">${t("shootEmptyHint")}</p>`;
      return;
    }
    thread.innerHTML = items.map(item => {
      if (item.kind === "msg") {
        if (item.role === "system") return `<div class="narr-dir-msg system">${esc(item.content)}</div>`;
        return `<div class="narr-dir-msg ${esc(item.role)}"><span>${esc(item.content)}</span></div>`;
      }
      const ok = item.status === "succeeded";
      const icon = ok ? "✓" : (item.status === "failed" ? "✗" : "…");
      const detail = Object.keys(item.result && item.result.artifacts || {}).length
        ? `<pre class="narr-tech-json">${esc(JSON.stringify(item.result, null, 2))}</pre>`
        : "";
      return `<details class="narr-dir-action ${ok ? "ok" : (item.status === "failed" || item.status === "rejected") ? "danger" : ""}">
        <summary>${icon} ${esc(shootingActionLabel(item.action))}${item.result && item.result.summary ? ` · ${esc(String(item.result.summary).split("\n")[0]).slice(0, 80)}` : ""}</summary>
        ${item.result && item.result.summary ? `<p class="narr-dir-action-summary">${esc(item.result.summary)}</p>` : ""}
        <div class="narr-tech">${detail || `<p class="meta">${esc(item.status)}</p>`}</div>
      </details>`;
    }).join("");
    thread.scrollTop = thread.scrollHeight;
  }

  async function pollShootingSession() {
    const base = shootingSessionUrl();
    const sid = state.shooting.sessionId;
    if (!base || !sid) return;
    const res = await api(`${base}/${sid}`);
    if (!res || !res.ok) return;
    renderShootingPanel(res.data);
    const status = res.data.session && res.data.session.status;
    if (status === "running") {
      if (!state.shooting.poll) state.shooting.poll = setInterval(pollShootingSession, 1500);
      return;
    }
    stopShootingPolling();
    // Auto-refresh the workbench once per newly succeeded action.
    const fresh = (res.data.actions || []).filter(a => a.status === "succeeded" && !state.shooting.seen.has(a.id));
    if (fresh.length) {
      fresh.forEach(a => state.shooting.seen.add(a.id));
      await refreshNarrativeAll();
    }
  }

  async function openShootingPanel() {
    const panel = $("#narr-shooting-panel");
    if (!panel || !state.narrProject || state.shooting.loading) return;
    state.shooting.loading = true;
    panel.hidden = false;
    try {
      const assignment = ((state.narrProject || {}).runtime_assignment || {});
      bindSharedRuntimeSelector("shoot", assignment.shooting_agent || {});
      await ensureNarrativeVideoProfiles();
      populateShootingTargetModel();
      const session = await ensureShootingSession();
      if (!session) { toast("无法创建拍摄会话"); return; }
      const res = await api(`${shootingSessionUrl()}/${session.id}`);
      if (res && res.ok) renderShootingPanel(res.data);
      if (session.status === "running") pollShootingSession();
    } finally {
      state.shooting.loading = false;
    }
  }

  async function openShootingPanelWithInstruction(instruction) {
    await openShootingPanel();
    const input = $("#narr-shoot-input");
    if (input && instruction) { input.value = instruction; input.focus(); }
  }

  async function saveShootingRuntime() {
    if (!state.narrProject) return;
    const cfg = readSharedRuntime("shoot");
    if (!cfg) { toast("请选择 READY / Connected 的拍摄 Agent"); return; }
    const res = await api(`/api/narratives/${state.narrProject.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        runtime_assignment: {
          ...((state.narrProject || {}).runtime_assignment || {}),
          shooting_agent: cfg,
        },
      }),
    });
    if (res && res.ok) {
      state.narrProject = res.data;
      toast(t("shootRuntimeSaved"));
    } else {
      toast((res && res.error) || "保存失败");
    }
  }

  function closeShootingPanel() {
    const panel = $("#narr-shooting-panel");
    if (panel) panel.hidden = true;
    stopShootingPolling();
  }

  async function sendShootingMessage() {
    const input = $("#narr-shoot-input");
    const content = ((input || {}).value || "").trim();
    const sid = state.shooting.sessionId;
    if (!content || !sid) return;
    const sendBtn = $("#btn-narr-shoot-send");
    if (sendBtn) sendBtn.disabled = true;
    try {
      const res = await api(`${shootingSessionUrl()}/${sid}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, runtime: readSharedRuntime("shoot") || undefined }),
      });
      if (res && res.ok) {
        input.value = "";
        renderShootingPanel(res.data);
        stopShootingPolling();
        state.shooting.poll = setInterval(pollShootingSession, 1500);
      } else {
        // Keep the draft: a failed send must never swallow the author's text.
        toast((res && res.error) || "发送失败");
      }
    } finally {
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  async function setShootingMode(mode) {
    state.shooting.mode = mode;
    const sid = state.shooting.sessionId;
    if (!sid) return;
    await api(`${shootingSessionUrl()}/${sid}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    pollShootingSession();
  }

  async function cancelShootingLoop() {
    const sid = state.shooting.sessionId;
    if (!sid) return;
    await api(`${shootingSessionUrl()}/${sid}/cancel`, { method: "POST" });
    stopShootingPolling();
    pollShootingSession();
  }

  // ─── Event Listeners ─────────────────────────────────────────────────────
  async function boot() {
    try {
    // Nav bar tab navigation
    $$(".nav-links button").forEach(b => b.addEventListener("click", () => showView(b.dataset.nav)));
    // Language switcher
    $$(".lang-switch button").forEach(b => b.addEventListener("click", () => {
      state.lang = b.dataset.lang;
      applyLang();
    }));

    // Narrative Director Agent panel
    onClick("#btn-narr-director", openDirectorPanel);
    onClick("#btn-narr-director-close", closeDirectorPanel);
    onClick("#btn-narr-dir-send", sendDirectorMessage);
    onClick("#btn-narr-dir-stop", cancelDirectorLoop);
    onClick("#btn-narr-dir-save-runtime", saveDirectorRuntime);
    $$("#narr-director-panel [data-dir-mode]").forEach(b => b.addEventListener("click", () => setDirectorMode(b.dataset.dirMode)));
    const dirInput = $("#narr-dir-input");
    if (dirInput) dirInput.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); sendDirectorMessage(); }
    });

    // Narrative Shooting Agent panel
    onClick("#btn-narr-shooting", openShootingPanel);
    onClick("#btn-narr-shoot-close", closeShootingPanel);
    onClick("#btn-narr-shoot-send", sendShootingMessage);
    onClick("#btn-narr-shoot-stop", cancelShootingLoop);
    onClick("#btn-narr-shoot-save-runtime", saveShootingRuntime);
    $$("#narr-shooting-panel [data-shoot-mode]").forEach(b => b.addEventListener("click", () => setShootingMode(b.dataset.shootMode)));
    const shootInput = $("#narr-shoot-input");
    if (shootInput) shootInput.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); sendShootingMessage(); }
    });
    const shootTargetModel = $("#shoot-target-model");
    if (shootTargetModel) shootTargetModel.addEventListener("change", () => {
      const pid = state.narrProject && state.narrProject.id;
      if (pid) { try { localStorage.setItem(`pc-shoot-model-${pid}`, shootTargetModel.value); } catch (_) {} }
    });

    initNarrativeUI();
    // Room Subsystem Listeners
    $$("#room-filter button").forEach(b => b.addEventListener("click", () => {
      state.roomFilter = b.dataset.filter;
      $$("#room-filter button").forEach(x => x.classList.toggle("is-active", x === b));
      renderRooms();
    }));
    onClick("#btn-new-room", openRoomLobby);
    onClick("#btn-back-rooms", () => { showRoomSub("list"); renderRooms(); });
    onClick("#btn-leave", () => {
      state.liveWatchToken += 1;
      disconnectRoomWs();
      showRoomSub("list");
      renderRooms();
    });
    onClick("#btn-topic-toggle", () => {
      state.topicExpanded = !state.topicExpanded;
      syncTopicClamp();
    });
    $("#btn-add-slot").onclick = () => {
      if (!state.personas.length) {
        toast("当前没有可用人物");
        return;
      }
      const nextP = state.personas[state.lobbySlots.length % state.personas.length];
      const previous = state.lobbySlots[0];
      const source = previous ? (previous.runtime_source || "local_cli") : (getSelectableLocalAgents().length ? "local_cli" : "api");
      const available = getSelectableAgentsForSource(source);
      const selected = previous
        ? available.find(agent => agent.id === previous.runtime_selection)
        : available[0];
      const addRole = defaultRole(selectedProtocol(), state.lobbySlots.length);
      state.lobbySlots.push({
        persona_id: nextP.id,
        runtime_source: source,
        runtime_selection: selected ? selected.id : "",
        model_selection: (selected && selected.models && selected.models[0]) ? selected.models[0].id : "default",
        reasoning_selection: "none",
        role: addRole,
        specialties: personaSpecialties(nextP),
        authority: authorityForRole(addRole),
        tool_permissions: []
      });
      renderSlots();
      renderBindingPreview();
    };
    $("#lobby-protocol").addEventListener("change", () => {
      const protocol = $("#lobby-protocol").value;
      state.lobbySlots.forEach((slot, index) => {
        slot.role = defaultRole(protocol, index);
        slot.authority = authorityForRole(slot.role);
      });
      renderProtocolSettings();
      applyLobbyMode(state.lobbyAdvanced ? "advanced" : "simple");
      renderBindingPreview();
    });
    document.querySelectorAll("#lobby-mode-toggle [data-lobby-mode]").forEach(btn => {
      btn.addEventListener("click", () => applyLobbyMode(btn.dataset.lobbyMode));
    });
    $("#lobby-template").addEventListener("change", (event) => applyRoomTemplate(event.target.value));
    $("#btn-randomize").onclick = () => {
      state.lobbySlots.forEach(s => { s.runtime_selection = "random"; s.model_selection = "random"; });
      renderSlots();
      renderBindingPreview();
    };
    $("#btn-preview").onclick = () => {
      $("#preview-box").innerHTML = `<p class="meta">${t("resolving")}</p>`;
      setTimeout(renderBindingPreview, 240);
    };
    $("#btn-start").onclick = startRoomFromLobby;

    $("#btn-next").onclick = () => stepTurn();
    $("#btn-retry-room").onclick = retryRoomInitialization;
    $("#btn-manual").onclick = () => {
      const spk = $("#manual-speaker").value;
      if (spk) stepTurn(spk);
    };
    $("#btn-pause").onclick = async () => {
      if (!state.currentRoom) return;
      confirmDlg(t("pauseRoom"), t("pause"), async () => {
        await api(`/api/rooms/${state.currentRoom.id}/pause`, { method: "POST" });
        state.currentRoom.status = "paused";
        $("#btn-pause").hidden = true;
        $("#btn-resume").hidden = false;
        $("#btn-next").disabled = true;
        $("#live-status").textContent = statusLabel("paused");
      });
    };
    $("#btn-resume").onclick = async () => {
      if (!state.currentRoom) return;
      if (!ROOM_UI_STATE.canResumeRoom(state.currentRoom)) return;
      const res = await api(`/api/rooms/${state.currentRoom.id}/resume`, { method: "POST" });
      if (res && res.ok && res.data) applyLiveRoom(res.data);
    };
    $("#btn-cancel-turn").onclick = async () => {
      if (!state.currentRoom) return;
      if (!ROOM_UI_STATE.canCancelTurn(state.currentRoom)) return;
      const endpoint = (state.currentRoom.protocol || "free_discussion") === "free_discussion" ? "cancel-turn" : "cancel";
      const res = await api(`/api/rooms/${state.currentRoom.id}/${endpoint}`, { method: "POST" });
      if (res && res.ok && res.data && res.data.cancelled) {
        toast(t("toastTurnCancelled"));
        banner(false);
        state.isTurnBusy = false;
      }
    };
    $("#btn-finalize-room").onclick = async () => {
      if (!state.currentRoom) return;
      banner(true, "主持人正在整理当前结果…");
      const res = await api(`/api/rooms/${state.currentRoom.id}/finalize`, { method: "POST" });
      if (res && res.ok) applyLiveRoom(res.data);
      else toast((res && res.error) || "立即总结失败");
    };
    const upgradeBtn = $("#protocol-upgrade-button");
    if (upgradeBtn) {
      upgradeBtn.onclick = async () => {
        const room = state.currentRoom;
        if (!room || !ROOM_UI_STATE.shouldSuggestProtocolUpgrade(room)) return;
        const active = (room.participants || []).filter(p => p && p.enabled !== false);
        const nameOf = (p) => p.display_name || p.persona_id || p.participant_id;
        const plan = ROOM_UI_STATE.protocolUpgradeRoleMapping(room);
        const roleMapping = { ...plan.mapping };
        let hostId = plan.hostId;
        // A consultation panel needs exactly one host.  When the room has
        // none, ask the user to pick it from the enabled quorum by number
        // before showing the final preview; cancelling aborts the upgrade.
        if (plan.needsHostSelection) {
          const list = plan.candidates
            .map((pid, index) => {
              const p = active.find(item => item.participant_id === pid);
              return `${index + 1}. ${p ? nameOf(p) : pid}`;
            })
            .join("\n");
          for (;;) {
            const answer = window.prompt(
              `升级为「专家会诊」需要指定一位主持人：\n\n${list}\n\n请输入主持人编号（1-${plan.candidates.length}）：`
            );
            if (answer === null) return; // cancelled: no conversion
            const picked = Number.parseInt(String(answer).trim(), 10);
            if (Number.isInteger(picked) && picked >= 1 && picked <= plan.candidates.length) {
              hostId = plan.candidates[picked - 1];
              break;
            }
            window.alert(`请输入 1 到 ${plan.candidates.length} 之间的编号。`);
          }
          roleMapping[hostId] = "host";
        }
        const previewLines = active.map(p => {
          if (p.participant_id === hostId) {
            const suffix = plan.needsHostSelection ? "（新指定）" : "（保持不变）";
            return `· ${nameOf(p)}：主持人${suffix}`;
          }
          return `· ${nameOf(p)}：${p.role} → 专家`;
        });
        const ok = window.confirm(
          `将把这个房间升级为「专家会诊」：\n\n${previewLines.join("\n")}\n\n确认升级？`
        );
        if (!ok) return;
        banner(true, "正在升级为专家会诊…");
        const res = await api(`/api/rooms/${room.id}/protocol/convert`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_protocol: "expert_consultation",
            role_mapping: roleMapping,
          }),
        });
        if (res && res.ok) {
          toast("已升级为专家会诊");
          banner(false);
          // Re-pull the converted room so live pills, protocol panel and
          // binding views all refresh from the authoritative snapshot.
          const latest = await api(`/api/rooms/${room.id}`);
          if (latest && latest.ok && latest.data) applyLiveRoom(latest.data);
          else if (res.data) applyLiveRoom(res.data);
        } else {
          banner(false);
          toast(`升级失败：${(res && res.error) || "未知错误"}`);
        }
      };
    }
    onClick("#btn-stop", async () => {
      if (!state.currentRoom) return;
      confirmDlg(t("stopRoom"), t("stop"), async () => {
        await api(`/api/rooms/${state.currentRoom.id}/stop`, { method: "POST" });
        state.currentRoom.status = "completed";
        $("#btn-next").disabled = true;
        $("#btn-pause").hidden = true;
        $("#btn-resume").hidden = true;
        $("#live-status").textContent = statusLabel("completed");
      });
    });
    onClick("#empty-new-room", (e) => {
      e.preventDefault();
      openRoomLobby();
    });
    onClick("#empty-new-world", (e) => {
      e.preventDefault();
      const btn = $("#btn-new-world");
      if (btn) btn.click();
    });
    onClick("#empty-add-api", (e) => {
      e.preventDefault();
      const dlg = $("#dlg-api");
      if (dlg) dlg.showModal();
    });

    const injectInput = $("#inject");
    if (injectInput) {
      injectInput.addEventListener("input", () => {
        $("#btn-inject").disabled = !injectInput.value.trim() || !state.currentRoom || !["ready", "discussing", "paused"].includes(state.currentRoom.status);
      });
      injectInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") injectUserMessage();
      });
      $("#btn-inject").onclick = injectUserMessage;
    }

    const dismissRoomErrBtn = $("#btn-dismiss-room-error");
    if (dismissRoomErrBtn) {
      dismissRoomErrBtn.onclick = () => {
        const errBox = $("#room-init-error");
        if (errBox) errBox.style.display = "none";
      };
    }

    const clearChatBtn = $("#btn-clear-chat");
    if (clearChatBtn) {
      clearChatBtn.onclick = clearCurrentConversation;
    }

    // Room Inspector Tab Switcher
    $$(".inspector-tabs button[data-ipanel]").forEach(b => b.addEventListener("click", () => {
      $$(".inspector-tabs button[data-ipanel]").forEach(x => x.classList.toggle("is-active", x === b));
      ["state", "recall", "bind"].forEach(k => {
        const el = $("#insp-" + k);
        if (el) el.hidden = b.dataset.ipanel !== k;
      });
    }));

    // Parallel World Listeners
    $("#btn-new-world").onclick = () => {
      initWorldCreateForm();
      if (!state.agents.length) {
        hydrateRuntimeSelectors(() => {
          setupRuntimeDropdowns("wb", "local_cli");
          setupRuntimeDropdowns("actor-default", "local_cli");
          setupRuntimeDropdowns("persona-creator", "local_cli");
        });
      }
    };
    $("#btn-cancel-world").onclick = () => showWorldSub("list");
    $("#btn-back-worlds").onclick = () => showWorldSub("list");
    $("#btn-leave-world").onclick = () => showWorldSub("list");

    const delWorldBtn = $("#btn-delete-world");
    if (delWorldBtn) {
      delWorldBtn.onclick = (e) => {
        e.stopPropagation();
        e.preventDefault();
        if (!state.currentWorld) return;
        const wid = state.currentWorld.id;
        confirmDlg(
          t("deleteWorldConfirm") || "确定要彻底删除该平行世界及其所有分支、时间线与快照吗？此操作无法撤销。",
          t("delete") || "删除",
          () => deleteWorldAction(wid),
          true
        );
      };
    }

    let previewSeedData = null;
    let previewActors = [];
    let previewPersonaMatches = [];
    let previewClassification = null;

    function updateWorldCreateRuntimes() {
      const wbSource = ($("input[name='wb-source']:checked") || {}).value || "local_cli";
      const actSource = ($("input[name='actor-default-source']:checked") || {}).value || "local_cli";
      const personaSource = ($("input[name='persona-creator-source']:checked") || {}).value || "local_cli";
      setupRuntimeDropdowns("wb", wbSource);
      setupRuntimeDropdowns("actor-default", actSource);
      setupRuntimeDropdowns("persona-creator", personaSource);
    }

    function initWorldCreateForm() {
      setupRuntimeDropdowns("wb", "local_cli");
      setupRuntimeDropdowns("actor-default", "local_cli");
      setupRuntimeDropdowns("persona-creator", "local_cli");

      const errBox = $("#world-create-error");
      if (errBox) errBox.style.display = "none";

      const previewPanel = $("#world-seed-preview-panel");
      if (previewPanel) previewPanel.style.display = "none";
      previewSeedData = null;
      previewActors = [];
      previewPersonaMatches = [];
      previewClassification = null;

      showWorldSub("create");
    }

    function setupRuntimeDropdowns(prefix, defaultSource = "local_cli") {
      const radios = $$(`input[name='${prefix}-source']`);
      const agentSel = $(`#${prefix}-agent`);
      const modelSel = $(`#${prefix}-model`);
      const reasoningSel = $(`#${prefix}-reasoning`);

      function populate(source) {
        const available = getSelectableAgentsForSource(source);
        if (!available.length) {
          const scanning = state.agentsLoading;
          const msg = scanning
            ? "正在扫描 Runtime…"
            : (source === "local_cli" ? "无可用本地 CLI Agent" : "无可用 API Provider");
          agentSel.innerHTML = `<option value="" disabled selected>${msg}</option>`;
          modelSel.innerHTML = `<option value="" disabled selected>${scanning ? "扫描结束后可选模型" : "无可用模型"}</option>`;
          reasoningSel.innerHTML = `<option value="none" selected disabled>${scanning ? "扫描结束后可选 Reasoning" : "使用 Agent 默认思考配置"}</option>`;
          reasoningSel.disabled = true;
        } else {
          agentSel.innerHTML = available.map(a => `<option value="${a.id}">${esc(a.name)} [READY]</option>`).join("");
          updateModels(available[0]);
        }
      }

      function updateModels(agent) {
        const models = getRuntimeModels(agent);
        const hasExplicitDefault = agent && agent.capabilities && (agent.capabilities.model_selection === "unsupported" || agent.capabilities.agent_default_model);
        if (!models.length) {
          if (hasExplicitDefault) {
            modelSel.innerHTML = `<option value="default" selected>Agent 默认模型</option>`;
          } else {
            modelSel.innerHTML = `<option value="" disabled selected>该 CLI 已连接，但未发现可用模型</option>`;
          }
        } else {
          modelSel.innerHTML = models.map(m => `<option value="${m.id}">${esc(runtimeModelOptionLabel(agent, m))}</option>`).join("");
        }
        updateReasoning(models[0]);
      }

      function updateReasoning(model) {
        const source = ($(`input[name='${prefix}-source']:checked`) || {}).value || defaultSource;
        const agent = getSelectableAgentsForSource(source)
          .find(item => item.id === agentSel.value);
        const capability = getReasoningCapability(model);
        const efforts = getRuntimeReasoningOptions(
          agent,
          model
        );
        if (efforts.length) {
          const defEff = (model && model.default_reasoning_effort)
            || capability.default_effort
            || efforts[0];
          reasoningSel.innerHTML = efforts.map(eff => `<option value="${esc(eff)}" ${eff === defEff ? "selected" : ""}>${esc(reasoningOptionLabel(eff))}</option>`).join("");
          reasoningSel.disabled = false;
        } else {
          reasoningSel.innerHTML = `<option value="" selected disabled>尚未检测到 Reasoning 能力</option>`;
          reasoningSel.disabled = true;
        }
        reasoningSel.title = runtimeReasoningNotice(agent, model, source);
      }

      radios.forEach(r => {
        r.checked = r.value === defaultSource;
        r.onchange = () => populate(r.value);
      });

      agentSel.onchange = () => {
        const source = ($(`input[name='${prefix}-source']:checked`) || {}).value || defaultSource;
        const available = getSelectableAgentsForSource(source);
        const ag = available.find(a => a.id === agentSel.value);
        if (ag) updateModels(ag);
      };

      modelSel.onchange = () => {
        const source = ($(`input[name='${prefix}-source']:checked`) || {}).value || defaultSource;
        const available = getSelectableAgentsForSource(source);
        const ag = available.find(a => a.id === agentSel.value);
        const m = ag && ag.models && ag.models.find(x => x.id === modelSel.value);
        updateReasoning(m);
      };

      populate(defaultSource);
    }

    const btnPreviewSeed = $("#btn-preview-seed");
    if (btnPreviewSeed) {
      btnPreviewSeed.onclick = async () => {
        const desc = $("#world-desc").value.trim();
        if (!desc) {
          toast("请先填写世界分歧描述");
          return;
        }
        btnPreviewSeed.disabled = true;
        btnPreviewSeed.textContent = "正在调用 LLM 解析...";

        const wbAgent = $("#wb-agent").value;
        const wbModel = $("#wb-model").value;
        const wbReasoning = $("#wb-reasoning").value;
        const wbSource = ($("input[name='wb-source']:checked") || {}).value || "local_cli";

        const payload = {
          description: desc,
          baseline: $("#world-baseline").value.trim() || "real_world",
          start_date: $("#world-start-date").value.trim() || "2011-10-05",
          simulation_end: $("#world-end-date").value.trim() || "2030",
          builder_runtime: {
            agent_id: wbAgent,
            model_id: wbModel,
            reasoning_effort: wbReasoning,
            runtime_source: wbSource
          }
        };

        try {
          const res = await api("/api/worlds/preview-seed", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
          });

          if (res && res.ok) {
            previewSeedData = res.data.seed;
            previewActors = res.data.actors || [];
            previewPersonaMatches = res.data.persona_matches || [];
            previewClassification = res.data.entity_classification || null;
            renderActorPreviewTable(previewActors, previewPersonaMatches, previewClassification);
            toast("大模型已成功解析 WorldSeed 角色阵容");
          } else {
            toast((res && res.error) || "解析失败");
          }
        } catch (err) {
          toast("解析请求失败: " + err);
        } finally {
          btnPreviewSeed.disabled = false;
          btnPreviewSeed.textContent = "智能解析 Actor 阵容";
        }
      };
    }

    function renderActorPreviewTable(actors, matches = [], classification = null) {
      const panel = $("#world-seed-preview-panel");
      const container = $("#world-actors-table-container");
      if (!panel || !container) return;
      panel.style.display = "block";

      const agentBox = $("#world-agent-classification-container");
      const nonAgentBox = $("#world-non-agent-entities-container");
      const classifiedAgents = (classification && classification.classified_agents) || [];
      const nonAgents = (classification && classification.non_agent_entities) || [];
      if (agentBox) {
        agentBox.innerHTML = `
          <h4 style="margin:0 0 6px;">可行动 Agent <span class="meta">${classifiedAgents.length} 个</span></h4>
          ${classifiedAgents.length ? `<div class="stack" style="gap:5px;">${classifiedAgents.map(item => `<div class="row-between" style="padding:7px 0;border-bottom:1px solid var(--border-soft);"><span><strong>${esc(item.name)}</strong> <span class="tag">${esc(item.subtype)}</span></span><span class="meta">${item.profile_match_status === "matched" ? `已匹配 · ${esc(item.profile_id || "")}` : `缺失档案 · ${Math.round(Number(item.confidence || 0) * 100)}%`}</span></div>`).join("")}</div>` : `<p class="meta">未识别到可行动 Agent。</p>`}`;
      }
      if (nonAgentBox) {
        nonAgentBox.innerHTML = `
          <h4 style="margin:0 0 6px;">世界实体 / 初始条件 <span class="meta">${nonAgents.length} 个</span></h4>
          ${nonAgents.length ? `<div class="row" style="gap:6px;flex-wrap:wrap;">${nonAgents.map(item => `<span class="tag">${esc(item.name)} · ${esc(item.subtype)} · 初始图谱</span>`).join("")}</div>` : `<p class="meta">未识别到非 Agent 世界实体。</p>`}`;
      }

      if (!actors.length) {
        container.innerHTML = `<p class="meta">未解析到动态角色。</p>`;
        return;
      }

      const personaOpts = state.personas.map(p => `<option value="${p.id}">${esc(p.display_name || p.id)}</option>`).join("");
      const classificationById = Object.fromEntries([...classifiedAgents, ...nonAgents].map(item => [item.id, item]));
      const matchByActor = Object.fromEntries((matches || []).map(item => [item.actor_id, item]));

      container.innerHTML = `
        <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px;">
          <thead>
            <tr style="border-bottom:1px solid var(--border);text-align:left;">
              <th style="padding:6px 8px;">Actor ID</th>
              <th style="padding:6px 8px;">角色名称</th>
              <th style="padding:6px 8px;">绑定 Agent Profile</th>
              <th style="padding:6px 8px;">运行时绑定</th>
            </tr>
          </thead>
          <tbody>
            ${actors.map((a) => `
              <tr style="border-bottom:1px solid var(--border-soft);">
                <td style="padding:6px 8px;font-family:var(--font-mono);">${esc(a.id)}</td>
                <td style="padding:6px 8px;font-weight:600;">${esc(a.name)} <span class="tag" style="margin-left:6px;">${esc((classificationById[a.id] && classificationById[a.id].subtype) || a.profile_type || "unknown")}</span></td>
                <td style="padding:6px 8px;">
                  ${(classificationById[a.id] && classificationById[a.id].agent_capable !== false) ? `<select class="input" style="padding:4px 8px;min-height:28px;font-size:12px;" data-actor-persona="${esc(a.id)}">
                    <option value="${esc((a.profile_id || (matchByActor[a.id] && matchByActor[a.id].persona_id) || ""))}">${a.profile_id || (matchByActor[a.id] && matchByActor[a.id].persona_id) ? "已匹配" : "(待确认)"}</option>
                    ${personaOpts}
                    ${(state.profiles || []).filter(profile => profile.profile_type !== "persona").map(profile => `<option value="${esc(profile.id)}">${esc(profile.display_name)} · ${esc(profileTypeLabel(profile.profile_type))}</option>`).join("")}
                  </select>` : `<span class="meta">非 Agent · 不创建档案</span>`}
                </td>
                <td style="padding:6px 8px;">
                  ${classificationById[a.id] && classificationById[a.id].agent_capable === false
                    ? `<span class="meta">不适用 · 初始世界实体</span>`
                    : `<label style="font-size:12px;display:inline-flex;align-items:center;gap:4px;">
                        <input type="checkbox" checked data-actor-inherit="${a.id}" /> 继承默认运行时
                      </label>
                      ${((classificationById[a.id] && ["missing", "ambiguous"].includes(classificationById[a.id].profile_match_status)) || (matchByActor[a.id] && ["MISSING", "AMBIGUOUS"].includes(matchByActor[a.id].status)))
                        ? `<label style="font-size:12px;display:inline-flex;align-items:center;gap:4px;margin-left:8px;">
                            <input type="checkbox" data-actor-generated="${a.id}" /> 使用 Generated Actor
                          </label>`
                        : ""}`}
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    }

    function collectWorldPersonaBindings() {
      const bindings = {};
      $$(`[data-actor-persona]`).forEach(select => {
        const value = select.value;
        if (value) bindings[select.getAttribute("data-actor-persona")] = value;
      });
      return bindings;
    }

    function collectGeneratedWorldActors() {
      return $$(`[data-actor-generated]:checked`).map(input => input.getAttribute("data-actor-generated"));
    }

    async function completeMissingWorldActors(details) {
      const actors = details.raw_actors || previewActors || [];
      const classification = details.classification || previewClassification || {};
      const classifiedMissing = classification.missing_profiles || [];
      const matches = details.matches || previewPersonaMatches || [];
      if (actors.length && !previewActors.length) {
        previewActors = actors;
        previewPersonaMatches = matches;
        previewClassification = classification;
        renderActorPreviewTable(actors, matches, classification);
      }
      const generatedIds = new Set(collectGeneratedWorldActors());
      const missing = classifiedMissing.length
        ? classifiedMissing.filter(item => item.profile_match_status === "missing" && !generatedIds.has(String(item.id)))
        : (details.missing_personas || matches).filter(item => ["MISSING", "AMBIGUOUS"].includes(item.status) && !generatedIds.has(String(item.actor_id)));
      if (!missing.length) {
        if (classifiedMissing.some(item => item.profile_match_status === "ambiguous")) {
          toast("存在低置信度模糊匹配，请在 Agent Profile 表格中手动选择档案后再继续。");
          return null;
        }
        return {};
      }
      const confirmed = window.confirm(`发现 ${missing.length} 个缺失 Agent 档案：\n${missing.map(item => item.name || item.display_name).join("、")}\n\n是否开始 Actor Completion Engine？`);
      if (!confirmed) return null;
      const runtime = {
        runtime_source: ($("input[name='persona-creator-source']:checked") || {}).value || "local_cli",
        agent_id: $("#persona-creator-agent").value,
        model_id: $("#persona-creator-model").value,
        reasoning_effort: $("#persona-creator-reasoning").value || null
      };
      // Legacy clients may still use /api/worlds/persona-completion/confirm;
      // the server keeps that alias while new UI uses the typed Actor endpoint.
      const completion = await api("/api/worlds/actor-completion/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entities: actors,
          selected_entity_ids: missing.map(item => item.id || item.actor_id),
          runtime,
          research_policy: { profile: $("#persona-creator-policy").value || "deep" },
          remote_material_consent: false
        })
      });
      if (!completion || !completion.ok) throw new Error((completion && completion.error) || "Actor Completion 启动失败");
      const jobs = completion.data.jobs || [];
      if (!jobs.length) throw new Error(completion.data.status === "waiting_for_materials" ? "缺失 Agent 需要本地资料或引导访谈。" : "没有创建可运行的补全任务。");
      const completedBindings = {};
      for (const job of jobs) {
        const isProfileJob = Boolean(job.target_profile_id);
        let snapshot = job;
        for (let attempt = 0; attempt < 180; attempt += 1) {
          const endpoint = isProfileJob
            ? `/api/profile-enrichment/jobs/${encodeURIComponent(job.id)}`
            : `/api/persona-creation/jobs/${encodeURIComponent(job.id)}`;
          const current = await api(endpoint);
          if (current && current.ok) snapshot = current.data;
          if (["completed", "completed_with_gaps", "failed", "cancelled"].includes(snapshot.status)) break;
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
        if (!["completed", "completed_with_gaps"].includes(snapshot.status)) {
          throw new Error(`档案 ${job.display_name || job.target_profile_id} 未完成：${snapshot.error || snapshot.status}`);
        }
        const actorId = (snapshot.job_config && snapshot.job_config.world_actor_id)
          || (snapshot.progress && snapshot.progress.actor_id)
          || (job.target_profile_id && (classifiedMissing.find(item => item.profile_id === job.target_profile_id) || {}).id)
          || (matches.find(m => m.display_name === job.display_name) || {}).actor_id;
        const actor = actors.find(item => String(item.id) === String(actorId));
        const bindingId = snapshot.persona_id || (snapshot.new_version && job.target_profile_id) || job.target_profile_id;
        if (actor && bindingId) completedBindings[actor.id] = bindingId;
      }
      return completedBindings;
    }

    $("#form-create-world").addEventListener("submit", async (e) => {
      e.preventDefault();
      const submitBtn = $("#btn-submit-world");
      const errBox = $("#world-create-error");
      const errDetails = $("#world-error-details");
      if (errBox) errBox.style.display = "none";

      submitBtn.disabled = true;
      submitBtn.textContent = "正在构建平行世界...";

      const desc = $("#world-desc").value.trim();
      const baseline = $("#world-baseline").value.trim() || "real_world";
      const start_date = $("#world-start-date").value.trim() || "2011-10-05";
      const sim_end = $("#world-end-date").value.trim() || "2030";

      const wbAgent = $("#wb-agent").value;
      const wbModel = $("#wb-model").value;
      const wbReasoning = $("#wb-reasoning").value;
      const wbSource = ($("input[name='wb-source']:checked") || {}).value || "local_cli";

      const defActorAgent = $("#actor-default-agent").value;
      const defActorModel = $("#actor-default-model").value;
      const defActorReasoning = $("#actor-default-reasoning").value;
      const defActorSource = ($("input[name='actor-default-source']:checked") || {}).value || "local_cli";
      const personaCreatorSource = ($("input[name='persona-creator-source']:checked") || {}).value || "local_cli";
      const personaCreatorAgent = $("#persona-creator-agent").value;
      const personaCreatorModel = $("#persona-creator-model").value;
      const personaCreatorReasoning = $("#persona-creator-reasoning").value || null;

      const payload = {
        description: desc,
        baseline,
        start_date,
        simulation_end: sim_end,
        builder_runtime: {
          agent_id: wbAgent,
          model_id: wbModel,
          reasoning_effort: wbReasoning,
          runtime_source: wbSource
        },
        default_actor_runtime: {
          agent_id: defActorAgent,
          model_id: defActorModel,
          reasoning_effort: defActorReasoning,
          runtime_source: defActorSource
        },
        raw_actors: previewActors,
        persona_creation_runtime: {
          runtime_source: personaCreatorSource,
          agent_id: personaCreatorAgent,
          model_id: personaCreatorModel,
          reasoning_effort: personaCreatorReasoning
        },
        persona_creation_policy: { profile: $("#persona-creator-policy").value || "deep" },
        persona_bindings: collectWorldPersonaBindings(),
        generated_actor_ids: collectGeneratedWorldActors()
      };

      if (previewSeedData) {
        payload.seed = previewSeedData;
      }

      try {
        const res = await api("/api/worlds", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });

        if (res && res.ok) {
          toast(state.lang === "zh-CN" ? "平行世界已成功创建" : "Parallel world created successfully");
          await loadWorlds();
          await openLiveWorld(res.data.world.id);
        } else {
          if (res && ["requires_persona_completion_confirmation", "requires_actor_profile_completion_confirmation"].includes(res.error)) {
            const bindings = await completeMissingWorldActors(res.details || {});
            if (bindings !== null) {
              const retryPayload = {
                ...payload,
                persona_completion_confirmed: true,
                actor_completion_confirmed: true,
                persona_bindings: { ...payload.persona_bindings, ...bindings },
                profile_bindings: { ...payload.persona_bindings, ...bindings },
                generated_actor_ids: collectGeneratedWorldActors()
              };
              if (!retryPayload.seed && res.details && res.details.seed) retryPayload.seed = res.details.seed;
              if (!retryPayload.raw_actors && res.details && res.details.raw_actors) retryPayload.raw_actors = res.details.raw_actors;
              const retry = await api("/api/worlds", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(retryPayload)
              });
              if (retry && retry.ok) {
                toast("Agent 档案已补全并绑定，平行世界已创建");
                await loadWorlds();
                await openLiveWorld(retry.data.world.id);
                return;
              }
              throw new Error((retry && retry.error) || "Agent 档案绑定后世界创建失败");
            }
          }
          throw new Error((res && res.error) || "平行世界创建失败");
        }
      } catch (err) {
        if (errBox && errDetails) {
          errBox.style.display = "block";
          errDetails.textContent = err.message || String(err);
        }
        toast("创建失败: " + (err.message || String(err)));
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = t("create");
      }
    });

    $("#btn-step-world").onclick = async () => {
      const wid = state.currentWorld.id;
      const bid = state.currentBranchId;
      const stepBtn = $("#btn-step-world");
      stepBtn.disabled = true;
      stepBtn.textContent = "推演中...";
      try {
        const res = await api(`/api/worlds/${wid}/branches/${bid}/step`, { method: "POST" });
        if (res && res.ok) {
          toast("推演完成 (Step Timestep committed)");
          await refreshWorldBranchState();
        } else {
          toast((res && res.error) || "LLM 推演失败 (Fail-closed)");
        }
      } finally {
        stepBtn.disabled = false;
        stepBtn.textContent = "继续模拟";
      }
    };

    $("#btn-pause-world").onclick = async () => {
      const wid = state.currentWorld.id;
      const res = await api(`/api/worlds/${wid}/pause`, { method: "POST" });
      if (res && res.ok) {
        state.currentWorld = res.data;
        $("#btn-step-world").disabled = true;
        $("#btn-pause-world").hidden = true;
        $("#btn-resume-world").hidden = false;
      }
    };
    $("#btn-resume-world").onclick = async () => {
      const wid = state.currentWorld.id;
      const res = await api(`/api/worlds/${wid}/resume`, { method: "POST" });
      if (res && res.ok) {
        state.currentWorld = res.data;
        $("#btn-step-world").disabled = false;
        $("#btn-pause-world").hidden = false;
        $("#btn-resume-world").hidden = true;
      }
    };
    $("#btn-view-timeline").onclick = () => $("#world-timeline-feed").scrollIntoView({ behavior: "smooth", block: "center" });
    $("#btn-view-branches").onclick = () => $("#world-branch-select").focus();

    $("#btn-fork-branch").onclick = async () => {
      const wid = state.currentWorld.id;
      const bid = state.currentBranchId;
      const name = prompt(t("branchNamePrompt"), "Branch_Alternative");
      if (!name) return;
      const res = await api(`/api/worlds/${wid}/branches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_branch_id: bid, new_name: name })
      });
      if (res && res.ok) {
        toast(t("toastBranchForked"));
        await openLiveWorld(wid);
      }
    };

    // Action Modal
    $("#btn-open-action").onclick = () => {
      const sel = $("#action-actor");
      sel.innerHTML = state.currentWorldActors.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
      $("#dlg-world-action").showModal();
    };
    $("#action-cancel").onclick = () => $("#dlg-world-action").close();
    $("#form-world-action").addEventListener("submit", async (e) => {
      e.preventDefault();
      const wid = state.currentWorld.id;
      const bid = state.currentBranchId;
      const actor_id = $("#action-actor").value;
      const action_type = $("#action-type").value;
      const description = $("#action-desc").value.trim();
      const target = $("#action-target").value.trim();
      const amount = parseFloat($("#action-amount").value || 2.0);

      const res = await api(`/api/worlds/${wid}/branches/${bid}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_id, action_type, description, target,
          parameters: { amount_billions: amount, budget_billions: amount }
        })
      });

      if (res && res.ok) {
        $("#dlg-world-action").close();
        toast(t("toastActionApplied"));
        await refreshWorldBranchState();
      }
    });

    // Scene Dialogue Modal
    $("#btn-open-scene").onclick = () => {
      const container = $("#scene-actors-checkboxes");
      container.innerHTML = state.currentWorldActors.map(a => `
        <label class="row" style="gap:8px;font-size:13px;">
          <input type="checkbox" name="scene_actor" value="${a.id}" ${['steve_jobs', 'jensen_huang'].includes(a.id) ? 'checked' : ''} />
          <span>${esc(a.name)} (${esc(a.actor_type)})</span>
        </label>`).join("");
      $("#dlg-world-scene").showModal();
    };
    $("#scene-cancel").onclick = () => $("#dlg-world-scene").close();
    $("#form-world-scene").addEventListener("submit", async (e) => {
      e.preventDefault();
      const wid = state.currentWorld.id;
      const bid = state.currentBranchId;
      const checkedActors = [...$$("input[name='scene_actor']:checked")].map(i => i.value);
      const topic = $("#scene-topic").value.trim();

      if (checkedActors.length < 2) {
        alert(t("sceneActorsNeed"));
        return;
      }

      toast(t("toastSceneRunning"));
      const res = await api(`/api/worlds/${wid}/branches/${bid}/scene`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ actor_ids: checkedActors, topic })
      });

      if (res && res.ok) {
        $("#dlg-world-scene").close();
        toast(t("toastSceneCompleted"));
        await refreshWorldBranchState();
      }
    });

    // Scenario Evaluation Modal
    $("#btn-open-eval").onclick = () => {
      $("#eval-result-view").hidden = true;
      $("#eval-form-view").hidden = false;
      $("#dlg-world-eval").showModal();
    };
    $("#eval-cancel").onclick = () => $("#dlg-world-eval").close();
    $("#btn-run-eval").onclick = async () => {
      const wid = state.currentWorld.id;
      const q = $("#eval-question").value.trim();
      const count = parseInt($("#eval-branches").value, 10) || 3;

      toast(t("toastEvaluating"));
      const res = await api(`/api/worlds/${wid}/evaluate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, branch_count: count })
      });

      if (res && res.ok) {
        const ev = res.data;
        const resBox = $("#eval-result-view");
        resBox.hidden = false;
        $("#eval-form-view").hidden = true;

        const distEntries = Object.entries(ev.distribution || {});
        resBox.innerHTML = `
          <div class="card" style="padding:16px;">
            <h4>${esc(ev.question)}</h4>
            <p class="meta" style="margin:6px 0 14px;">${t("sampledBranches").replace("{n}", ev.total_branches)}</p>
            <div class="stack" style="gap:10px;">
              ${distEntries.map(([sc, cnt]) => `
                <div>
                  <div class="row-between"><strong style="font-size:13px;">${esc(sc)}</strong><span class="num meta">${cnt} / ${ev.total_branches}</span></div>
                  ${progressBar(sc, cnt / Math.max(1, ev.total_branches))}
                </div>`).join("")}
            </div>
            <div class="meta" style="margin-top:16px;white-space:pre-wrap;background:var(--surface);padding:12px;border-radius:var(--radius-sm);">
              ${esc(ev.causal_summary || "")}
            </div>
          </div>
          <div class="row" style="justify-content:flex-end;margin-top:12px;">
            <button class="btn btn-secondary" onclick="document.getElementById('dlg-world-eval').close()">${t("close")}</button>
          </div>
        `;
      }
    };

    // Causal Graph Query Modal
    $("#btn-open-causal").onclick = () => {
      $("#dlg-world-causal").showModal();
      queryCausalChain();
    };
    $("#causal-close").onclick = () => $("#dlg-world-causal").close();
    $("#btn-query-causal").onclick = () => queryCausalChain();

    async function queryCausalChain() {
      if (!state.currentWorld || !state.currentBranchId) return;
      const target = $("#causal-target-input").value.trim() || "divergence";
      const res = await api(`/api/worlds/${state.currentWorld.id}/branches/${state.currentBranchId}/causal?target=${encodeURIComponent(target)}`);
      const container = $("#causal-chain-results");
      if (res && res.ok && Array.isArray(res.data) && res.data.length) {
        container.innerHTML = res.data.map((step, idx) => `
          <div class="card" style="padding:12px;border-left:3px solid var(--accent);background:var(--surface);">
            <div class="row-between">
              <span class="tag solid">Step ${idx + 1} · ${esc(step.type)}</span>
              <span class="meta num">${esc(step.time)}</span>
            </div>
            <p style="margin-top:6px;font-weight:600;">${esc(step.name)}</p>
            ${step.relation_to_next ? `<p class="meta" style="margin-top:4px;color:var(--accent);">↓ ${esc(step.relation_to_next)}</p>` : ''}
          </div>`).join("");
      } else {
        container.innerHTML = `<div class="empty"><p class="meta">${t("noCausalChain")}</p></div>`;
      }
    }

    // Historical Replay Modal
    let replayTrajectory = null;
    $("#btn-open-replay").onclick = async () => {
      if (!state.currentWorld || !state.currentBranchId) return;
      const res = await api(`/api/worlds/${state.currentWorld.id}/branches/${state.currentBranchId}/replay`);
      if (res && res.ok && res.data && res.data.steps) {
        replayTrajectory = res.data.steps;
        const slider = $("#replay-slider");
        slider.min = "0";
        slider.max = String(Math.max(0, replayTrajectory.length - 1));
        slider.value = slider.max;
        updateReplayStepView(parseInt(slider.value, 10));
        $("#dlg-world-replay").showModal();
      } else {
        toast(t("noReplaySnapshots"));
      }
    };
    $("#replay-close").onclick = () => $("#dlg-world-replay").close();
    $("#replay-slider").oninput = (e) => {
      updateReplayStepView(parseInt(e.target.value, 10));
    };

    function updateReplayStepView(idx) {
      if (!replayTrajectory || !replayTrajectory[idx]) return;
      const step = replayTrajectory[idx];
      $("#replay-time-display").textContent = step.timestamp;
      $("#replay-step-display").textContent = `Step ${idx + 1} / ${replayTrajectory.length}`;
      $("#replay-step-title").textContent = `Snapshot at ${step.timestamp}`;
      $("#replay-step-diff").textContent = step.diff_summary || `Active events: ${step.active_events ? step.active_events.length : 0}`;

      const evBox = $("#replay-step-events");
      if (step.active_events && step.active_events.length) {
        evBox.innerHTML = step.active_events.map(e => `
          <div class="card" style="padding:10px;font-size:12px;">
            <strong>${esc(e.cause)}</strong>
            <p class="meta" style="margin-top:2px;">${esc(e.effect)}</p>
          </div>`).join("");
      } else {
        evBox.innerHTML = `<p class="meta">${t("baselineStable")}</p>`;
      }
    }

    // Simulation Report Modal
    $("#btn-open-report").onclick = async () => {
      if (!state.currentWorld) return;
      toast(t("toastReportGenerating"));
      const res = await api(`/api/worlds/${state.currentWorld.id}/report`);
      if (res && res.ok && res.data) {
        const report = res.data;
        $("#report-content-view").textContent = report.markdown_report;
        $("#dlg-world-report").showModal();
      }
    };
    $("#report-close").onclick = () => $("#dlg-world-report").close();
    $("#btn-copy-report").onclick = () => {
      const text = $("#report-content-view").textContent;
      navigator.clipboard.writeText(text);
      toast(t("toastReportCopied"));
    };

    // Room Inspector Tabs (人物状态 / 关系状态 / 召回证据 / 引擎绑定)
    $$(".inspector-tabs button[data-ipanel]").forEach(b => b.addEventListener("click", () => {
      $$(".inspector-tabs button[data-ipanel]").forEach(x => x.classList.toggle("is-active", x === b));
      ["state", "relation", "recall", "bind"].forEach(k => {
        const el = $("#insp-" + k);
        if (el) el.hidden = b.dataset.ipanel !== k;
      });
    }));

    // Parallel World Inspector Tabs
    $$(".inspector-tabs button[data-wpanel]").forEach(b => b.addEventListener("click", () => {
      $$(".inspector-tabs button[data-wpanel]").forEach(x => x.classList.toggle("is-active", x === b));
      ["projects", "orgs", "tech", "actors"].forEach(k => {
        const el = $("#world-insp-" + k);
        if (el) el.hidden = b.dataset.wpanel !== k;
      });
    }));

    // Agents Listeners
    $$("#agent-filter button").forEach(b => b.addEventListener("click", () => {
      state.agentFilter = b.dataset.afilter;
      $$("#agent-filter button").forEach(x => x.classList.toggle("is-active", x === b));
      renderAgents();
    }));
    $("#btn-rescan").onclick = async () => {
      const btn = $("#btn-rescan");
      const originalText = btn ? btn.textContent : "";
      if (btn) {
        btn.disabled = true;
        btn.textContent = t("scanning") || "扫描中…";
      }
      toast(t("scanning"));
      try {
        await loadAgents(true);
        toast(t("scanned"));
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = originalText;
        }
      }
    };

    // API Providers Listeners
    $("#btn-add-api").onclick = () => {
      resetApiProfileEditor();
      $("#dlg-api").showModal();
    };
    $("#api-cancel").onclick = () => {
      resetApiProfileEditor();
      $("#dlg-api").close();
    };
    $("#api-reasoning-mode").addEventListener("change", syncApiReasoningFields);
    syncApiReasoningFields();
    $("#form-api").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#api-error");
      if (err) err.hidden = true;

      const name = $("#api-name").value.trim();
      const base_url = $("#api-url").value.trim();
      const api_key = $("#api-env").value.trim();
      const provider_type = $("#api-provider").value;
      const default_model = $("#api-model").value.trim();

      if (!name || !base_url) {
        if (err) {
          err.hidden = false;
          err.textContent = t("needNameUrl");
        }
        return;
      }

      let reasoningCapability;
      try {
        reasoningCapability = apiReasoningMetadata();
      } catch (reasoningError) {
        if (err) {
          err.hidden = false;
          err.textContent = reasoningError && reasoningError.message
            ? reasoningError.message
            : "Reasoning 能力配置无效";
        }
        return;
      }
      const capabilityKey = default_model || "default";

      const editingId = state.apiEditingId;
      const body = {
        name,
        base_url,
        default_model,
        provider_type,
        metadata: {
          model_capabilities: {
            [capabilityKey]: { reasoning_capability: reasoningCapability }
          }
        }
      };
      if (api_key) body.api_key = api_key;
      const res = await api(
        editingId ? `/api/auth-profiles/${encodeURIComponent(editingId)}` : "/api/auth-profiles",
        {
        method: editingId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
        }
      );

      if (res && res.ok) {
        resetApiProfileEditor();
        $("#dlg-api").close();
        toast(state.lang === "zh-CN" ? "API 提供方已保存并动态注册" : "API provider saved and registered");
        await loadApiProfiles();
        await loadAgents(true);
      } else {
        if (err) {
          err.hidden = false;
          err.textContent = (res && res.error) || "保存失败";
        }
        toast("保存失败: " + ((res && res.error) || ""));
      }
    });

    // Personas Search
    const pq = $("#persona-q");
    if (pq) {
      pq.addEventListener("input", () => {
        state.personaQ = pq.value;
        loadProfiles();
      });
    }
    const profileTypeFilter = $("#profile-type-filter");
    if (profileTypeFilter) profileTypeFilter.addEventListener("change", () => {
      state.profileType = profileTypeFilter.value;
      loadProfiles();
    });
    const profileStatusFilter = $("#profile-status-filter");
    if (profileStatusFilter) profileStatusFilter.addEventListener("change", () => {
      state.profileStatus = profileStatusFilter.value;
      loadProfiles();
    });
    const profileSort = $("#profile-sort");
    if (profileSort) profileSort.addEventListener("change", () => {
      state.profileSort = profileSort.value;
      loadProfiles();
    });

    onClick("#btn-create-persona", () => {
      openPersonaCreation();
      if (!state.agents.length) {
        hydrateRuntimeSelectors(() => setupPersonaCreationRuntimeSelectors());
      }
    });
    onClick("#btn-create-profile", openProfileCreate);
    onClick("#profile-task-refresh", loadBackgroundJobs);
    $$('[data-background-filter]').forEach(button => {
      button.addEventListener("click", () => {
        state.backgroundJobFilter = button.getAttribute("data-background-filter") || "all";
        $$('[data-background-filter]').forEach(item => item.classList.toggle("is-active", item === button));
        renderBackgroundJobs();
      });
    });
    onClick("#profile-task-prev", async () => {
      if (state.backgroundTerminalPage <= 1) return;
      state.backgroundTerminalPage -= 1;
      await loadBackgroundJobs();
    });
    onClick("#profile-task-next", async () => {
      const counts = state.backgroundJobCounts || {};
      if (state.backgroundTerminalPage * state.backgroundTerminalPageSize >= Number(counts.terminal || 0)) return;
      state.backgroundTerminalPage += 1;
      await loadBackgroundJobs();
    });
    onClick("#profile-task-cleanup", async () => {
      const selector = $("#profile-task-cleanup-status");
      const normalized = String(selector && selector.value || "all").trim().toLowerCase();
      const statuses = normalized === "all"
        ? ["failed", "completed", "completed_with_gaps", "cancelled"]
        : normalized === "completed"
          ? ["completed", "completed_with_gaps"]
          : [normalized];
      if (!statuses.every(item => ["failed", "completed", "completed_with_gaps", "cancelled"].includes(item))) {
        toast("清理范围无效");
        return;
      }
      if (!window.confirm("只会从后台任务中心移除记录，不会删除已创建的人格、证据或版本。")) return;
      const res = await api("/api/background-jobs/cleanup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ statuses })
      });
      if (!res || !res.ok) toast((res && res.error) || "清理失败");
      else await loadBackgroundJobs();
    });
    onClick("#profile-create-close", () => $("#dlg-profile-create").close());
    onClick("#profile-enrich-close", () => $("#dlg-profile-enrich").close());
    onClick("#profile-enrich-progress-close", () => $("#dlg-profile-enrich-progress").close());
    onClick("#profile-enrich-progress-pause", async () => {
      const job = state.profileEnrichmentJob;
      if (!job) return;
      const res = await api(`/api/profile-enrichment/jobs/${encodeURIComponent(job.id)}/pause`, { method: "POST" });
      if (res && res.ok) renderProfileEnrichmentProgress(res.data);
      else toast((res && res.error) || "升级任务暂停失败");
    });
    onClick("#profile-enrich-progress-resume", async () => {
      const job = state.profileEnrichmentJob;
      if (!job) return;
      const res = await api(`/api/profile-enrichment/jobs/${encodeURIComponent(job.id)}/resume`, { method: "POST" });
      if (res && res.ok) {
        renderProfileEnrichmentProgress(res.data);
        pollProfileEnrichmentJob(res.data.id, res.data);
      } else toast((res && res.error) || "升级任务继续失败");
    });
    onClick("#profile-enrich-progress-cancel", async () => {
      const job = state.profileEnrichmentJob;
      if (!job || !window.confirm("确定停止本次升级吗？正在进行的模型分析将被终止。")) return;
      const res = await api(`/api/profile-enrichment/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
      if (res && res.ok) renderProfileEnrichmentProgress(res.data);
      else toast((res && res.error) || "升级任务取消失败");
    });
    onClick("#profile-enrich-progress-retry", async () => {
      const job = state.profileEnrichmentJob;
      if (!job || !BACKGROUND_FAILED.has(job.status)) return;
      const button = $("#profile-enrich-progress-retry");
      if (button) { button.disabled = true; button.textContent = "正在重试…"; }
      try {
        const res = await api(`/api/profile-enrichment/jobs/${encodeURIComponent(job.id)}/retry`, { method: "POST" });
        if (!res || !res.ok) throw new Error((res && res.error) || "升级任务重试失败");
        state.profileEnrichmentJob = res.data;
        if (button) button.textContent = "重试当前阶段";
        renderProfileEnrichmentProgress(res.data);
        await pollProfileEnrichmentJob(res.data.id, res.data);
        await loadProfiles();
        await loadBackgroundJobs();
      } catch (err) {
        if (button) { button.disabled = false; button.textContent = "重试当前阶段"; }
        console.error("Profile enrichment retry failed:", err);
        toast(`升级重试失败：${err && err.message ? err.message : err}`);
      }
    });
    const profileCreateForm = $("#form-profile-create");
    if (profileCreateForm) profileCreateForm.addEventListener("submit", submitProfileCreate);
    const profileEnrichForm = $("#form-profile-enrich");
    if (profileEnrichForm) profileEnrichForm.addEventListener("submit", (event) => {
      submitProfileEnrichment(event).catch((err) => {
        console.error("Profile enrichment submit failed:", err);
        const error = $("#profile-enrich-error");
        if (error) {
          error.hidden = false;
          error.textContent = `升级失败：${err && err.message ? err.message : err}`;
        }
        toast(`升级失败：${err && err.message ? err.message : err}`);
      });
    });
    onClick("#persona-create-close", () => $("#dlg-persona-create").close());
    onClick("#pc-cancel-create", () => $("#dlg-persona-create").close());
    onClick("#persona-creation-progress-close", () => $("#dlg-persona-creation-progress").close());
    const personaForm = $("#form-persona-create");
    if (personaForm) personaForm.addEventListener("submit", submitPersonaCreation);
    onClick("#pc-persona-type", personaCreationTypeChanged);
    const personaTypeSel = $("#pc-persona-type");
    if (personaTypeSel) personaTypeSel.addEventListener("change", personaCreationTypeChanged);
    const personaPolicySel = $("#pc-policy");
    if (personaPolicySel) personaPolicySel.addEventListener("change", personaCreationPolicyChanged);
    onClick("#pc-pause", async () => {
      const job = state.personaCreationJob;
      if (!job) return;
      const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(job.id)}/pause`, { method: "POST" });
      if (res && res.ok) { state.personaCreationJob = res.data; renderPersonaCreationJob(res.data); }
    });
    onClick("#pc-resume", async () => {
      const job = state.personaCreationJob;
      if (!job) return;
      const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(job.id)}/resume`, { method: "POST" });
      if (res && res.ok) { state.personaCreationJob = res.data; renderPersonaCreationJob(res.data); }
      else toast((res && res.error) || "Runtime 不可用，无法继续");
    });
    onClick("#pc-cancel", async () => {
      const job = state.personaCreationJob;
      if (!job) return;
      if (!window.confirm("确定停止本次 Persona 创建吗？正在进行的模型分析将被终止。")) return;
      const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
      if (res && res.ok) { state.personaCreationJob = res.data; renderPersonaCreationJob(res.data); }
    });
    onClick("#pc-retry", async () => {
      const job = state.personaCreationJob;
      if (!job || !BACKGROUND_FAILED.has(job.status)) return;
      const button = $("#pc-retry");
      if (button) { button.disabled = true; button.textContent = "正在重试…"; }
      try {
        const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(job.id)}/retry`, { method: "POST" });
        if (!res || !res.ok) throw new Error((res && res.error) || "Persona Creation 重试失败");
        state.personaCreationJob = res.data;
        renderPersonaCreationJob(res.data);
        if (state.personaCreationPoll) clearInterval(state.personaCreationPoll);
        state.personaCreationPoll = setInterval(() => pollPersonaCreationJob(res.data.id), 1000);
        if (button) button.textContent = "重试当前阶段";
      } catch (err) {
        if (button) { button.disabled = false; button.textContent = "重试当前阶段"; }
        console.error("Persona creation retry failed:", err);
        toast(`Persona 重试失败：${err && err.message ? err.message : err}`);
      }
    });
    onClick("#pc-resume-model", () => armPersonaCreationModelSwitch("resume"));
    onClick("#pc-retry-model", () => armPersonaCreationModelSwitch("retry"));
    onClick("#pc-continue", async () => {
      const job = state.personaCreationJob;
      if (!job) return;
      const res = await api(`/api/persona-creation/jobs/${encodeURIComponent(job.id)}/continue`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ research_policy: { profile: $("#pc-policy").value || (job.research_policy || {}).profile || "deep" } })
      });
      if (res && res.ok) {
        state.personaCreationJob = res.data;
        renderPersonaCreationJob(res.data);
        if (state.personaCreationPoll) clearInterval(state.personaCreationPoll);
        state.personaCreationPoll = setInterval(() => pollPersonaCreationJob(res.data.id), 1000);
      } else toast((res && res.error) || "继续研究启动失败");
    });
    onClick("#pc-interview-submit", submitPersonaInterviewAnswer);

    } catch (err) {
      console.error("UI initialization error:", err);
    }

    try {
      applyLang();
    } catch (err) {
      console.error("UI render error:", err);
    }

    try {
      await Promise.all([loadPersonas(), loadRooms(), loadApiProfiles(), loadWorlds()]);
      await loadProfiles();
      await loadBackgroundJobs();
      await loadNarrativeProjects();
    } catch (err) {
      console.error("UI data load error:", err);
    }
    loadAgents(false).catch((err) => console.error("agent scan error:", err));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
