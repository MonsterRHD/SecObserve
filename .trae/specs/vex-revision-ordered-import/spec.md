# VEX 导入按文档身份与修订顺序发布 - 需求规格

## Overview
- **Summary**: 重构 OpenVEX、CSAF、CycloneDX VEX 文档的导入路径，使三种格式都按“文档身份 + 修订顺序”发布：旧修订拒绝、同修订幂等或冲突拒绝、新修订在全部解析与校验通过后一次性原子替换，并保证并发/乱序导入最终收敛到最高已确认修订。
- **Purpose**: 安全团队的多条流水线会重复或乱序上传同一份 VEX 文档。现有导入先删除已保存文档（级联删除 statements，触发观察结果回退日志）再重建，导致已按新修订收敛的 Observation 状态、关联声明、评估日志和下游（通知/Issue Tracker）结果被迟到的旧文件打回；重复上传同一文件还会产生无意义的日志与下游动作。
- **Target Users**: 安全运营 / 流水线集成（通过 `/api/vex/import/` 上传 VEX 文档的超级用户与自动化任务）。

## Goals
- 按文档身份（`document_id` + `author`，类型必须一致）识别同一份 VEX 文档的不同修订。
- 按各格式标准的版本/发布时间字段建立全序比较：合法但较旧的候选不得覆盖当前修订，接口返回 4xx 明确拒绝。
- 相同修订：内容一致 → 幂等重放（零写入、零日志、零下游动作）；内容不同 → 明确 4xx 拒绝。
- 较新候选完成“全部解析 → 声明校验 → 适用对象计算”后，在一个数据库事务中一次替换文档字段与 statements，并把受影响 Observation 的 VEX 状态、关联声明、评估日志与派生结果更新为只反映该修订。
- 解析失败、替换过程中的数据库错误、进程中止：上一修订完整保留。
- 多个导入并发或乱序完成时收敛到最高确认修订；失败候选与迟到旧任务不能删除或回退已发布结果。
- 全新文档（库中无同身份文档）的导入结果与三种格式现有字段语义保持不变。

## Non-Goals
- 不修改三种格式的导出/生成器（openvex_generator / csaf_generator / cyclonedx_generator）及其模型（`OpenVEX`、`CSAF`、`CycloneDX` 及 `VEX_Base.content_hash`）。
- 不改变 VEX 引擎在观察结果创建/更新路径（`VEX_Engine`）上的既有跨文档匹配优先级与字段含义。
- 不改变手动删除 VEX 文档/声明的行为（`pre_delete` 信号保留）。
- 不做前端改动，不新增对外 API。
- 不引入分布式锁以外的新基础设施（仅依赖数据库事务与行锁；MySQL/PostgreSQL 生产，SQLite 单进程测试）。

