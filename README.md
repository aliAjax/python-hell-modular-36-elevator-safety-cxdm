# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/reconcile.py`：平台事件与本地报警/救援任务的对账匹配、差异原因与指纹。
- `src/platform_sink.py`：对账结论回传平台的出站通道（可注入失败模拟）。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/platform-events`：值班员导入城市应急平台困人事件清单（`{"events":[...]}`）。
- `POST /api/platform-events/<id>/claim`：值班员把平台事件对账认领到本地救援任务。
- `POST /api/reports/flush`：把认领结论上报平台；只重试还没写进去的项。
- `GET /api/reports`：查上报状态，可用`?status=pending|sent`过滤。
- `GET /api/platform_events`、`GET /api/reconciliations`：查询平台事件与认领结论。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制。

## 平台事件对账认领

- 平台只提供事件清单（`event_ref`、设备编号`asset_no`、报警时刻`alarm_at`），不提供到场/完成时间；`(source, event_ref)`幂等，重复推送不重复建。
- 认领时按**设备编号 + 报警时刻**挂本地记录：同设备下取与平台报警时刻相差不超过300秒（`alarm_window_seconds`可调）的最近本地报警，再取其最后一条救援任务。
- 到场时间（救援`arrive`自动记录`arrived_at`）和完成时间（`complete`自动记录`completed_at`）一律以本地记录为准。
- 挂不上（无设备/无本地报警/无救援任务）或本地记录不完整（未到场、未完成、时间偏差超窗）一律保留为`discrepancy`并给出`reasons`，不猜测补全；全部对齐才是`matched`。
- 两名值班员并发认领同一事件：`claim_owners`行锁 + `BEGIN IMMEDIATE`保证先提交的生效，后到者收到409（含已认领人）。
- 认领只允许值班角色（`dispatcher`/`admin`），其他角色导入或认领一律403。
- 本地报警、救援任务、设备记录一旦新建或状态变化，关联的已认领结论**立即作废重算**（认领人和认领关系保留，`recompute_count`累加，审计留痕）；启动时`refresh_claims`再按指纹兜底扫描库外改动。
- 上报走`report_outbox`：认领即入队；失败保留认领项，只重试`pending`，重启后接着发；`sent`项不重发。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
