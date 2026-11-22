# 可重取的字幕复核工单 v1（subtitle-review-queue）

`subtitle-review-queue --state STATE --source INPUT.json --output REPORT.json` 是独立离线命令，把普通对版报告里的待复核行变成可派给多个复核席位的工单账本。它只读写 `STATE` 指定的账本文件，登记过之后不再需要原始输入文件；不读取媒体、不联网、不改变 `subtitle-conform` 及既有命令、报告或服务入口的行为。输入解析沿用仓库的严格 JSON：最多 8 MB、拒绝重复键与非有限常量、拒绝未知字段和非法类型（含布尔冒充整数）。成功响应原子新建一个 JSON 报告，不覆盖已有报告。

**一个账本对应一个已锁定节目版。** 重新剪辑必须使用另一个 `--state` 账本，旧账本里的决定不迁移。

## 输入与响应

根对象的 `op` 取 `register`、`issue`、`resolve`、`status`。

### register

```json
{"op": "register", "conform": { "sources": [...], "clips": [...],
  "cues": [...], "min_duration_ms": 0, "max_chars_per_second": 1000 }}
```

- `conform` 是一份普通 `subtitle-conform` 输入，按普通模式的既有规则校验并计算对版；普通对版只在这里算一次，并写入账本。
- 账本不存在时创建；已存在且内容不同则报错（退出 2），已锁定的内容不能被替换。同一份登记输入重送返回最初回执。
- 回执给出 `group_count` 以及每个待复核组的 `group_id`、`member_count` 和完整 `members`。

每个成员保留：

| 字段 | 含义 |
| --- | --- |
| `fragment_id` | `[clip_id, cue_id]`，精确碎片身份（不用 SRT 行号） |
| `source_id` | 源素材 ID |
| `source_start_ms` / `source_end_ms` | 该次出现的源半开区间 |
| `start_ms` / `end_ms` | 精确节目区间，有理数字符串 |
| `text` | 正文 |
| `chars_per_second` | 普通对版阅读速度，有理数字符串 |
| `issues` | 普通对版提示，`short_duration` / `reading_speed` |
| `srt_dropped` | 是否因毫秒舍入而未进入普通 SRT |

### 待复核组的形成

1. **同源同 cue：** 同一 `[source_id, cue_id]` 的所有实际出现必须一起复核。
2. **精确重叠：** 精确节目区间有正长交集的两行必须一起复核；端点接触不算重叠。

两条规则各自传递闭合，再取并集闭包；闭包推导不依赖中间行自身是否有阅读提示，也不按正文相同推断同一句，更不使用报告里截断到 100 条的重叠示例。整组只要满足任一条即入账：

- 组内任一行有时长或阅读速度提示；
- 组内有舍入后未进入普通 SRT 的行；
- 组内任意两行精确重叠。

无此类问题的孤立行或重复组不派单。

- 成员按普通对版的精确排序 `(start, end, clip_id, cue_id)` 排列；
- 组 ID 取首成员的 `fragment_id`；组顺序按首成员在普通对版里的位置。

### issue

```json
{"op": "issue", "request_id": "seat-alice-1", "reviewer": "alice",
 "max_fragments": 4}
```

- `max_fragments` 为 1..2000 的整数，是该席位本次可处理的精确碎片数。
- 按登记的组顺序扫描当前待领组：整组能放进剩余容量就领取，放不下就跳过并继续，不拆组；一次领取的组全部归入同一工单。
- 工单 ID 即为 `request_id`；回执带 `ticket_id`、`reviewer`、`max_fragments`、实际领取的 `fragment_count`、`groups`（含每个成员的完整详情）与 `empty: false`。
- 没有可领组时返回 `empty: true`、`ticket_id: null`、`groups: []`，并把这空回执记入账本以便重取；它不产生可处置工单。

### resolve

```json
{"op": "resolve", "request_id": "supervisor-return-1",
 "ticket_id": "seat-alice-1", "action": "return"}
```

- `action` 为 `complete` 或 `return`：主管确认整单已复核完，或要求整单退回重新派发。它不改字幕。
- 只允许处置当前仍有效的非空工单，一次处理其全部组。`complete` 后各组永久为已完成；`return` 后各组回到待领。两种动作都关闭工单。
- 回执给出被处置的 `ticket_id`、`action` 和受影响组。
- 若这些组已被退回并重新派出，它们现在归属别的工单；旧工单即使尚未关闭也不能处置新归属。
- 对已关闭工单、不存在的工单或用旧工单处置新归属均报错（退出 2），账本不变。

### status

```json
{"op": "status"}
```

不变更账本，按登记顺序给出每组 `group_id`、`member_count` 与当前 `state`（`pending` / `assigned` / `completed`），`assigned` 时附当前 `ticket_id` 与 `reviewer`。它反映现在的派工，不从旧回执推断当前归属。

