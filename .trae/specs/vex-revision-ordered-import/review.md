# VEX 导入按文档身份与修订顺序发布 - 独立审查

- [x] CP-R1: 旧修订拒绝/同修订幂等零副作用（三格式）
  - **Type**: `rule`
  - **Covers**: AC-2、AC-3
  - **Evidence**: R2/R3 独立实跑：三格式拒绝/幂等用例含 obs 字段、FK、日志、push=0 与消息内容快照；全量 1623 OK

- [x] CP-R2: 同修订异内容与跨类型冲突被 4xx 拒绝且状态不变
  - **Type**: `rule`
  - **Covers**: AC-4、FR-5
  - **Evidence**: OpenVEX/CSAF/CycloneDX 冲突 + 跨类型冲突用例（test_vex_import_revisions.py）经 R3 逐一读码实跑

- [x] CP-R3: 较新修订原子替换，Observation 只反映新修订且每个受影响 Observation 至多一条日志、通知不重复
  - **Type**: `rule`
  - **Covers**: AC-5、FR-9~FR-12
  - **Evidence**: R1 F-1 双日志缺陷已修复并经 R2 /tmp 三场景实证、R3 回归确认；复合修订用例逐 obs 断言 0/1/1/1 日志、2 次 push；手动删除信号回归用例通过

- [x] CP-R4: 解析失败/数据库异常回滚后上一修订完整保留、无下游动作
  - **Type**: `rule`
  - **Covers**: AC-6
  - **Evidence**: 解析失败零写入 + bulk_create 注入 IntegrityError 全快照复原；on_commit 机制审查确认回滚不触发下游

- [x] CP-R5: 并发/乱序（含唯一约束竞争）收敛到最高确认修订
  - **Type**: `rule`
  - **Covers**: AC-7
  - **Evidence**: 乱序 1→3→2 终态=3；6 个保存点竞争分支（含两轮全败 4xx）；has_select_for_update 能力标志 SQLite False/MySQL·PG True 实测。真实 MySQL/PG 交错为已知残留风险

- [x] CP-R6: 全新导入与三格式字段语义不变、迁移可正向应用、历史空哈希平滑升级
  - **Type**: `rule`
  - **Covers**: AC-8、AC-9
  - **Evidence**: 既有夹具测试零修改、全量 1623 OK；全新 sqlite 库真实迁移链 0011 OK；makemigrations --check 无缺失；空哈希回填用例通过

- [x] CP-U1: 解析/发布职责分离、事务边界、跨数据库兼容、类型与代码质量
  - **Type**: `rubric`
  - **Covers**: AC-1、AC-10
  - **Scale**: 1-5
  - **Anchors**: 1 = 仍有先删后建窗口/专有锁/解析器含写入；3 = 功能正确但事务分散或有兼容缺陷；5 = 纯解析+单一事务+能力锁+三格式共用发布器且类型/门禁全绿
  - **Pass Threshold**: >= 4
  - **Evidence**: R3 评分 5/5；black 26.5.1/isort 9.0.1/flake8 7.4.1 全绿、pylint 10.00/10、mypy 46 文件 0 问题；9 个改动文件经 Python 3.12 语法解析验证

## Review History

### Review R1
- **Result**: `fail`
- **Evidence**:
  - 独立代理（fresh context）执行：重构镜像全量 Ran 1617 tests → OK；热挂载子集 316 → OK；迁移正向执行 OK；能力标志与 on_commit 语义经容器实证。
  - 容器内实证 major 缺陷 F-1：v1→v3 删除声明时被影响 Observation 产生 2 条相同 "Removed VEX statement" 日志（delta=2），并会重复注册通知任务。
  - F-2 经 Python 3.14.7 与语法级核查确认（PEP 758 无括号 except，3.12/3.13 SyntaxError）。
  - 详见审查报告（本会话记录）与下方 Issues。
