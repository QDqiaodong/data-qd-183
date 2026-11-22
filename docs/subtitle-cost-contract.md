# 跨节目字幕译制费分账 v1（subtitle-cost）

`subtitle-cost --source INPUT.json --output OUTPUT.json` 是独立离线命令，按各成本中心对库内源字幕的**实际源时间用量**分摊供应商按源字幕一次性收取的译制费。输入以根对象的 `op` 区分三个步骤：`collect`（统计单节目用量包）、`merge`（多级归并并处理节目撤回）、`settle`（按覆盖权重结算）。它不读取媒体文件，不改变 `subtitle-conform`、`--broadcast`、`--carry-review`、`cut-clearance` 与 `subtitle-playback-qc` 的行为；输入解析、单份输入 8 MB 上限、重复 JSON 键/非有限常量拒绝、严格字段与原子新建报告的语义与其他命令一致，非法输入或目标已存在时退出 2 且不覆盖已有目标、不留下部分报告。

所有时间均为源时间轴上的整数毫秒，所有区间一律半开 `[start_ms, end_ms)`。同一条库内字幕由 `[source_id, cue_id]` 唯一标识。仅依赖标准库，全程不联网。

## 通用约定

- 根对象必须含字符串字段 `op`，取值 `collect`/`merge`/`settle`；未知 `op` 与未知字段均为输入错误。
- 用量包（bundle）是 `collect` 与 `merge` 的统一产物，也是可继续归并的输入形态：
  - `schema_version`：固定为整数 `1`；
  - `mode`：固定为 `"subtitle-cost"`；
  - `op`：固定为 `"bundle"`；
  - `programs`：节目条目数组，0..1000 项，按 `program_id` 的 Unicode 码点升序输出。
- 节目条目：
  - 有效节目严格含 `program_id`、`cost_center`、`withdrawn`（布尔，输出为 `false`）、`usage` 四个字段；
  - 撤回墓碑 `withdrawn` 为 `true`。撤回发生在用量到达之后时，墓碑保留该节目的 `cost_center` 与规范 `usage`，用于同 ID 内容冲突检测；撤回先于任何用量到达时为空墓碑，只含 `program_id` 与 `withdrawn`；
  - `program_id` 全库唯一，返工重交必须使用新 ID；`cost_center` 为 1..200 字符的非空标识；
  - `usage` 为 0..2000 行，键 `[source_id, cue_id]` 在单节目内唯一，按 `(source_id, cue_id)` 码点序排列。每行含 `source_id`、`cue_id`、原 cue 起止 `cue_start_ms`/`cue_end_ms`（正长整数）与 `intervals`；
  - `intervals` 为 1..2000 个正长半开整数区间，全部落在原 cue 起止内，输入不得重叠，规范输出为相接合并后的并集（按起点再终点排序）。
- 用量包不含字幕正文，也不含整份剪辑清单（EDL）。

## 第一步：collect（用量统计）

### 输入契约

根对象严格只含 `op`（`"collect"`）、`program_id`、`cost_center`、`conform` 四个字段。`conform` 是一份普通 `subtitle-conform` 输入对象（含 `sources`、`clips`、`cues`、`min_duration_ms`、`max_chars_per_second`），按普通模式的既有规则校验与对版。

### 统计规则

1. 复用普通对版，以**实际产生的精确碎片**为准：每个碎片是某个 cue 与某个 clip 在源时间上的正长交集，记录其 `[source_start_ms, source_end_ms)`。
2. 按 `[source_id, cue_id]` 把同一节目内全部碎片的源区间求并集：同节目重复剪入同一源位置只计覆盖一次；端点相接即合并。
3. 变速只改变成片（节目时间）时长，碎片始终带源时间，因此**变速不改变源用量**。
4. 普通 SRT 渲染会把舍入后零长的短碎片丢弃，但这些碎片仍在精确碎片集合内，其源区间照常计入用量。
5. 每行同时记录该 cue 的原起止 `cue_start_ms`/`cue_end_ms`；节目完全没剪到的 cue 不出现在用量包中。

