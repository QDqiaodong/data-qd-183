# 剪辑发布素材授权核验 v1（cut-clearance 模式）

`cut-clearance --source INPUT.json --output REPORT.json` 是独立离线命令，读取剪辑清单、发布目标、素材授权及经批准的例外，输出逐次素材使用的授权依据、未覆盖范围和每个发布目标的许可结论。它不读取媒体文件，不改变 `subtitle-conform` 及其 `--broadcast` 行为；输入解析、8 MB 上限、重复键/非有限常量拒绝、严格字段与原子新建报告的语义与原命令一致，非法输入或目标已存在时退出 2 且不覆盖已有目标。

## 输入契约

根对象严格只含 `sources`、`clips`、`deliveries`、`grants`、`exceptions` 五个字段。

- `sources`：至少 1 项，`id` 唯一，`duration_ms` 为 1..86400000 的正整数。
- `clips`：至少 1 项，数组顺序即 EDL 顺序；`id` 唯一，`source_id` 必须引用已知源，源区间 `[in_ms,out_ms)` 半开、正长且落在源时长内，`speed_num`/`speed_den` 为 1..1000 的正整数（拒绝布尔值）。同一素材可被多个 clip 重复引用，每个 clip 实例独立核验。
- `deliveries`：至少 1 项，`id` 唯一；`channel`、`territory` 为非空字符串，`release_at` 为带 `Z` 的 UTC RFC 3339 时间（允许小数秒，按精确分数解析，不接受无时区后缀或其他形式）。
- `grants`：可为空，`id` 唯一；`source_id` 引用已知源；`[start_ms,end_ms)` 为源时间半开正长区间；`channels`、`territories` 为非空、元素不重复的字符串数组；`valid_from`/`valid_until` 为半开有效期，`valid_until` 为 `null` 表示无到期日，否则有效期必须正长。
- `exceptions`：可为空，`id` 唯一；`delivery_id`、`clip_id` 必须引用已知目标与剪辑；`[start_ms,end_ms)` 半开正长，且必须完整落在该剪辑使用的源区间 `[in_ms,out_ms)` 内；`approved_by` 为非空标识，工具不认证审批人；`approved_at` 为 UTC RFC 3339 时间且不得晚于该目标的 `release_at`（相等合法）。

重复 ID、未知引用、非正长区间、越界例外、未知字段、缺失字段与格式错误均为输入错误。

## 核验规则

对每个发布目标与每个剪辑实例，先分别筛选**同时**满足下列条件的授权：

1. 授权 `source_id` 与剪辑同源；
2. 目标 `channel` 属于授权 `channels`；
3. 目标 `territory` 属于授权 `territories`；
4. 目标 `release_at` 落在半开有效期 `[valid_from,valid_until)` 内；`release_at` 恰等于 `valid_until` 时授权不适用，`null` 上界永不到期。

不匹配该目标的授权不得参与补齐，也不得跨目标拼接。把同一来源的全部适用授权区间求并集（端点相接即合并，覆盖只看正长区间，端点接触本身不构成覆盖），再与剪辑实际使用的源区间 `[in_ms,out_ms)` 求差，得到未授权缺口。

例外只按 `(delivery_id, clip_id)` 严格绑定：它只能覆盖指定发布目标、指定剪辑实例在源时间中的缺口，不得匹配同一素材的另一个 `clip.id`，也不得用于其他目标。仅当例外与当前缺口存在正长交集时才计入补缺，且只把相交部分计为例外覆盖；与缺口仅端点接触、或完全落在已授权范围内的例外不产生补缺。例外不得超出剪辑使用范围（输入阶段已强制）。剩余缺口 = 剪辑区间 −（适用授权并集 ∪ 有效例外相交部分）。

节目时间换算沿用对版口径：对剪辑声明的速度比 `scale = speed_den/speed_num`，源时间 `x` 对应节目时间 `clip_program_start + (x-in_ms)*scale`，全部用 `fractions.Fraction` 精确计算，不先四舍五入；每个剪辑实例的节目起点按 EDL 顺序累加。

一个使用没有剩余缺口（含被有效例外完全补足）时为 `cleared`，否则为 `blocked`；发布目标仅在其全部使用均为 `cleared` 时为 `cleared`，各目标独立得出结论。未被任何剪辑引用的授权（其来源未被任何 clip 使用）列入未使用授权清单，不影响任何发布结论。

## 报告契约

原子新建单个 JSON 报告，字段：

- `schema_version`（1）、`mode`（`"cut-clearance"`）；
- `deliveries`：按输入顺序排列，每项含 `delivery_id`、`channel`、`territory`、`release_at`（原样回显）、目标 `status`（`cleared`/`blocked`）与 `uses`；
- `uses`：按 EDL 顺序排列，每项含：
  - `clip_id`、`source_id`、整数源区间 `source_interval_ms`；
  - `program_interval_ms`：该剪辑在节目时间线上的精确有理数起止字符串；
  - `grant_ids`：对该目标/实例适用的全部授权 ID，按 Unicode 码点升序；
  - `exception_ids`：与缺口有正长交集、实际参与补缺的例外 ID，按 Unicode 码点升序；
  - `remaining_gaps`：剩余缺口数组，按源起点再终点排序；每项含整数 `source_start_ms`/`source_end_ms` 与精确有理数字符串 `program_start_ms`/`program_end_ms`；
  - `status`：`cleared`/`blocked`；
- `summary`：`delivery_count`、`cleared_count`、`blocked_count`、`gap_count`（全部目标剩余缺口区间总数）；
- `unused_grant_ids`：来源未被任何剪辑引用的授权 ID，按 Unicode 码点升序。

## 失败约定

源文件超过 8 MB、含重复 JSON 键、非有限 JSON 常量（`NaN`/`Infinity`）、根对象非对象或任何字段/引用/时间格式非法时退出 2，不写报告；输出目标已存在时退出 2 并保留旧文件，不产生临时残留。报告先完整校验与计算，再通过同目录临时文件原子硬链接新建。
