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

## 数据契约和边界

不打开媒体、不识别语音；输入结构化JSON字幕。时间裁剪保留整段文字，不自动按语义删词。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