### 输出

单个 `op="bundle"` 用量包，含一个有效节目条目。

## 第二步：merge（多级归并与撤回）

### 输入契约

根对象严格只含 `op`（`"merge"`）、`bundles`、`withdraw`：

- `bundles`：0..100 个子节点的数组（顶层允许为空，使撤回可以先于用量到达）。每个子节点要么是一个用量包（`op="bundle"`），要么是另一个 `merge` 节点（`op="merge"`，含自己的 `bundles`/`withdraw`）；嵌套 `merge` 节点的 `bundles` 至少 1 项，递归深度最大 100；
- `withdraw`：待撤回的 `program_id` 数组，0..1000 项，元素非空且在同一节点内不重复。归并树任意层级的 `withdraw` 都会被收集并应用；同一 ID 在不同层级重复撤回是幂等的。

### 归并规则

1. 递归展开整棵归并树，按 `program_id` 合并所有节目条目，与批次顺序、树层级无关，结果等价于一次性结算所需的有效用量。
2. 相同 `program_id` 且成本中心与规范用量逐字节相同的节目只计一次（任一层级、任意批次重送均幂等）。
3. 相同 `program_id` 但成本中心或规范用量不同，一律报错；**即使该节目已经撤回也报错**。先到的空墓碑没有内容，不触发内容冲突；一旦它吸收过同 ID 用量，后续不同内容的同 ID 包仍会被拒绝。
4. `withdraw` 永久压过同 ID 用量：标记撤回后，任何旧包重送都不能恢复该节目；撤回先于用量到达时建立空墓碑，后到的同 ID 用量被吸收并保持撤回状态。
5. 撤回按单个节目生效：输出保留每个节目的独立用量，不折算成成本中心总量，因此中心内其他节目的重叠覆盖在某个节目撤回后仍然保留。

### 输出

一个可继续归并的 `op="bundle"` 用量包，包含全部有效节目与全部撤回墓碑（含空墓碑），节目按 `program_id` 码点序排列。对同一输入反复归并（含把输出重新喂回）幂等。

## 第三步：settle（按覆盖权重结算）

### 输入契约

根对象严格只含 `op`（`"settle"`）、`bundle`、`fees`：

- `bundle`：单个用量包（`op="bundle"`），其中撤回墓碑的用量不参与结算；
- `fees`：0..2000 条报价，键 `[source_id, cue_id]` 唯一，重复键报错。每条含 `source_id`、`cue_id`、`cue_start_ms`、`cue_end_ms` 与 `fee_fen`；`fee_fen` 为 0..10^12 的非负整数（拒绝布尔值）。

### 校验

- 每个仍在使用（至少一个有效节目剪入）的 cue 必须有对应报价，否则报错；
- 报价的原 cue 起止必须与用量包中该键的原起止完全一致，不一致报错；不同节目对同一键记录的原起止不一致也报错；
- 同键表示库内同一 cue。

### 分账规则

对每个在用 cue，独立结算其整笔费用：

1. 先在每个成本中心内把该中心全部有效节目的区间求并集（同中心多个节目不重复计覆盖）。
2. 扫描全部事件点，把源时间轴切成若干段；对每一段，统计覆盖该段的**不同成本中心数** `k`，把该段长度等分给这 `k` 个中心，每个中心分得 `段长/k`。同中心无论多少个节目覆盖该段都只持一份份额。
3. 各中心把逐段份额累加得到精确有理权重 `weight_ms`（用 `fractions.Fraction` 计算，输出为精确有理数字符串，如 `"40"`、`"125/3"`）；按权重比例分配该 cue 的整笔费用，不按成片时长计费。
4. 分配金额逐中心向下取整；剩余的整分按小数余数从大到小补齐，余数相同（平局）时按成本中心 ID 的 Unicode 码点序优先。每个 cue 的分配合计严格等于该 cue 报价。

### 报告契约

原子新建单个 JSON 报告：

