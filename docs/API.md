# 接口

`POST /api/run`：{"text":"CSV内容"}，可选 `parent_id`（同一分析批的上级批次，用于补齐标样）。同规则版本、上级批次与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`POST /api/review`：id、整数revision、decision取通过或退回；可选 `samples`（样品键 `batch|analyte|sample_id` 列表）。不传 samples 复核整批；传入时只更新指定样品。旧版本409；阻断项不能通过；不在可复核范围409。

`GET /api/export/{id}`：唯一证据CSV。含清单行（批次结论、标曲版本事件）、curve 行（每个标曲版本）、result 行（样品绑定的 curve_id / standard_version / standards_digest / 限定符 / 复核结论）与 quarantine 行。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413，上级批次不属于同一分析批422。逐行异常进入quarantine。

## 标样版本

- 每条标曲按 (batch, analyte) 保存全部历史版本 `v1..vN`，版本不覆盖。
- 版本由标样内容摘要 `standards_digest`（浓度+响应）识别；重试同一次标样修订复用同一版本，不新增版本号。
- 补齐标样产生新版本，只接管尚未复核的样品；已复核样品保持其当时 curve_id、浓度、限定符与结论。
- 曲线更新失败时保留旧版本继续出结果，并记录 `update_failed` 事件；无旧版可保留时批次阻断。