## Background & Context
- 入口：[vex_import.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_import.py) 的 `import_vex()` 由 [views.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/api/views.py#L548-L563) 的 `VEXImportView` 调用（唯一调用方）。
- 三个解析器（[openvex_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/openvex_parser.py)、[csaf_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/csaf_parser.py)、[cyclonedx_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/cyclonedx_parser.py)）当前流程相同：按身份找到旧 `VEX_Document` → `delete()` → `create()` 新文档 → 边解析边 `save()` 新 `VEX_Statement` → 最后 `apply_vex_statements_after_import()`。
- 删除旧 statement 时 [signals.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/signals.py) 的 `vex_statement_pre_delete` 对关联 Observation 执行 `write_observation_log_no_vex_statement()`（写“Removed VEX statement”日志并回退 VEX 字段）；随后新 statements 应用时再写一次“Updated by VEX statement”日志并触发 issue tracker。重建即产生一对冗余日志。
- [vex_engine.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_engine.py) 通过 product purl、component purl、CycloneDX BOM link 三条路径匹配并更新 Observation；是否写日志取决于当前快照与新状态的字段比较（包含 statement 主键比较）。
- 下游动作均已事务安全：`push_observation_to_issue_tracker` 走 `@on_commit_task()`（Huey），通知（`send_observation_notification` 等）同样是 `@on_commit_task()`——事务回滚时不会触发。
- 版本字段现状：`VEX_Document.version` 为 `CharField(255)`。OpenVEX `version` 为正整数（spec v0.2.0）；CSAF `document/tracking/version` 为整数（CSAF 2.0）；CycloneDX 顶层 `version` 为整数（缺省 1）。
- 生产数据库为 MySQL/PostgreSQL；单元测试在 Docker 中以 SQLite 运行（`supports_select_for_update=False`，使用行锁必须按后端能力降级）。

## Functional Requirements

### 身份与修订
- **FR-1**: 每个候选在任何数据库写入之前完成解析与校验，产出内存候选：文档类型、身份（`document_id`、`author`）、`role`、版本原值、初始/当前发布时间、规范化修订键、内容哈希、声明集合（9 个既有字段）、product purl 集合。三种格式的字段提取规则、必填校验、状态/justification 校验、purl 校验与现状完全一致。
- **FR-2**: 修订先后只按版本号比较：优先按数值比较（OpenVEX/CSAF/CycloneDX 均为整数版本；点分数字版本按数字段比较），无法数值化时退化为字典序；版本号相等即视为同一修订，发布时间差异不改变修订先后，只参与内容哈希（同版不同日期 = 内容冲突，按 FR-7 拒绝）。非纯数字版本不得导致导入崩溃。
- **FR-3**: 内容哈希为对规范化语义负载（类型、document_id、author、role、版本原值、规范化后的初始/当前时间、按 9 字段排序后的声明元组集合）计算的 SHA-256，存入 `VEX_Document.content_hash`（新字段）。时间按解析后的时间点规范化（等价时区表示视为相同）；CycloneDX 缺省时间戳在哈希中规范化为空（服务端 `now()` 补默认值仅作用于存储字段，保证同一无时间戳文件重复上传仍判定内容一致）。

### 发布决策
- **FR-4**: 库中无同身份文档 → 作为全新修订发布（行为与现状一致）。
- **FR-5**: 已存在同身份文档且其类型与候选不同 → 4xx 拒绝（防止跨格式身份碰撞覆盖）。
- **FR-6**: 候选修订旧于当前修订 → 抛 `ValidationError`（4xx，消息说明当前已有更新修订）；文档、statements、Observation、日志、下游动作均不发生变化。
- **FR-7**: 候选修订与当前相同：
  - 内容哈希一致 → 幂等重放：不修改文档与 statements（主键保持稳定），不修改任何 Observation，不写 Observation_Log，不注册任何提交后任务；接口仍返回 204。
  - 内容哈希不一致 → 抛 `ValidationError`（4xx，消息说明同修订内容冲突）；任何状态不变。
- **FR-8**: 候选修订新于当前修订 → 进入唯一一次原子发布（FR-9）。
- **FR-9**: 原子发布在单个数据库事务内完成，顺序为：更新文档行字段（version、时间、role、content_hash；身份行主键不变）→ 插入该文档的全部新 statements → 计算受影响 Observation 集合 → 对每个受影响 Observation 完成状态/关联/日志/派生字段更新 → 删除旧 statements → 提交。任何一步异常（含进程被杀）整体回滚，上一修订的文档、statements、Observation 关联与日志完整保留；提交后下游任务才允许触发。
- **FR-10**: 受影响 Observation 集合 = 修订发布前关联到旧文档 statements 的 Observation ∪ 新声明集合按现有三条匹配路径（product purl、component purl、BOM link）命中的 Observation；集合内去重，每个 Observation 在一次发布中至多被评估一次、至多产生一条评估日志。
- **FR-11**: 受影响 Observation 的评估结果：
  - 命中新文档声明：映射到新声明行；仅当 VEX 语义结果（`vex_status`/`current_status`、`vex_vex_justification`/`current_vex_justification`、`vex_vex_remediations`/`current_vex_remediations`、`recommendation`，或声明语义内容）发生变化时才写日志并触发 issue tracker；旧→新声明为“同内容换修订”（9 字段相同）时只静默重指向，不写日志、不触发下游。
  - 不再被新文档声明命中且此前关联旧文档：执行与现有 `write_observation_log_no_vex_statement` 等价的回退（清空 VEX 字段、重算派生状态、写移除日志）；该路径不触发 issue tracker（与现状一致）。
  - 日志文案、auto-approve 评估状态、risk acceptance 过期计算等与现状一致。
- **FR-12**: 旧 statements 必须在受影响 Observation 全部重指向/清空后才删除，使 `pre_delete` 信号对这些 Observation 为空操作，避免“移除+更新”双日志。
- **FR-13**: 并发与乱序：
  - 同身份已存在文档：发布路径在事务开始时对该身份行执行 `SELECT ... FOR UPDATE`（仅在 MySQL/PostgreSQL 上；SQLite 按能力降级为普通查询），将同身份导入串行化；后到者在锁内重新按 FR-5~FR-9 决策。
  - 同身份文档尚不存在：插入在嵌套保存点中进行；唯一约束竞争的负方捕获 `IntegrityError` 后读取胜方已提交行并加锁，重新执行同一决策（旧→拒绝、同修订同内容→幂等、同修订异内容→冲突、更新→执行替换）。
  - 任意乱序完成顺序后，持久化结果恒为最高确认修订；失败候选不留任何残片（事务回滚），不删除/回退胜方结果。
- **FR-14**: 历史数据：迁移为既有 `VEX_Document` 行补空白 `content_hash`（默认 `""`）。同修订上传遇到历史空哈希时，执行一次 FR-9 的“静默重指向式”替换以补齐哈希（不产生日志与下游动作）；此后按 FR-7 正常幂等。

## Non-Functional Requirements
- **NFR-1（正确性）**: 并发/乱序/失败注入场景下，文档、statements、Observation、日志的持久化状态满足单一最高修订不变量；无孤儿 statement、无悬挂外键。
- **NFR-2（兼容性）**: 三种格式现有全部单元测试（含 long/short purl、component、BOM link 场景）保持通过；首次导入的库表字段值与现状逐字段一致。
- **NFR-3（可观测性）**: 拒绝原因（旧修订/同修订冲突/类型冲突）通过明确的错误消息反馈给调用方。
- **NFR-4（质量）**: 满足 black/isort/flake8/pylint/mypy 门禁与 120 字符行宽；业务逻辑位于 `application.vex.services`，类型注解完整。

## Constraints
- **Technical**: Python 3.12 / Django 6 / DRF；同时兼容 MySQL（pymysql）与 PostgreSQL（psycopg），测试库为 SQLite；不允许使用单一数据库专有锁原语。
- **Business**: 导入仅超级用户可用（权限不变）；HTTP 成功状态码仍为 204；拒绝沿用 `ValidationError` → 400。
- **Dependencies**: Huey `@on_commit_task` 的提交后语义；现有 `create_observation_log`、`get_current_status` 等服务函数。

## Assumptions
- OpenVEX/CSAF/CycloneDX 文档的版本字段遵循各自标准（单调递增整数版本）；非标准的非数字版本按 FR-2 退化比较，尽力而为。
- 同身份文档的 `type` 永远应一致；不一致视为异常输入而非合法修订。
- 导入仅在同步 API 请求中发生（当前唯一调用方），`get_current_user()` 在请求线程内可用。
- “内容一致”以解析后的语义负载为准（忽略 JSON 空白、键序差异），而非上传字节流。

## Acceptance Criteria

### AC-1: 三格式候选在写入前完成解析校验并产出修订键与内容哈希
- **Type**: `rule`
- **Given**: 任意 OpenVEX/CSAF/CycloneDX VEX 文件
- **When**: 调用导入入口
- **Then**: 在任何 INSERT/UPDATE/DELETE 之前完成全部字段/状态/purl 校验；产出包含身份、修订键、content_hash、声明集合的内存候选；非法输入抛出与现状一致的 `ValidationError` 且不触碰数据库
- **Pass Condition**: 单测断言：校验失败时 `VEX_Document/VEX_Statement/Observation/Observation_Log` 计数与字段零变化；同语义不同 JSON 排版（空白/键序）哈希相等；同版本改动任一 statement 字段或发布时间哈希不同
- **Evidence**: `unittests.vex.services` 下新增测试与 `./bin/unittests.sh unittests.vex` 输出

### AC-2: 较旧修订被明确拒绝且无副作用
- **Type**: `rule`
- **Given**: 已发布修订 N 的同身份文档
- **When**: 上传修订 N-1（三种格式各一）
- **Then**: 导入抛 `ValidationError`（4xx），消息表明存在更新修订；文档字段、statements 主键与内容、Observation 的 vex 字段与 `vex_statement` 关联、Observation_Log 数量、issue tracker/通知调用次数均保持上传前状态
- **Pass Condition**: 三格式各有单测验证拒绝与完整状态快照
- **Evidence**: 新增单测断言与 mock 调用计数（零调用）

### AC-3: 相同修订相同内容为幂等重放
- **Type**: `rule`
- **Given**: 已发布修订 N
- **When**: 连续两次上传与 N 字节不同但语义一致的文件（仅 JSON 排版差异）
- **Then**: HTTP 204；`VEX_Document` 与全部 `VEX_Statement` 主键不变；无 Observation 字段变化；无新增 Observation_Log；无提交后任务注册
- **Pass Condition**: 三格式幂等单测通过（比较主键集合、日志计数、下游 mock 零调用）
- **Evidence**: 新增单测

### AC-4: 相同修订内容不同被明确拒绝
- **Type**: `rule`
- **Given**: 已发布修订 N
- **When**: 上传版本号相同但任一 statement 语义字段或发布时间不同的文件
- **Then**: 抛 `ValidationError`（4xx），消息说明同修订内容冲突；全部持久化状态保持上传前
- **Pass Condition**: 三格式冲突单测通过
- **Evidence**: 新增单测

### AC-5: 较新修订原子替换文档与声明，Observation 只反映新修订
- **Type**: `rule`
- **Given**: 已发布修订 N，存在被其 statements 关联的 Observation
- **When**: 上传修订 N+1（包含：保留不变的声明、状态变化的声明、被删除的声明、新增的声明）
- **Then**: 提交后同身份文档行（主键不变）字段为 N+1 且 `content_hash` 更新；仅存在新声明行；受影响 Observation 中：不变声明的关联静默指向新行且无新日志，变化声明产生恰好一条反映新语义的日志并触发一次 issue tracker，删除声明的 Observation 被回退并写移除日志，新增声明按现有路径首次应用；所有受影响 Observation 重算后的 `current_status` 等派生字段只反映 N+1
- **Pass Condition**: 单测断言文档/statements 集合、每个受影响 Observation 的字段与关联、日志条数与文案、下游调用次数
- **Evidence**: 新增单测（至少 OpenVEX 与 CycloneDX BOM-link 两个场景）

### AC-6: 解析失败或发布中止时上一修订完整保留
- **Type**: `rule`
- **Given**: 已发布修订 N 且 Observation 已收敛
- **When**: 场景 a) 上传非法文件（statement 校验错误）；场景 b) 上传合法 N+1 但在事务内任意写入点注入数据库异常
- **Then**: 两种场景后修订 N 的文档字段、statements（主键与内容）、Observation 关联与 vex 字段、Observation_Log 数量与上传前完全一致；无提交后下游任务；非法文件得到 4xx，注入异常向上传播
- **Pass Condition**: 单测覆盖解析失败回滚与 `transaction.atomic` 回滚（mock 抛 `DatabaseError`）两种场景
- **Evidence**: 新增单测

