# 字幕剪辑对版 v1

sources定义唯一源片id和正整数duration_ms≤86400000；clips列表的输入顺序就是新节目拼接顺序，唯一id、source_id、半开[in_ms,out_ms)及speed_num/speed_den（正整数≤1000）表示播放速度。片段可重排、重复使用或源时段重叠；输出片段始终首尾顺接，不支持转场、叠画或输出时间另行定位。原片只用时长及身份，不打开媒体文件。

cues为唯一字幕id、source_id、半开[start_ms,end_ms)、1..2000字符text。只接收已结构化JSON，不解析任意SRT/ASS。时间严格整数毫秒、范围在源片内。正文允许换行但拒绝CR、控制字符、空行/首尾换行，保留正常文字。最多50源/100片段/500字幕，输出碎片最多2000，超过则失败。JSON字段严格检查。

每字幕与同源每clip取正长交集，端点仅接触不产生碎片。映射到输出：clip累计输出起点+(源时间-in)*speed_den/speed_num。Fraction保持精确有理数毫秒，重复clip产生独立[clip_id,cue_id]复合身份；不把字幕全文按字裁剪，即使时间被剪也保留全文。omitted_cue_ids明确列出一次都未使用的字幕。输出按时间、片段ID、字幕ID排序。

完整精确时间留在fragments中；srt_text字段按最近整数毫秒half-up独立量化起止，量化后零长的片段只从SRT省略并列出srt_dropped_fragments，不能丢失精确fragment。阅读速度是非空白Unicode代码点/精确秒，结合min_duration_ms和max_chars_per_second给出质量提示；不模拟屏幕字宽或语义断句。输出精确时间有正长交集即算重叠，overlap_count给总数，overlap_examples最多100对；重叠不自动合并、不覆盖字幕。质量告警不当输入错误。

srt_text是JSON里的可导出文本，CLI仅原子新建一个JSON文件，避免跨文件半发布。非法输入或已存在目标退出2，保留旧文件。没有媒体编码、语音识别、自动翻译或节目版本审批。
