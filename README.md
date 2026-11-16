# 炭素材料证据与产能台账

本地 FastAPI 服务记录生产批次、实验室检验、发运、产线产能、客户订单与交付证据。所有示例记录均为合成数据，不连接真实工厂。

## 运行

在已准备好锁定依赖的 Python 环境中执行 uvicorn carbon_trace.api:app，通过 CARBON_TRACE_DB 指定 SQLite 文件。接口说明见 /docs。执行 python -m carbon_trace.cli demo --fixture fixtures/demo.json 可运行隔离的合成数据演示，输出日报、版本与对账结果。

## 数据约定

日期为 YYYY-MM-DD，事件时间必须带时区。工厂时区决定生产与发运所属日期，数量采用十进制吨。来源与外部事件编号组成不可变身份；完全相同的重试视为重复，不同内容的重试产生冲突拒绝。批次编号也具备幂等性，错误行不会回滚同批有效行。

登记工厂、产线和等级标准后提交生产批次。每个等级的生效区间不得重叠；每次新增或修订都会生成不可变的“标准修订”（spec_revision）及完整生效时间线，任意日期只解析出一条标准。质量分析使用生产当日标准和该批次最新检验，区分未检验和检验不合格。准备发布时固定数据修订号与发布头以及当时的等级标准快照；如其间新增事件、竞争版本或标准修订，原候选失效，已发布数据与当时标准保持不变。

补发或追溯的等级标准通过 `POST /specifications/{grade}/amendments` 修订：以 `valid_from`/`valid_to` 在现有区间内切出被更正的一段，其余时间线保持不变；`POST /specifications/{grade}/amendments/preview` 可在不落库的情况下得到同一份影响分析。修订会记录原因、前后修订号、受影响批次（`previous_status`/`current_status`/原因）、已发运或已预约的批次及其风险标记，以及使用旧标准的已发布版本；这些信息同时写入审计时间线和 `GET /specifications/{grade}/timeline`。历史修订永远可读，`GET /lots/{lot_id}/quality/history` 可按修订逐条重放某批次的判定，已发布版本 `GET /versions/{version}` 保留 `specifications` 快照。

实时视角始终采用当前标准：新标准导致不合格的批次在库存中计入 `held_tons`（从 `available_tons` 扣除），订单的 `at_risk_reservations`、预约分配与产能预测都会反映这些风险，且此类批次不能再被预约。旧批次与旧发布记录本身不被改写。

订单预约只接受检验合格批次，且不得超过订单需求或批次余量。取消订单或释放预约后可重新使用余量。事故标记显示在日报；交付核收与物理发运量分别统计。

## 核心接口

使用 POST /plants、POST /lines、POST /specifications 和 POST /batches 建立数据。日报通过 GET /plants/{plant_id}/report 查询。标准修订通过 POST /specifications/{grade}/amendments 应用，GET /specifications/{grade}/timeline 查看全部修订与影响。先从 GET /status 取得修订号与发布头，再调用 POST /candidates 和 POST /candidates/{candidate_id}/publish。历史版本通过 GET /versions/{version} 查询。其他接口提供库存、质量、溯源、对账、产能预测、订单、预约、事故和交付核收。错误响应包含稳定代码与原因。