### AC-7: 并发/乱序导入收敛到最高确认修订
- **Type**: `rule`
- **Given**: 同身份文档的修订 1/2/3 文件
- **When**: 以乱序与交错方式执行导入（含两个全新文档并发插入触发唯一约束竞争，以及在已存在行上交错发布）
- **Then**: 最终持久化恒等于修订 3 单独发布后的状态：文档版本 3、仅版本 3 的 statements、Observation 关联与日志集合与“仅按 1→3 顺序发布”的期望一致、无孤儿 statements；被拒绝的导入得到 4xx
- **Pass Condition**: 并发/竞争单测（真实线程或等价的锁/保存点交错模拟）通过；最终状态快照比较相等
- **Evidence**: 新增单测

### AC-8: 全新文档导入与三格式字段语义不变
- **Type**: `rule`
- **Given**: 现有全部 VEX 导入夹具
- **When**: 运行现有导入测试
- **Then**: 全部通过，首次导入后文档/声明各字段值（含日期、purl、BOM link、description 拼接、remediation 文案等）与现状逐字段一致
- **Pass Condition**: 现有 `test_vex_import_openvex/csaf/cyclonedx`、`test_vex_engine`、`basetest_vex_import` 覆盖的断言零修改通过
- **Evidence**: `./bin/unittests.sh unittests.vex` 全绿

### AC-9: 历史空哈希数据平滑升级
- **Type**: `rule`
- **Given**: 升级前遗留的 `content_hash=""` 的同身份文档
- **When**: 上传同修订文件
- **Then**: 完成一次静默重指向式替换并写入哈希；无新增语义日志、无下游动作；再次上传走 AC-3 幂等
- **Pass Condition**: 迁移可正向执行；遗留场景单测通过
- **Evidence**: 迁移文件与新增单测

### AC-10: 实现与代码质量
- **Type**: `rubric`
- **Dimension**: 事务边界清晰度、解析/发布职责分离、跨数据库兼容、重复代码控制、类型注解与可维护性
- **Scale**: 1-5
- **Anchors**: 1 = 仍有“先删后建”窗口、解析器含写入、数据库专有锁；3 = 功能正确但事务/锁处理分散、存在重复逻辑；5 = 纯解析 + 单一发布事务 + 能力检测锁、三格式共用发布器且格式差异仅在提取层、类型完整、无死代码
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查与 `black --check`、`isort --check`、`flake8`、`run_pylint.sh`、`run_mypy.sh` 输出

## Open Questions
- 无（旧修订响应方式已经用户确认为 4xx 明确拒绝）。