- **Findings**:
  - F-1/I-1: `actionable` major；correctness/atomicity；[vex_publisher.py:178-186](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_publisher.py#L178-L186) 回退分支调用 `write_observation_log_no_vex_statement` 前未置 `vex_statement=None`，旧 statement 删除时 pre_delete 信号二次处理 → 双日志+重复通知，违反 FR-10/FR-12/AC-5。
  - F-2/I-2: `actionable` minor；portability；[vex_candidate.py:155](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_candidate.py#L155) 无括号多异常语法仅 Python 3.14 支持，与 requires-python >=3.12 冲突。
  - F-3/I-3: `actionable` minor；test-gap；静默重指向（同内容换修订 0 日志 0 push）与复合修订（保留/变化/删除/新增同次发布）缺直接断言；删除/新增场景缺日志条数断言。
  - F-4/I-4: `actionable` minor；test-gap；CycloneDX 同版冲突用例缺失、CSAF/CycloneDX 旧修订拒绝缺 obs 字段/FK/push 快照、跨类型冲突分支零覆盖。
  - F-5/I-5: `advisory`；胜方回退后的第二次 create 未再包保存点，第三竞争者再撞唯一约束会 500（概率低，FR-13 仅要求一轮；可廉价加固为最多两轮重试）。
  - F-6: `advisory`；导入匹配优先级由旧实现的无序多遍改为确定性 BOM→product→component 单遍，极端重叠构造赢家可能翻转；purl 规范小写且旧实现本身不确定，实际影响极小，不修改。
  - F-7: `advisory`；select_related 未含 vex_statement（N+1）、跨模块引用私有 `_get_search_purl`、非法日期文案细化、历史空哈希同版异内容只能静默替换（FR-14 已知边界）。前两项顺手修复，后两项保留并说明。
- **Resume When**: 不适用（缺陷可直接修复）

### Review R2
- **Result**: `fail`
- **Evidence**:
  - 全新代理独立复跑：重构镜像全量 1621 tests OK；black/isort/flake8/pylint 全绿；F-1 经 /tmp 临时用例三场景实证 resolved（手动删除/单删/发布回退均恰好一条日志、日志 FK 终态为 None）；F-3/F-4/F-5/F-7 resolved；F-6 经独立分析接受（与 VEX_Engine 既有优先级一致，旧多遍无序路径反不满足 FR-10）。
  - 遗留：F-2 实际未修复——black（target-version py314）把 `except (TypeError, ValueError)` 回写为 PEP 758 无括号写法，3.12/3.13 启动期 SyntaxError（tasks 状态与工作区不符）；N-1 极端竞争裸 IntegrityError → 500（advisory）；N-2 缺手动删除信号回归与文档主键不变字面断言（advisory）。
  - AC 矩阵：AC-1~AC-9 通过，AC-10 因 F-2 暂评 4/5。
- **Findings**: I-1~I-5 复核 resolved；F-2 未闭环（升级判定为 major，启动链影响）；N-1、N-2 见 tasks.md 增补处理。
- **Resume When**: 不适用（已修复，待 R3 确认）

### R2 问题修复（R3 待审）
- F-2：[vex_candidate.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_candidate.py) 改为两个独立 except 子句（black 不会合并、3.12 兼容）；全部 9 个改动文件经容器 `ast.parse(feature_version=(3,12))` 验证 ALL 3.12 PARSE OK；black --check 通过。
- N-1：两轮竞争均落败且胜方均回退时改抛 `ValidationError`（4xx，提示重试），新增 `test_unique_race_all_competitors_roll_back_returns_4xx`。
- N-2：新增 `test_manual_document_deletion_logs_removal_exactly_once`（手动 document.delete 恰好一条移除日志、FK None、状态回退）；原子替换用例补充文档主键不变字面断言。
- 验证：子集 322 tests OK；black/isort/flake8 干净、pylint 10.00/10、mypy application/vex 46 文件 0 问题；最终重构镜像全量 **1623 tests OK**（CI 等价 --build）。

### Review R3
- **Result**: `pass`
- **Evidence**:
  - 全新代理三重证据闭环 F-2：11 个改动文件 `ast.parse(feature_version=(3,12))` 全部通过（含 PEP 758 阴性对照）；black 26.5.1 --check 61 文件无改写，并以对照样本实证两个独立 except 子句形式不会被回写；全量 1623 OK。
  - N-1：两轮竞争全败抛 ValidationError(400)，`test_unique_race_all_competitors_roll_back_returns_4xx` 经判定为真实覆盖。
  - N-2：手动删除级联信号探针实证恰好 1 次 pre_delete、一条日志、日志 FK=None；文档主键不变字面断言到位。
  - F-1 无回归：三调用方语义核实，复合修订/删除场景测试通过。
  - 门禁独立复现：black/isort/flake8 clean、pylint 10.00/10、mypy 46 文件 0 问题；真实迁移链 + makemigrations --check 通过；AC-1~AC-10 全部 PASS（AC-10 5/5）。
- **Findings**: 无 critical/major；仅 INFO-1（`except TypeError` 为防御性不可达分支，保留）；F-6 维持接受。
- **残留风险**: 真实 MySQL/PostgreSQL 行锁交错未在 SQLite 实测（规格内已知）；FR-14 空哈希同版异内容静默替换为既定边界；外部通知系统未联调（mock + on_commit 语义覆盖）。
- **Blocked By**: 无；检查点全部勾选，审查结束。