- `schema_version`（1）、`mode`（`"subtitle-cost"`）、`op`（`"settle"`）；
- `cues`：逐 cue 报告，按 `(source_id, cue_id)` 码点序排列。每项含 `source_id`、`cue_id`、`cue_start_ms`、`cue_end_ms`、`fee_fen` 与 `centers`；
  - `centers` 按成本中心 ID 码点序排列，每项含 `cost_center`、该中心在本 cue 的源覆盖并集 `coverage_intervals`、精确权重字符串 `weight_ms` 与整数 `allocated_fen`；
- `deferred_fees`：没有任何有效节目使用的报价（含对应 cue 只被撤回节目引用的情况），原样列出，按 `(source_id, cue_id)` 码点序；
- `center_totals`：每个在有效节目中出现的成本中心一行（即使本中心分到 0 分也列出），按中心 ID 码点序，含 `cost_center` 与跨 cue 累计的 `total_fen`；
- `summary`：`program_count`、`active_program_count`、`withdrawn_program_count`、`settled_cue_count`、`deferred_fee_count`、`allocated_total_fen`、`deferred_total_fen`。

分配总额与待摊总额之和必须等于全部报价之和（程序内断言对平）。

## 稳定输出顺序

- 节目：`program_id` Unicode 码点序；
- 节目内用量行：`(source_id, cue_id)` 码点序；
- 区间：起点升序，起点相同按终点升序，相接已合并、互不重叠；
- settle 的 cue 与待摊报价：`(source_id, cue_id)` 码点序；
- cue 内中心与中心汇总：成本中心 ID 码点序；
- JSON 键序由代码固定写入，报告以两格缩进并以换行结尾。

## 可运行示例

在仓库根目录执行，目标路径须不存在；重复演示使用新文件名。

```sh
.venv/bin/subtitle-cost --source fixtures/subtitle-cost-collect.json --output run/cost-collect.json
.venv/bin/subtitle-cost --source fixtures/subtitle-cost-merge.json   --output run/cost-merge.json
.venv/bin/subtitle-cost --source fixtures/subtitle-cost-settle.json  --output run/cost-settle.json
```

预期：

1. collect 输出 1 个有效节目；`caption-2` 因两次剪入产生两段不相邻源覆盖 `[4000,5000)` 与 `[7000,8000)`，重复/相接覆盖会被并成一段；
2. merge 的归并树含两个层级与三个节目，`prog-p` 被撤回，输出 3 个节目、其中 1 个墓碑；
3. settle 对报价 101 分的 `cue-x`，有效节目为 center-a 的 `[40,100)` 与 center-b 的 `[20,80)`，两中心精确权重均为 40，分配 51/50（平局余分按码点序给 center-a）；未被任何有效节目引用的 `cue-idle`（37 分）进入待摊；分配 101 加待摊 37 对平全部报价 138。

### 必测数值案例（cue 为 `[0,100)`，报价 101 分）

- 中心 A：节目 P 用 `[0,60)`、Q 用 `[40,100)`；中心 B：节目 R 用 `[20,80)`。先归并 P、R 再收入 Q：A 权重 70、B 权重 30，分 A 71、B 30；
- 撤回 P 后，A 的 `[40,60)` 仍由 Q 覆盖，两中心权重均为 40，分 A 51、B 50；
- 再并回最早的 P/R 包结果不变；把 P 换成另一成本中心的同 ID 包必须拒绝；
- 先撤回后到用量：用量不能恢复撤回状态；全部节目撤回：全部报价转待摊。

## 失败约定

源文件超过 8 MB、含重复 JSON 键、非有限 JSON 常量（`NaN`/`Infinity`）、根对象非对象、未知/缺失字段、布尔值冒充整数、非正长或越界区间、重叠区间、未知引用、重复键、归并嵌套超深、同 ID 节目内容冲突、在用 cue 缺报价或报价边界不一致等情况均退出 2，不写报告；输出目标已存在时退出 2 并保留旧文件，不产生临时残留。报告先完整校验与计算，再通过同目录临时文件原子硬链接新建；任何一步出错都不会留下部分报告。
