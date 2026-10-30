# 配置与恢复

配置在用户主目录，按机器独立授权。`open_id` 与飞书应用有关，换应用时重新从实时文档或通讯录解析，不能共享另一应用下的 ID。

最小配置结构（示意值必须替换，不可直接运行）：

```json
{
  "schema_version": 1,
  "document_url": "https://example.feishu.cn/wiki/REPLACE",
  "document_id": "REPLACE_DOCX_TOKEN",
  "document_origin": "https://example.feishu.cn",
  "timezone": "Asia/Shanghai",
  "meeting_query": "项目组周会",
  "meeting_anchor": "REPLACE_MEETING_HEADING_BLOCK_ID",
  "periods": {"2026-Q4": "REPLACE_NATIVE_TABLE_BLOCK_ID"},
  "column_widths": [100, 187, 241, 460],
  "projects": [
    {"name": "专项 A", "owners": ["ou_REPLACE"],
     "fields": ["当前进展", "后续建设", "能力交付节点", "风险与依赖"]}
  ]
}
```

`periods` 明确日期所属年份，避免只按 `MM/DD` 跨年误判。已有表格按日期倒序排列、四列不合并。首次发现非标准文档时，由 Agent 用 `lark-cli docs +fetch --detail full` 核对表头、年份、锚点与负责人，再写本地配置。不要从名字推测 open_id。

## 中断恢复

1. 读取 `config.pending.json` 中日期和表格 ID，对该日期执行 `create --date`。脚本只在原 cell 序列恰好增加四个单元格时续填；已填成功的单元格跳过。
2. 若显示 uncertain，定向读取原生表格与对应日期行。无新增 cell、无目标日期，且原 revision 未执行时，可由 Agent 核对失败 API 响应后清理本地 pending，再重新创建。响应不明时不要仅凭超时重试插行。
3. 若日期行已经存在但有人修改了内容，保留人工编辑，核对结构和三个目标列后将本地 pending 归档到私有目录；不要删除远端行来迁就模板。
4. 新季度出现标题却没有完整表格时，先读回补齐该标题下的内容；不要再插入同名标题。多个候选交给用户选择。

## API 与验证范围

插行使用原生 `PATCH /open-apis/docx/v1/documents/:document_id/blocks/batch_update`，请求为 `insert_table_row.row_index`；每次带 `document_revision_id`、唯一 `client_token` 和 `user_id_type=open_id`。新行填充、资源引用使用 `docs +update` XML。CLI 无原生表格插行 shortcut，因此使用 `api`，没有绕过同等的 typed command。

官方规范：[批量更新块](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/batch_update)、[读取块](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/get)。

测试覆盖周日期与跨年、真实 @ 数据、重复运行、插行中断恢复、历史内容保留、并发版本冲突、多会议消歧、Note/Minutes 独立缺失与权限错误。单元测试使用合成数据；实际可访问性取决于当前用户权限与会议产物状态。
