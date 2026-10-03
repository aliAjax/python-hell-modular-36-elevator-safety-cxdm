# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
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
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。

角色包括`viewer`、`admin`、`inspector`、`dispatcher`、`maintenance`和`duty`（值班员）。对账认领与上报只允许值班员（`duty`）提交，其他角色一律 403。

## 平台困人报警对账

城市应急平台每天推来困人报警清单，平台只交代“有哪些事件”（平台事件编号、设备编号、报警时刻），到场与完成时间一律以本地救援记录为准，对不上的留成差异。

- 平台事件按`asset_no`（设备编号）+ 报警时刻挂到本地救援任务：报警时刻归一化到 UTC，相差在容差（默认 5 分钟）内视为同一事件，挂到时刻最接近的救援任务；超出容差或找不到设备则留差异。
- 挂上后，本地缺失到场/完成时间，或平台给的时间与本地相差超过 1 分钟，记为差异；本地有救援任务而平台清单没有，记为“本地救援未上报”。
- 两名值班员同时认领同一台电梯的对账时，先处理的生效，后到的看到“已被认领”（409）。
- 本地报警、救援或设备状态一变，已认领的结论立即作废重算，结论版本递增并写入审计。
- 上报失败后保留已认领项，只重试没写进去的；状态持久化到 SQLite，重启后自动接着处理。

相关接口：

- `POST /api/platform-events`：推送当日平台报警清单，请求体`{"events":[...]}`，幂等（同一平台事件编号不重复入账）。
- `POST /api/reconciliations/claim`：值班员认领某台电梯对账，请求体`{"asset_no":"..."}`。
- `GET /api/reconciliations` / `GET /api/reconciliations/<id>`：查询对账结论。
- `POST /api/reconciliations/<id>/report`：值班员上报对账结论，只重试未写进平台的项。
- `GET /api/reconciliations/<id>/items`：查询该对账的出报项及状态。
- `POST /api/reconciliations/recover`：手动续跑所有未完成的上报（服务启动时也会自动续跑）。

## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
