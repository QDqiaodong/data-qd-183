# data-qd-183 · 剪辑字幕对版

按照剪辑片段顺序重排、拆分和变速映射源字幕，保留精确时间和质量提示，并在JSON中提供SRT文本。

## 独立安装

Python >=3.11，实际验证使用本地CPython3.12.14。运行与unittest仅用标准库，不依赖外部服务。每仓建立独立虚拟环境；构建工具锁定版本，公共包缓存可复用。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

离线安装需要已安装的兼容Python及缓存中的setuptools==80.9.0、wheel==0.45.1。若缓存缺失，应先准备公共构建工具，不能把以上命令当空缓存可用承诺。修改源码后须重新安装本仓。

## 演示

在仓库根目录执行，目标路径须不存在；重复演示使用新文件名。

```sh
.venv/bin/subtitle-conform --source fixtures/input.json --output run/report.json
```

预期：输出时长4000ms、3个字幕碎片、3条SRT、0对重叠、1个有阅读提示的片段。

## 播出端单轨模式（broadcast）

加 `--broadcast` 切换为 25 fps 播出端单轨交付：输入根对象改为 `{"conform": <原输入>, "broadcast": {...}}`，在全部精确碎片（含旧 SRT 会舍掉的极短碎片）上枚举合法条目，按必保覆盖数、可选优先值、条目数、偏移量、字典序五级规则选出唯一方案，并逐片给出审核归属与未入选可选碎片的全局反事实解释。

```sh
.venv/bin/subtitle-conform --source fixtures/broadcast.json --output run/broadcast-report.json --broadcast
```

预期：状态 `ready`，同一句对白跨批准剪缝合并为一条 `[0,160)` 字幕，两个必保碎片都被条目 1 覆盖。台标/片名卡保护段、最小间隔、精确偏移与读速约束全部按精确有理数比较；必保无法全覆盖时输出 `blocked` 诊断报告且 `srt_text` 为 null。字段、反事实分类与报告结构见[播出契约](docs/broadcast-contract.md)。

成功退出0；业务质量提示或诊断不等于输入失败，见summary。非法输入或已有目标退出2，保留旧目标。JSON最多8MB，拒绝重复键、未知字段及非有限常量。先完整验证与计算，再同目录临时文件原子硬链接新建一个JSON报告。run/已忽略。

## 重新剪辑审校迁移（carry-review）

加 `--carry-review` 切换为重新剪辑后的逐次字幕审校迁移：输入根对象为 `{"previous": <原输入>, "current": <原输入>, "links": [...], "edits": [...]}`，只凭剪辑部提供的来源对应关系，把旧剪辑实例上批准的改词/隐藏决定迁移到新版字幕；原始正文已更正或候选结论不一致的区间标记 `stale_review`/`conflict` 并阻断交付，可交付时按迁移后的实显正文重新计算质检与 SRT。

```sh
.venv/bin/subtitle-conform --source fixtures/carry-review.json --output run/carry-report.json --carry-review
```

预期：状态 `ready`，6 段审校、5 条交付字幕、5 条 SRT、4 条质检提示，改词与隐藏两条旧决定完整迁移。判定规则、逐段清单、旧决定去向与交付字段见[审校迁移契约](docs/carry-review-contract.md)。`--carry-review` 与 `--broadcast` 互斥。

## 剪辑发布素材授权核验（cut-clearance）

独立离线命令 `cut-clearance` 按发布目标核验逐次素材使用：先按渠道、地区与 `release_at` 有效期筛选适用授权，在源时间轴上求并集后与剪辑源区间求差得到缺口，再用严格绑定目标与剪辑实例的已批准例外按正长交集补缺，最后给出逐使用 `cleared`/`blocked`、精确有理数节目时间缺口与目标级结论。它不读取媒体，不改变 `subtitle-conform` 或其 `--broadcast` 行为。

```sh
.venv/bin/cut-clearance --source fixtures/clearance.json --output run/clearance-report.json
```

预期：3 个目标中 `dl-tv-us` 为 `cleared`，其余 2 个 `blocked`，共 4 个剩余缺口；未引用的 `g-c-unused` 进入 `unused_grant_ids`；端点接触的例外不补缺，只批准给 `cut-3` 的例外不放行重复实例 `cut-4`，缺口节目时间保留 `17500/3` 等精确分数。字段、排序与失败约定见[授权核验契约](docs/clearance-contract.md)。

## 离线复盘播放质检（subtitle-playback-qc）

