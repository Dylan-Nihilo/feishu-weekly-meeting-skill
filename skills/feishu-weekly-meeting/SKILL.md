---
name: feishu-weekly-meeting
description: 为每周三、周五的飞书项目周会创建周报行，沿用四列表格、专项字段和真实负责人 @，会后通过用户授权定位 AI 智能纪要与妙记并登记到对应日期。用于新建本周周报、准备周三周五会议板块、会后同步纪要；不用于个人邮件周报。
---

# 飞书双周次会议周报

运行 `scripts/weekly.py`，默认补齐本周三、周五的周报行，并查询已结束会议的产物。使用 Python 3.10+ 与已配置应用的 `lark-cli`（已验证 1.0.89），支持 macOS/Linux。

## 首次绑定

配置默认保存到 `~/.config/feishu-weekly-meeting/config.json`，不放 Git 仓库。先检查文件是否存在；存在时直接复用，不重新询问文档或负责人。

未配置时，从用户提供的正式周报文档初始化：

```bash
python3 <skill-dir>/scripts/weekly.py init \
  --doc 'https://<tenant>.feishu.cn/wiki/<node>' \
  --meeting-query '<会议主题中的实际关键词>'
```

初始化从实时文档提取当前季度四列表格、列宽、专项字段和人员 open_id。源文档须有“会议纪要”一级标题，季度二级标题为 `Q1`～`Q4` 或 `YYYY Qn`。有多个匹配表格或缺负责人时停止，让用户选择候选；不要猜 ID。其他结构可按 [配置说明](references/configuration.md) 定向配置。

## 运行

用户说“运行”“准备本周周报”时：

```bash
python3 <skill-dir>/scripts/weekly.py
```

按配置时区计算本周星期三和星期五，按日期去重。新季度创建带年份的季度标题和原生四列表格。指定日期/周或仅创建：

```bash
python3 <skill-dir>/scripts/weekly.py create --date YYYY-MM-DD
python3 <skill-dir>/scripts/weekly.py create --week YYYY-MM-DD
python3 <skill-dir>/scripts/weekly.py run --dry-run
```

`--date` 只接受周三或周五；`--week` 接受该周任意一天。`--dry-run` 做远端读取与预览，不写飞书；默认配置已绑定时，用户调用即授权本次创建和回填，无需重复确认。

新行保留“日期 / AI 总结 / 摘要（妙记） / 关键结论”四列。关键结论保留专项名称、原生负责人 @ 和原有字段，具体进展留“待补充”。不要自动把上次进展复制成本次事实。用户给出本次确认事实时，再对相应专项做最小修订。

## 会后回填

```bash
python3 <skill-dir>/scripts/weekly.py sync --date YYYY-MM-DD
# 同一天有多场匹配会议，向用户展示候选后使用其选定的长 ID
python3 <skill-dir>/scripts/weekly.py sync --date YYYY-MM-DD --meeting-id '<long-id>'
```

脚本通过主题、当地日期定位已结束会议；多候选时停止，不默认选最近一场。读取真实 AI 纪要文档并验证妙记可访问，然后在 AI 总结列登记原生文档引用，在摘要（妙记）列登记妙记链接，沿用原表格式。保留人工关键结论，不自动重写。

AI 智能纪要与录制妙记是两条独立链路。未结束、产物未生成或无权限时，结果标为 `pending_*` / `partial_artifacts`；不填假链接或把缺失称为成功。后续再次运行即可补齐。没有启用 AI 总结或录制的会议不会凭空产生对应产物。本 skill 是按需运行，不会自行安装定时任务、加入会议或启动录制。

## 飞书授权

沿用用户身份，认证由 `lark-cli` 管理；不要自行保存或输出凭据。按本次缺失 scope 增量授权，禁止整域授权。核心 scopes：

| 操作 | scope |
|---|---|
| 文档读取 | `docs:document.content:read`、`docx:document:readonly` |
| 原生表格与段落更新 | `docx:document:write_only` |
| Wiki URL 解析（初始化时按 CLI 提示） | `wiki:node:read` |
| 已结束会议检索 | `vc:meeting.search:read` |
| 智能纪要元数据 | `vc:note:read` |
| 妙记标题与关联标识 | `minutes:minutes.basic:read` |

具体 CLI 若报其他精确 scope，以该操作返回的 `missing_scopes` 为准，仅申请所缺项。默认登记链接不需要下载音视频或读取妙记全部 AI 产物。

```bash
lark-cli auth check --scope '<本次所需 scopes>' --json
lark-cli auth login --scope '<missing scopes>' --no-wait --json
lark-cli auth qrcode '<原样 verification_url>' --output ./authorization.png
# 用户授权后
lark-cli auth login --device-code '<本次临时 device_code>' --json
```

授权链接与二维码一起展示，URL 在前；只在当前交互中使用 device_code，不写入仓库或日志。用户完成授权后由 Agent 收尾登录并重试原操作。

## 写入与恢复边界

- 写入使用新读到的 revision；历史表格行、列宽和表头回读比对。遇到冲突重新读取和诊断，不强行覆盖。
- 原生表格插行使用请求唯一标识；本地 pending journal 保留日期、原 cell ID 与版本，不含正文。中途失败可重运行同一天，先识别刚插入的行，再补齐空单元格。无法唯一识别时停止，按 [恢复说明](references/configuration.md) 处理，不再插行。
- 已有日期不重复创建；同一产物不重复登记；有人工文本或其他链接的单元格不替换。全表/全文 overwrite 不在此工作流内。
- 每次变更完成后追加配置旁的 `*.operations.jsonl`，只记录对象、日期、版本和结果。正文只在内存中处理。
- 若用户工作区有资产 manifest，远端验证后按其既有流程刷新目标版本和 operation log；不要把正文或逐字稿放进资产控制目录。

完成时报告文档链接、已建日期、@ 验证、回填情况与待生成/待授权项。不要把 `pending_meeting` 或已有行的 `exists` 当作本次新建成功。
