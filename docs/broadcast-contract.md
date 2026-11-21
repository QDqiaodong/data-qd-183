# 播出端单轨字幕对版 v2（broadcast 模式）

`subtitle-conform --source INPUT.json --output REPORT.json --broadcast` 在原对版结果之上，为 25 fps（1 帧 = 40 ms）播出端交付一条单轨字幕。不带 `--broadcast` 时，输入、报告、SRT 舍入、告警与 `subtitle_conform.service.run(data)` 的行为完全不变。

## 输入契约

根对象严格只含 `conform` 与 `broadcast` 两个字段，未知字段、重复 JSON 键、非有限常量沿用原报错语义。`conform` 是原有完整输入，沿用原校验、精确有理数毫秒对版、半开区间、2000 碎片上限与排序。新模式以**所有精确碎片**为审核单位，包括按旧 SRT 最近毫秒舍入会消失的碎片；重复素材产生独立的 `[clip_id, cue_id]` 复合身份。

`broadcast` 字段全部必填，三个数组可以为空，所有整数字段拒绝布尔值：

| 字段 | 约束 |
| --- | --- |
| `max_shift_ms` | 整数 0..200，条目起止距锚点的最大绝对偏移 |
| `min_gap_frames` | 整数 0..5，相邻条目最小间隔帧数（×40 ms） |
| `protected` | 0..200 项 `{start_ms, end_ms}`，半开、正长、不超出精确节目范围；可重叠相接，按并集禁显 |
| `join_cuts` | `[left_clip_id, right_clip_id]` 数组，只允许 EDL 中相邻、有向、不重复的剪缝 |
| `fragment_rules` | 每项含 `fragment_id`、布尔 `must_keep`、整数 `priority`（1..1000） |

`fragment_rules` 中同一精确碎片最多声明一次；未知身份（包括 clip/cue 组合合法但无正长交集）为输入错误。未声明的碎片为可选、优先值 1；优先值只对可选碎片计分。

## 合法条目

一条播出字幕可使用一个精确碎片，或精确排序中**连续**的一组碎片。相邻碎片必须同时满足：来自某条批准剪缝两侧的 clip、属于同一源 cue、前碎片源结束等于后碎片源开始、前碎片精确节目结束等于后碎片精确节目开始。可跨多道满足条件的剪缝并允许速度变化；不允许跳过中间排序项、跨源时间缺口、把同一源区间重放当连续、或仅因文字相同合并。组内正文完整显示一次，不改写不删字。

条目起止均为非负 40 ms 整数倍、正长，完全落在首碎片 clip 精确起点到末碎片 clip 精确终点之间，与组内每个碎片有正长交集，且起点距首碎片精确起点、终点距末碎片精确终点各不超过 `max_shift_ms`（边界用精确 `Fraction` 比较，不先舍入）。时长不小于 `conform.min_duration_ms`；非空白 Unicode 代码点数 ÷ 精确秒数不大于 `conform.max_chars_per_second`。与保护段只端点接触（无正长交集）合法。

## 方案选择（五级，低优先级不牺牲高优先级）

1. 覆盖的必保碎片数最多（组内每个原碎片分别计数）；
2. 覆盖的可选碎片优先值之和最大；
3. 播出条目数最少（优先连读，不重复显示同一句）；
4. 全部条目起止绝对偏移量之和最小，每条只相对该组首/末精确锚点比较；
5. 条目序列按 `(start_ms, end_ms, [fragment_id, ...])` 字典序最小，时间按整数比、身份按原字符串 Unicode 码点、列表逐元素比较且真前缀较小。

每个碎片至多使用一次；条目按组的精确排序下标顺序输出（禁止倒置对白）；相邻条目间隔至少 `min_gap_frames × 40 ms`。不能在 SRT 导出时截短或挪动已选条目。空方案始终是候选。

## 报告契约

原子新建一个 JSON 报告（目标已存在则退出 2 并保留旧文件，不另建 SRT 文件），字段：

- `schema_version`（2）、`mode`（`"broadcast"`）、`status`（`ready`/`blocked`）、`output_duration_ms`（精确有理数字符串）；
- `scores`：`must_keep_fragments_total`、`must_keep_fragments_covered`、`optional_priority_sum`、`entry_count`、`total_abs_offset_ms`（有理数字符串）；
- `entries`：编号从 1 开始，含整数 `start_ms`/`end_ms`、`text`、有序 `fragment_ids`；
- `fragment_audit`：按原排序，逐片给出身份、源区间整数毫秒、精确节目区间有理数字符串、`must_keep`、`priority`、`has_legal_entry`、`covered_by_entry`（编号或 `null`）；
- `srt_text`：`ready` 时为选中条目整数时间渲染的 SRT（零条目为空串 `""`）；`blocked` 时为 `null`；
- `counterfactuals`：仅 `ready` 出现，针对每条未被覆盖的可选碎片。

### 反事实解释

对每条未入选可选碎片，在仍覆盖**全部**必保碎片并强制包含该碎片的所有合法方案中，求可达的最大可选优先值之和；不存在任何方案时为 `null`。这是全局重排结果（可换分组、时槽与取舍），不是在当前方案上硬塞。四类 `reason`：

| reason | 含义 |
| --- | --- |
| `no_legal_entry` | 没有任何合法条目包含该碎片（`max_optional_priority_sum` 为 `null`） |
| `cannot_satisfy_all_must_keep` | 有合法条目，但无法与全部必保碎片同时排入（`null`） |
| `optional_score_reduced` | 可与必保同排，但可选总分严格下降 |
| `same_optional_score_decided_by_later_rules` | 可维持可选总分，由条目数/偏移/字典序等后续规则取舍 |

`blocked` 是成功生成的诊断报告而非输入错误：仍按同一五级规则给出诊断方案与逐片审计，但 `srt_text` 为 `null`，且不输出任何可选反事实（不假定必保可满足）。

## 实现要点

枚举所有合法连读组与组内 40 ms 网格时机后，前向扫描配合按结束时间索引的 Fenwick 前缀最大值求唯一最优方案（自动满足碎片不重叠、顺序与最小间隔）；反向扫描用 Fenwick 后缀最大值为每个时机缓存最优后缀，反事实即「最优前缀 + 强制条目 + 最优后缀」的组合。全部比较基于 `fractions.Fraction` 精确值，仅用 Python 标准库，不读取媒体、不引入外部服务或求解器。2000 碎片规模下秒级完成。
