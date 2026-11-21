# 重新剪辑审校迁移 v3（carry-review 模式）

`subtitle-conform --source INPUT.json --output REPORT.json --carry-review` 在节目重新剪辑后，把字幕组在旧剪辑实例上批准的改词/隐藏决定迁移到新版字幕：只凭剪辑部显式提供的来源对应关系继承，正文已更正或候选结论不一致的区间标记重审并阻断交付；可交付时按迁移后的实显正文重新计算质检与 SRT。`--carry-review` 与 `--broadcast` 互斥；不带模式标志时原行为完全不变，`cut-clearance` 不受影响。这里做的是字幕编辑交接，不是播出排版或素材授权判断。

## 输入契约

根对象严格只含四个必填字段：`previous`、`current`、`links`、`edits`。未知字段、重复 JSON 键、非有限常量、8 MB 上限沿用原报错语义。

- `previous`、`current`：分别是旧版和新版的普通对版输入（见[对版契约](contract.md)），各自独立通过原有校验与 2000 精确碎片上限。跨版本同一个素材 ID 表示同一素材和同一毫秒坐标系；两版节目时间互不相干。
- `links`：可为空的数组，最多 2000 项。每项严格为 `{id, old_clip_id, new_clip_id, start_ms, end_ms}`：
  - `id` 在数组内唯一，沿用普通输入的字符串规则；
  - `old_clip_id`/`new_clip_id` 必须分别引用 `previous`/`current` 中的剪辑，且两个剪辑使用同一素材；
  - `[start_ms,end_ms)` 为正长半开区间，整数毫秒（拒绝布尔值），且完整落在两边剪辑的源使用范围 `[in_ms,out_ms)` 内；
  - 同一 `(old_clip_id, new_clip_id)` 对下的多条关系区间可以相接，不能重叠；不同旧剪辑指向同一新剪辑的关系可以重叠，表示该段有多个可信旧候选，不按数组顺序覆盖；一个旧剪辑可以被多个新剪辑继承。表内没列出的关系不能靠 ID、素材、时间或文字相同推断。
- `edits`：可为空的数组，最多 2000 项。每项严格为 `{id, clip_id, cue_id, start_ms, end_ms, action}`（`replace` 另带 `text`）：
  - `id` 在数组内唯一；`(clip_id, cue_id)` 必须引用旧版某个**实际产生**的精确碎片（组合合法但无正长交集不算），即使该碎片短到不会出现在旧 SRT 中；
  - `[start_ms,end_ms)` 为正长半开整数毫秒区间，完整落在该碎片的源范围内；
  - `action` 为 `replace`（必须附带 `text`，正文规则同普通 cue）或 `hide`（不得携带 `text`）；
  - 同一旧碎片上的审校区间可以相接，不能重叠。一条决定只属于指定的旧剪辑实例，不向别的旧实例自动传播。

未知字段、无效引用、越界或非法区间均为输入错误。

## 判定规则

以新版普通对版产生的每个精确碎片为范围，独立判断其各段源区间。一条关系只在它的区间、新版碎片、对应旧剪辑上同素材同 cue ID 的旧碎片**三者正长重叠**处提供依据；旧 cue 换了 ID、移到其他素材、没有旧碎片或仅与端点相接，都不提供依据。旧版节目时间从不带入新版；新版节目时间按新版 EDL 顺序、剪辑起点和速度用 `Fraction` 精确换算。

对每段源区间：

1. 没有适用关系的部分保留新版原文。
2. 有适用关系时，每个候选关系独立作出判断（主张）：
   - 该候选在这一段没有旧审校决定：主张新版原文；
   - 有决定且旧、新 cue 的整段原始正文（各自输入 cue 的 `text`，不是改词后的正文）逐字符相等：主张决定指定的正文（`replace`）或隐藏（`hide`）；
   - 有决定但原始正文已改变：这条依据**失效**。
3. 有任何失效依据时，该段结果为 `stale_review`（即使其他候选都同意）。
4. 否则所有候选的显示结果一致才可自动采用：隐藏和任何正文都不相同，两个正文逐字符比较。一致时结果为 `visible`（附确定正文）或 `hidden`；不一致时结果为 `conflict`，不按多数、数组顺序或有无决定来挑。替换文字恰好等于新版原文时两者不冲突，但成功采用的旧决定仍须留痕。源起止变动本身不使重叠部分失效。

