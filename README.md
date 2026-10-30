# Feishu Weekly Meeting Skill

每周三、周五项目周会的飞书周报自动化：创建日期行、保留专项结构和负责人 @，会后把 AI 智能纪要与妙记登记到原生四列表格。

```text
日期 | AI 总结 | 摘要（妙记） | 关键结论
```

重复运行按日期和产物去重；有人工内容的单元格不自动覆盖。新季度自动建立带年份的季度板块。实际进展留待负责人补充，不沿用上次进展冒充本次结论。

## 安装

需要 Python 3.10+、macOS/Linux 和已完成应用配置的 `lark-cli`。CLI 1.0.89 已验证。

```bash
git clone https://github.com/Dylan-Nihilo/feishu-weekly-meeting-skill.git
mkdir -p ~/.codex/skills
cp -R feishu-weekly-meeting-skill/skills/feishu-weekly-meeting ~/.codex/skills/
```

首次向 Codex 提供正式周报地址和真实会议主题关键词：

> 使用 $feishu-weekly-meeting 绑定这份飞书周会文档，会议主题是“项目组周会”，准备本周三和周五的板块。

之后直接说：

> 使用 $feishu-weekly-meeting 运行本周周报。

> 使用 $feishu-weekly-meeting 同步今天会后的 AI 总结和妙记。

## 命令行

```bash
python3 skills/feishu-weekly-meeting/scripts/weekly.py init \
  --doc 'https://<tenant>.feishu.cn/wiki/<node>' --meeting-query '<会议主题关键词>'

# 一次运行：补齐本周三/周五，再同步已结束的会议
python3 skills/feishu-weekly-meeting/scripts/weekly.py

# 只创建、预览、指定日期回填
python3 skills/feishu-weekly-meeting/scripts/weekly.py create
python3 skills/feishu-weekly-meeting/scripts/weekly.py run --dry-run
python3 skills/feishu-weekly-meeting/scripts/weekly.py sync --date YYYY-MM-DD
```

配置默认保存在 `~/.config/feishu-weekly-meeting/config.json`。仓库不包含真实文档地址、人员 ID、会议内容或认证信息。权限按当前命令增量授权，流程见 [SKILL.md](skills/feishu-weekly-meeting/SKILL.md)。

会后回填沿用原表格式：AI 总结为原生纪要文档引用，妙记为链接。查询会读取真实智能纪要并确认妙记可访问。尚未生成或无权限的产物明确标为待处理，重新运行即可补齐。多场候选会议需先选择。

这是按需运行的 skill，不会自动加入会议、开启录制或安装定时任务。会议需已启用相应产物。若要定时执行，可另行配置本地任务调用同一命令。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用合成数据，不访问飞书。配置结构、原生接口和中断恢复见 [配置说明](skills/feishu-weekly-meeting/references/configuration.md)。
