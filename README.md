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

## 数据契约和边界

不打开媒体、不识别语音；输入结构化JSON字幕。时间裁剪保留整段文字，不自动按语义删词。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