独立离线命令 `subtitle-playback-qc` 把一次采集中播放器的时钟表和显示日志同普通模式实际交付的 SRT 对账：按日志批次到达顺序重建可信显示状态（序号缺口、缺日志尾部、首快照之前都记未知，只有完整快照能重建信任），在已知区间内找出漏显/多显行号，并为每一批给出要撤回/要加入的完整告警行与未知、漏显、多显三项时长的有符号差值；质检台从「整段未知」的初始视图只回放这些更正即得该批规范结果。它不读取媒体，不改变既有命令行为。

```sh
.venv/bin/subtitle-playback-qc --source fixtures/playback-qc.json --output run/playback-qc-report.json
```

预期：3 批日志全部应用后最终 1 条告警 `[400,800)` 多显行 1，三项时长为未知 0、漏显 0、多显 400；首批缺第 3 条时 `[200,800)` 只记未知不算漏显，第二批补包撤回该未知告警并改为多显告警（未知 −600、多显 +400），第三批重发无更正。时钟覆盖、可信状态、行号对照、更正与计量规则见[播放质检契约](docs/playback-qc-contract.md)。

## 跨节目字幕译制费分账（subtitle-cost）

独立离线命令 `subtitle-cost` 处理供应商按源字幕一次性收取、财务须按各成本中心实际源用量分账的译制费，以输入的 `op` 区分三步：`collect` 在普通对版的精确碎片上统计单节目对各 `[source_id,cue_id]` 的源时间半开区间并集（同节目重复剪入只计一次、变速不改源用量、SRT 舍掉的短碎片仍计入）；`merge` 归并任意层级的用量包并永久处理节目撤回（同 ID 内容不同即报错，撤回可先于用量到达且旧包重送不能恢复，保留单节目用量而不折算中心总量）；`settle` 按每段上不同成本中心等分长度得到精确权重，逐 cue 用最大余数法分配整笔费用，未使用报价列待摊，分配与待摊对平报价。

```sh
.venv/bin/subtitle-cost --source fixtures/subtitle-cost-collect.json --output run/cost-collect.json
.venv/bin/subtitle-cost --source fixtures/subtitle-cost-merge.json   --output run/cost-merge.json
.venv/bin/subtitle-cost --source fixtures/subtitle-cost-settle.json  --output run/cost-settle.json
```

预期：collect 得到 1 个有效节目且 `caption-2` 保留两段不相邻源覆盖；merge 的三层归并后 3 个节目中 `prog-p` 为撤回墓碑；settle 对 101 分报价按两中心各 40 的精确权重分配 51/50，另把 37 分未使用报价列入待摊，101+37 对平全部报价。JSON 契约、稳定输出顺序、撤回墓碑与失败约定见[译制费分账契约](docs/subtitle-cost-contract.md)。

## 可重取的字幕复核工单（subtitle-review-queue）

独立离线命令 `subtitle-review-queue` 从固定版的普通对版输入登记待复核组，并在独立账本里记录领取、交回、完成和原样重取。同一源 cue 的多次出现、精确节目区间正长相交的行（含中间行无提示时的传递闭包）必须整组复核；组内只有出现时长/读速提示、舍入后丢弃或任意两行精确重叠时才派单。`issue` 按登记顺序把整组装进席位容量、不拆组；`resolve` 由主管整单完成或退回；成功提交的请求 ID 配相同请求会原样返回最初回执，先于当前状态判断。一个账本对应一个已锁定节目版，重新剪辑须换账本、不迁移旧决定。

```sh
.venv/bin/subtitle-review-queue --state run/review-state.json \
  --source fixtures/review-queue-register.json --output run/review-register.json
.venv/bin/subtitle-review-queue --state run/review-state.json \
  --source fixtures/review-queue-issue.json --output run/review-issue.json
.venv/bin/subtitle-review-queue --state run/review-state.json \
  --source fixtures/review-queue-resolve.json --output run/review-return.json
```

预期：登记 2 个待复核组——`["X","A"]` 四成员（X 上 A/B/C 与 Y 上 A）与变速剪辑中 SRT 消失的 `["Z","D"]`（精确区间 `[140,421/3)`）；容量 1 跳过四成员组领取第二组，容量 4 只领取四成员组。账本旁的锁文件保证跨进程互斥与完整快照；变更原子提交后才写报告，报告写失败退出 3，用原请求和新输出路径重取。字段、请求/响应契约与失败约定见[复核工单契约](docs/review-queue-contract.md)。

## 数据契约和边界

不打开媒体、不识别语音；输入结构化JSON字幕。时间裁剪保留整段文字，不自动按语义删词。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
