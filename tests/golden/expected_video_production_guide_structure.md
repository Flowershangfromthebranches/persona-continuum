// Golden structure: EPxx《...》AI视频完整制作方案
//
// Machine-parsed acceptance contract (task #52/#53) abstracted from the
// user's reference document 《视频生成模型.md》. Every non-blank line below
// is a marker that MUST appear in the generated `markdown_document` after
// `{...}` placeholder substitution — EXCEPT lines starting with `!`, which
// must NEVER appear (developer-facing leakage). Lines starting with `//` are
// comments.
//
// Placeholders:
//   {EP}       EP01
//   {TITLE}    episode title
//   {MODEL}    target model display name
//   {CHAR_KEY} first character master asset key
//   {LOC_KEY}  first location master asset key

// ── Document head (two H1 lines) ──
# {EP}《{TITLE}》
# {MODEL} AI视频完整制作方案

// ── Fixed pre-video sections ──
## 一、制作目标与基础设置
## 二、先建立永久角色参考素材
### 素材1：{CHAR_KEY}
这张图不是一个视频分镜
完整图片生成 Prompt（直接复制到图片生成工具）：
## 三、本集需要建立的场景参考素材
{LOC_KEY}
## 四、关键道具 / UI / 屏幕与开镜画面素材
## 五、整集视频结构
| 时间 | 视频段 | 作用 | 模式 | 使用素材 |

// ── Per-video work order contract (every clip section) ──
## 使用方式
生成模式：
### Start Frame
### Ingredients / References
### Prompt
生成完成以后

// ── Post-production sections (dynamic numbering) ──
、尾帧接首帧完整流程
、字幕时间轴
、手机 / 邮件 / UI 后期合成方案
、对白与环境音
、SFX
、BGM 完整生成 Prompt
、剪辑顺序与转场
、最终检查清单

// ── BGM prompt is a complete music brief (task #38/#39) ──
OVERALL STYLE:
INSTRUMENTATION:
EMOTIONAL ARC:
MIX:
STRICT:

// ── Copy-ready prompt contract (every video prompt, task #24/#33) ──
GOAL
of the SAME continuous episode.
REFERENCE INPUTS
CHARACTER IDENTITY
ENVIRONMENT IDENTITY
ACTION AND PERFORMANCE
CAMERA
TEMPORAL PROGRESSION
CONTINUITY
ENDING
STRICT

// ── Honesty rules: developer-facing fields never leak (task #28/#29/#7) ──
!duration_seconds:
!target_duration_seconds:
!model_prompt_package
!clip_fingerprint