## 报告契约

原子新建一个 JSON 报告（目标已存在则退出 2 并保留旧文件）。字段：

- `schema_version`（3）、`mode`（`"carry-review"`）、`status`（`ready`/`blocked`）、`output_duration_ms`（新版节目精确有理数字符串）；
- `review_segments`：逐段审校清单，无遗漏、无重复地划分**所有**新版精确碎片（含隐藏与待重审区间）。同一新碎片内判定、正文和全部候选依据相同的相邻区间并为一段。每行：
  - `fragment_id`：`[new_clip_id, cue_id]`；
  - `source_start_ms`/`source_end_ms`：整数源区间；
  - `start_ms`/`end_ms`：精确新版节目区间（有理数字符串）；
  - `verdict`：`visible`/`hidden`/`stale_review`/`conflict`；
  - `text`：确定显示的正文，否则为 `null`；
  - `basis`：全部适用关系，按关系 ID 排序；每项含 `link_id`、`old_clip_id`、`edit_id`（无则为 `null`）、`claim`（`visible`/`hidden`/`stale`）与 `text`（仅 `visible` 时有值）。质量问题凭 `fragment_id` 加区间可追溯发生位置。
- `edit_dispositions`：按旧审校 ID 排序，每条给出 `edit_id`、`old_fragment_id`、原源区间 `source_start_ms`/`source_end_ms`、`adopted`（在至少一个新实例上成功采用的源区间并集）与 `not_adopted`（在所有新实例中均未成功采用的补集）；两列都是按起点排列的最大连续区间。只有旧决定实际参与且该段结果为 `visible` 或 `hidden` 才算成功；相同源区间在多个新实例被采用只在并集中计一次，各实例的依据仍保留在 `review_segments` 里。旧决定仅因剪掉或无对应关系未迁移不阻断新版。
- `delivery_lines`（`blocked` 时为 `null`）：只含可见字幕。同一新剪辑实例、同一 cue 内相邻且实显正文相同的区间并为一条，即使审校依据有变化；不跨隐藏间隙、剪辑实例或 cue 合并。每行以 `line_id`（`[new_clip_id, cue_id, source_start_ms, source_end_ms]`）标识，含源区间、精确新版节目区间和实际显示正文。
- `quality`（`blocked` 时为 `null`）：以交付行重新计算的普通模式质检——`lines`（逐行精确区间、实显正文、`chars_per_second`、`issues`、`srt_start_ms`/`srt_end_ms`）、`overlap_count`、`overlap_examples`（前 100 对）、`warning_lines`、`srt_cues`、`srt_dropped_line_ids`（舍入后消失的交付行身份）。阈值取 `current`，使用合并后的精确时长和实显正文，不沿用新版未应用审校时的统计；舍入后消失的字幕仍出现在精确交付行和质检里。
- `srt_text`（`blocked` 时为 `null`）：按普通模式的最近毫秒 half-up 舍入生成的 SRT；没有可见行时交付行为空、质检计数为零、`srt_text` 为空串 `""`。
- `summary`：`status`、`review_segments`、`stale_review_segments`、`conflict_segments`、`edit_decisions`、`edits_carried`（`adopted` 非空的决定数）、`delivery_lines`、`srt_cues`。

排序：`review_segments` 与 `delivery_lines` 按精确节目起点、终点、剪辑 ID、cue ID、源起止排列；候选依据按关系 ID、旧决定去向按决定 ID 排列；字符串按 Unicode 码点比较。所有精确节目时间为有理数字符串。

只要任一区间为 `stale_review` 或 `conflict`，报告即为 `blocked`：仍交付完整的逐段审校清单和旧决定去向，但 `delivery_lines`、`quality`、`srt_text` 均为 `null`，不把局部结果当成可发版本。没有待重审片段即为 `ready`，即使结果没有任何可见字幕。

## 失败约定

非法输入（含非法引用、越界区间、重叠区间、未知字段、超限）或目标已存在时退出 2，不创建或覆盖报告；合法输入（包括 `blocked`）成功时先完整校验与计算，再同目录临时文件原子硬链接新建唯一一份 JSON。不读取媒体、不联网、不引入新的运行时依赖。