## 请求身份与重取

- `issue` 与 `resolve` 共享账本内的请求 ID 空间。
- 成功提交过的 `request_id`，配相同请求（JSON 对象键顺序不计）必须返回最初的完整回执，不重新做事；先于任何当前状态判断，包括重取已关闭工单、原先是空回执的请求。
- 同一个 `request_id` 用于不同请求（字段不同、换席位、换容量、换动作等）则报错。
- 重取不是恢复工单效力：旧回执只说明当时发了什么，当前归属以 `status` 为准。
- 失败且未提交的请求不占用 ID；校验失败、业务拒绝、提交前写盘失败都不留下占用。

## 多进程与提交边界

- 账本是一个 JSON 文件，变更通过同目录临时文件加 `fsync` 后 `os.replace` 原子替换；读取整份文件。账本旁的 `<STATE>.lock` 哨兵提供跨进程互斥：读共享锁，变更排他锁。不同席位可用独立进程操作同一账本。
- 一次变更提交时，请求身份、可重取的回执、请求 ID 占用和全部组变更在同一份快照里一起生效；提交前失败不会留下部分领单或完成。
- 竞争领取时同一组不能同时属于两张有效工单。
- 报告只是回执的交付方式，不是事务提交标志。账本提交后若写报告失败（例如目标已存在），命令**以退出码 3** 结束并说明变更已提交；调用方须用原请求和新的 `--output` 路径重取，不得取消已提交的业务变更。重取返回最初回执。
- 请求非法、账本不存在或业务状态不允许时**退出码 2**，账本不变、不写报告。成功时**退出码 0**。

## 稳定输出顺序

- 组：首成员在普通对版精确排序中的位置；
- 成员：普通对版精确排序 `(start_ms, end_ms, clip_id, cue_id)`；
- `status` 的组按登记顺序；
- 精确区间以有理数字符串输出，不丢失变速造成的分数；
- JSON 按键序由代码固定写入，两格缩进并以换行结尾。

## 可运行示例

在仓库根目录执行，目标路径须不存在；重复演示使用新文件名与新账本。

```sh
.venv/bin/subtitle-review-queue --state run/review-state.json \
  --source fixtures/review-queue-register.json --output run/review-register.json
.venv/bin/subtitle-review-queue --state run/review-state.json \
  --source fixtures/review-queue-issue.json --output run/review-issue.json
echo '{"op":"status"}' > run/review-status-request.json
.venv/bin/subtitle-review-queue --state run/review-state.json \
  --source run/review-status-request.json --output run/review-status.json
.venv/bin/subtitle-review-queue --state run/review-state.json \
  --source fixtures/review-queue-resolve.json --output run/review-return.json
```

预期：

- 登记得到 2 个待复核组：`["X","A"]` 含 4 个成员（`X/A`、`X/B`、`X/C`、`Y/A`），`["Z","D"]` 含 1 个成员；
- `max_fragments: 4` 的 Alice 工单只领取第一个四成员组；
- 变速剪辑里精确节目区间 `[140, 421/3)` 的 `Z/D` 在普通 SRT 中消失（`srt_dropped: true`），但作为第二组登记；
- 以容量 1 领取会跳过四成员组，领取 `["Z","D"]`；
- `status` 显示一领一待；`return` 后第一组回到待领。

## 验收要点

1. 素材 S 上剪辑 X 用源 `[0,100)`、紧接着 Y 重用 `[0,40)`，均原速；cue A/B/C 为 `[0,45)`、`[40,80)`、`[70,90)`。X 上 A 与 C 从不重叠、Y 上只有 A，但 A/B/C 三段连续重叠且 A 跨 X/Y 两次出现，闭包仍把 X 上 A/B/C 与 Y 上 A 合成一组。
2. 另一素材的变速剪辑里独立 cue 的精确节目区间为 `[140, 140+1/3)`：`end_ms` 为精确 `421/3`，普通 SRT 中消失，仍作为第二组派单。
3. 仅端点相接、整组无提示且无舍入丢弃时不派单；同源同 cue 的重复组若也无提示、无重叠则不派单。
4. 101 组独立重叠对时报告只保留前 100 个重叠示例，分组仍覆盖全部 101 组。

## 失败约定

未知/缺失字段、非法 `op`、布尔冒充整数、容量越界、空席位或空 ID、未知工单、已关闭工单、旧工单处置新归属、同 ID 不同请求、账本不存在、账本格式不支持、登记内容与已锁定账本不同、输入超过 8 MB、重复 JSON 键或非有限常量，均退出 2 且不改账本、不写报告。账本提交后的报告写入失败退出 3，账本保持已提交状态。
