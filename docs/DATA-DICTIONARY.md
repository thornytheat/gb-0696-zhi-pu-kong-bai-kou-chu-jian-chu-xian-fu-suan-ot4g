# 质谱字段与规则

sample_id保留前导零。kind取standard、blank、sample、control。area与internal_area为峰面积。level为标样浓度，单位支持ug/L、mg/L、ng/L；统一为ug/L。dilution必须大于0。

每批每检测项至少3个独立浓度点。最小二乘使用Decimal，分母为零拒绝；斜率非正拒绝。空白取同批同项的浓度中位数。净浓度允许为负；不能提前截零。稀释后检出限=0.05×倍数。控制样相对误差超过20%阻断。超过标曲响应范围优先于其他质控判断。

重复编号相同内容标DUPLICATE，不同内容标KEY_CONFLICT。采信包括标曲和空白，最终样品数不等于输入采信数。行数对账独立于样品结果数。

同一批次同一分析项内sample_id唯一；相同样品编号可在不同分析项或批次中复用。

## 标曲版本与样品绑定

- 标曲按 (batch, analyte) 版本化：`curve_id = batch|analyte|vN`，版本历史只追加不覆盖。
- `standards_digest` 是标样浓度与响应的内容摘要（与行序无关）。同一摘要对应同一版本，因此重试同一次标样修订不会新增版本。
- 每个样品行绑定实际采用的 `curve_id`、`standard_version`、`standards_digest`，并附带该标曲的 slope/intercept/r2/max_response 供复核核对。
- 补齐标样后的批次通过 `parent_id` 继承上级批次：已复核样品按上级记录冻结（保留当时的浓度、限定符、原因码、标曲版本与结论），只有待复核样品切换到新版本。
- 曲线更新失败（点数不足、秩亏、斜率非正）时保留上级旧版本继续计算，事件记 `update_failed` 与 `retained_version`；无旧版可保留时批次阻断。
- 空白扣除在版本切换时按所绑定标曲计算，不跨版本混用。

## 复核

- 样品级复核结论保存在 `sample_reviews`，键为 `batch|analyte|sample_id`，含 decision、revision、curve_id、decided_at。
- 整批复核只写入待复核样品；指定 samples 可单独通过/退回，但必须携带当前 revision。
- 复核页面同时展示新旧标曲归属（版本注册表 + 版本事件）、样品绑定、限定符与批次结论；导出 CSV 保留相同字段。
