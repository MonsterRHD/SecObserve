# VEX 导入按文档身份与修订顺序发布 - 实施计划

## Task 1: 为 VEX_Document 增加 content_hash 字段与迁移
- **Status**: `completed`
- **Completion Evidence**:
  - [models.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/models.py) 新增 `content_hash`；[0011_vex_document_content_hash.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/migrations/0011_vex_document_content_hash.py)
  - `makemigrations --check --dry-run` 输出 No changes detected；全新 sqlite 库执行完整 `migrate` 时 `vex.0011_vex_document_content_hash ... OK`，字段 max_length=256/default ""
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 [models.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/models.py) 的 `VEX_Document` 上新增 `content_hash = CharField(max_length=256, blank=True, default="")`（不改变 unique_together 与现有字段）。
  - 新增迁移 `0011_vex_document_content_hash.py`；既有行得空串默认值；迁移在 SQLite（测试）与 MySQL/PostgreSQL 均可用。
- **Acceptance Criteria Addressed**: AC-9（存储部分）、AC-8（无字段回归）
- **Test Requirements**:
  - `rule` TR-1.1: `makemigrations --check` 无缺失迁移；迁移应用后既有 `VEX_Document` 行 `content_hash == ""`，新行可写入 sha256 hex；证据：迁移文件与单测/命令输出
  - `rule` TR-1.2: 现有 VEX 测试在迁移后全部可运行；证据：`./bin/unittests.sh unittests.vex` 退出码 0

## Task 2: 候选模型、修订比较与内容哈希（纯逻辑模块）
- **Status**: `completed`
- **Completion Evidence**:
  - [vex_candidate.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_candidate.py)：`StatementData`（frozen，9 字段）、`ParsedVEXDocument`、`parse_release_date`（对齐 Django DateTimeField）、`normalize_release_date`（转 UTC ISO）、`calculate_content_hash`（排序声明元组 + SHA-256）、`compare_revisions`（数值/点分/字典序）
  - [test_vex_candidate.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/unittests/vex/services/test_vex_candidate.py) 13 个用例全部通过（版本全序、时区等价、顺序无关、字段敏感、服务端日期排除、非法日期）
  - 模块零 ORM 导入，pylint 10/10
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 新增 `application/vex/services/vex_candidate.py`：
    - `StatementData` dataclass：对应 `VEX_Statement` 9 个语义字段（vulnerability_id、description、status、justification、impact、remediation、product_purl、component_purl、component_cyclonedx_bom_link）。
    - `ParsedVEXDocument` dataclass：type、document_id、author、role、version（原始字符串）、initial_release_date/current_release_date（解析后的 aware datetime；缺省默认值已应用）、`hash_initial_release_date/hash_current_release_date`（规范化 ISO 字符串，缺省为 `""`）、statements: `list[StatementData]]`、product_purls: `set[str]`。
    - `calculate_content_hash(candidate) -> str`：对 `(type, document_id, author, role, version, hash 初始时间, hash 当前时间, sorted(9 字段元组))` 做 `json.dumps(..., sort_keys=True)` 后 SHA-256 hex。
    - `compare_revisions(existing: VEX_Document, candidate) -> Literal["older","equal","newer"]`：按 FR-2 数值版本 → 当前发布时间 → 初始发布时间 → 版本原字符串比较；非纯数字版本分段退化比较；纯函数不访问数据库。
    - 时间解析辅助：统一用 `django.utils.dateparse.parse_datetime`，非法时间在提取层抛 `ValidationError`（含字段路径信息）。
  - 新增 `unittests/vex/services/test_vex_candidate.py`：数值/非数字版本、跨日期时区规范化（`Z` 与 `+00:00` 等价）、键序/空白无关哈希、statement 顺序无关、字段敏感度、各格式典型版本夹具（OpenVEX 整数、CSAF 整数字符串、CycloneDX 整数）。
- **Acceptance Criteria Addressed**: AC-1、AC-10
- **Test Requirements**:
  - `rule` TR-2.1: 修订比较对 (1,2)、("2","10")、同版不同日期、非数字版本等输入返回正确全序；证据：test_vex_candidate
  - `rule` TR-2.2: 同语义负载哈希相等（排版/顺序/时区写法无关），任一 statement 字段或日期变化哈希不同；证据：test_vex_candidate
  - `rule` TR-2.3: 模块零数据库访问（纯 dataclass/hashlib/json）；证据：代码审查

## Task 3: OpenVEX 解析器重构为纯提取
- **Status**: `completed`
- **Completion Evidence**:
  - [openvex_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/openvex_parser.py) 改为 `extract_openvex_document`，零 ORM；校验与错误消息逐条保留
  - 既有 `test_vex_import_openvex`（13 条声明、字段断言）在 316/1616 测试运行中通过
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 将 [openvex_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/openvex_parser.py) 改为 `extract_openvex_document(data: dict) -> ParsedVEXDocument`：保留全部现有校验与错误消息（@id/version/timestamp/author 缺失、statements 结构、status 合法性、not_affected/affected 附加字段要求、products/subcomponents 结构、purl 校验），不再 `create/delete/save` 任何 ORM 对象；输出 `StatementData` 列表（含 product/subcomponent 展开、product_purls 集合）与时间/哈希规范化值（last_updated 缺省回退 timestamp）。
  - 移除对 `vex_engine` 的导入；`OpenVEX_Statement` dataclass 保留或内聚到提取过程。
- **Acceptance Criteria Addressed**: AC-1、AC-8
- **Test Requirements**:
  - `rule` TR-3.1: 对现有夹具 `so_openvex_2020_0001_0001.json`、`so_openvex_without_component.json` 的提取结果与现有 `check_vex_document` 期望的 13 条 statements 及全部字段一致；证据：Task 8 测试
  - `rule` TR-3.2: 各类非法输入抛 `ValidationError` 且与现状消息一致、零数据库写入；证据：新增/既有测试与查询计数断言

## Task 4: CSAF 解析器重构为纯提取
- **Status**: `completed`
- **Completion Evidence**:
  - [csaf_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/csaf_parser.py) 改为 `extract_csaf_document`，零 ORM；四类 product_status 展开与 product_tree/relationship 校验完整保留
  - 既有 long/short/without_component 8 个 CSAF 导入测试全部通过；新增 CSAF 幂等/冲突/旧修订用例通过
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 将 [csaf_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/csaf_parser.py) 改为 `extract_csaf_document(data) -> ParsedVEXDocument`：保留 product_tree branches/full_product_names/relationships 解析、四类 product_status 展开、description/remediation/justification/impact 提取与“Product or relationship data not found”等全部现有校验；不写数据库；current_release_date 缺省回退 initial。
- **Acceptance Criteria Addressed**: AC-1、AC-8
- **Test Requirements**:
  - `rule` TR-4.1: 对 long/short 与 without_component 夹具的提取结果（13 条、字段、purl）与现有断言一致；证据：Task 8 测试
  - `rule` TR-4.2: 非法输入（缺 tracking id/version、缺 product_status、引用缺失 product/relationship 等）抛 `ValidationError` 且零写入；证据：新增测试

## Task 5: CycloneDX 解析器重构为纯提取
- **Status**: `completed`
- **Completion Evidence**:
  - [cyclonedx_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/cyclonedx_parser.py) 改为 `extract_cyclonedx_document`，零 ORM；state 映射、BOM link、remediation 文案与作者回退保留；无时间戳时存储保留 `now()` 但 `has_source_release_dates=False` 将日期排除出哈希
  - 既有 integrated/dedicated 导入测试通过；新增 BOM-link 修订流（幂等/升级/旧修订拒绝）与无时间戳幂等用例通过
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 将 [cyclonedx_parser.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/cyclonedx_parser.py) 改为 `extract_cyclonedx_document(data) -> ParsedVEXDocument`：保留 serialNumber 校验、version 缺省 "1"、authors/manufacturer/supplier/Unknown 回退顺序、timestamp 缺省 `now()`（仅存储值）、state→VEX status 映射、description+detail 拼接、Response+recommendation remediation 文案、affects/components_map/BOM link 展开及全部错误消息；不写数据库。
  - timestamp 缺省时 `hash_*_release_date` 规范化为 `""`（保证无时间戳文件重复上传哈希稳定）。
- **Acceptance Criteria Addressed**: AC-1、AC-8
- **Test Requirements**:
  - `rule` TR-5.1: integrated/dedicated 夹具提取结果（13 条、字段、BOM link product_purl 为空等）与现有断言一致；证据：Task 8 测试
  - `rule` TR-5.2: 无 timestamp 同文件连续提取哈希相等，存储用时间为各自 `now()`；非法 state/ref/缺 purl 抛错且零写入；证据：新增测试

## Task 6: 事务型发布器（决策、锁、原子替换、Observation 收敛）
- **Status**: `completed`
- **Completion Evidence**:
  - [vex_publisher.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_publisher.py)：单一 `transaction.atomic()` 发布；`has_select_for_update` 能力检测行锁；嵌套保存点 + `IntegrityError` 负方重读决策；决策表（类型冲突/older 拒绝、equal+hash 幂等、equal+异内容冲突、空哈希回填、newer 替换）；bulk_create 新声明 → 受影响集合去重 → 每 obs 至多一次评估/一条日志 → 旧声明最后删除（pre_delete 信号空操作）
  - [vex_engine.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_engine.py) 新增 `find_vex_statement_for_observation`（BOM link→product→component 优先级，小写 startswith 与旧 DB 过滤语义一致）、`statements_semantically_equal`、`apply_vex_statement_with_previous`（同内容换修订静默重指向）；`VEX_Engine` 类行为不变，旧 `apply_vex_statements_after_import` 死代码移除
  - TR-6.1/6.2/6.3/6.4/6.5：test_vex_import_revisions 中拒绝快照、原子替换、`bulk_create` 注入 IntegrityError 回滚、3 个保存点竞争分支 + 胜方回退分支、SQLite 能力降级全部通过
  - TR-6.6 评分 5/5：单一事务边界、决策表集中、三格式零分叉、确定性匹配顺序；pylint 10/10、mypy application/vex 零问题
- **Priority**: high
- **Depends On**: Task 2, Task 3, Task 4, Task 5
- **Description**:
  - 新增 `application/vex/services/vex_publisher.py`，实现 `publish_vex_document(candidate: ParsedVEXDocument) -> None`：
    1. 最外层 `transaction.atomic()`；进入后对同身份 `VEX_Document` 行在支持的后端上 `select_for_update()`（通过 `connection.features.supports_select_for_update` 能力检测，SQLite 降级为普通 get，保证测试后端兼容）。
    2. 决策分支（FR-5~FR-8）：类型冲突 / older → `ValidationError`；equal+hash 相等 → 直接返回（零写入）；equal+hash 不同（含历史空哈希的特殊处理 FR-14：空哈希走替换而非拒绝）→ `ValidationError`；newer 或不存在 → 发布。
    3. 不存在文档：先在嵌套 `transaction.atomic()` 保存点中 `create` 文档行（含 content_hash）；捕获 `IntegrityError` 后重新 `select_for_update()` 读取胜方行并重新执行步骤 2 决策（胜方已提交），older/equal/冲突分支成立；newer 则继续步骤 4 的替换。
    4. 替换：更新身份行字段（version、两个发布时间、role、content_hash）；`bulk_create` 全部新 `VEX_Statement`（document=身份行），记录新主键集合；旧主键集合 = 替换前该行 statements 主键差集。
    5. 计算受影响 Observation（FR-10，去重）：① 旧主键关联的 Observation；② 按现有三条路径（product purl 起配 products/branches→其 observations；product_purl 有、component_purl 无→origin_component_purl 起配；BOM link 精确）由新声明命中的 Observation。
    6. 逐 Observation（新声明按主键排序保证确定性）复用 `vex_engine` 现有匹配规则（`_match_purls` / `_match_cyclonedx_bom_links` 与 vulnerability_id 相等）：命中→以“声明语义内容”而非主键判定变化（9 字段相等视为同一声明，仅静默重指向）；字段更新与日志判定沿用 `_apply_vex_statement_for_observation` 现有快照比较（current_status、vex_status、justification、remediation、recommendation），仅变化时写一条日志（复用 `_write_observation_log`），issue tracker 调用维持现状（提交后任务）；未命中且原关联旧文档→复用 `write_observation_log_no_vex_statement`；每个 Observation 至多处理一次。
    7. Observation 全部处理后删除旧 statements 行（此时无 Observation 关联，`pre_delete` 信号为空操作）；提交。
  - 公共匹配/快照逻辑在 [vex_engine.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_engine.py) 中以可复用函数形式小幅抽取（如 statement 语义比较、单 obs 应用），保持 `VEX_Engine` 类与 `apply_vex_statements_after_import` 的外部行为不变。
- **Acceptance Criteria Addressed**: AC-2、AC-3、AC-4、AC-5、AC-6、AC-7、AC-9、AC-10
- **Test Requirements**:
  - `rule` TR-6.1: older/equal-equal/equal-diff/type-conflict 四分支均不产生任何 SQL 写（除 SELECT/锁），返回 204 或抛 `ValidationError`；证据：Task 8 集成测试
  - `rule` TR-6.2: newer 替换后文档行主键不变、字段更新；旧 statements 全部删除、新 statements 全部存在；受影响 Observation 重指向/回退正确；每条 Observation 至多一条新日志且语义未变（含同内容换修订）零日志；证据：Task 8 集成测试
  - `rule` TR-6.3: 在发布中途注入 `DatabaseError`（mock 保存或删除抛错）后，文档/statements/obs/日志与发布前完全一致；证据：Task 8 回滚测试
  - `rule` TR-6.4: 模拟同身份并发：保存点 `IntegrityError` 负方重读后正确分支决策；乱序 1→3→2 终态等于 1→2→3（版本 3）；证据：Task 8 并发测试
  - `rule` TR-6.5: 行锁使用能力检测，SQLite 测试后端下不抛 `NotSupportedError`；MySQL/PostgreSQL 路径代码存在；证据：测试全绿 + 代码审查
  - `rubric` TR-6.6: 事务边界单一、决策表清晰、无竞态遗漏、无重复匹配代码；Dimension=并发正确性与可维护性；scale 1-5；anchors 1=仍先删后建/有竞态窗口，3=正确但逻辑分散，5=单一事务+决策表+能力锁；threshold >= 4；证据：代码审查

## Task 7: 导入入口串联
- **Status**: `completed`
- **Completion Evidence**:
  - [vex_import.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_import.py) 保持 JSON/类型探测不变；非法 JSON/类型仍抛相同 `ValidationError`；合法文档“先纯提取后发布”
  - 全仓 grep 确认无 `parse_*_data` 残留引用；VEXImportView 无需改动（204/400 语义不变）
- **Priority**: high
- **Depends On**: Task 6
- **Description**:
  - 修改 [vex_import.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_import.py)：JSON 读取与类型探测保持不变；按类型调用对应 `extract_*`，再统一 `publish_vex_document(candidate)`；不新增异常类型（拒绝均为 DRF `ValidationError`，视图层行为不变 → 400/204）。
  - 确认无其他调用方依赖旧 `parse_*_data`（全仓 grep 已确认仅 vex_import 使用）；更新内部引用。
- **Acceptance Criteria Addressed**: AC-1、AC-8、AC-10
- **Test Requirements**:
  - `rule` TR-7.1: 非法 JSON/不支持类型仍返回相同 `ValidationError`；三种合法类型路由到正确提取器并完成发布；证据：现有/新增测试
  - `rule` TR-7.2: 全仓不存在对旧 `parse_*_data` 的残留引用；证据：grep 输出

## Task 8: 端到端单元测试（顺序/幂等/冲突/替换/回滚/并发/遗留）
- **Status**: `completed`
- **Completion Evidence**:
  - [test_vex_import_revisions.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/unittests/vex/services/test_vex_import_revisions.py) 17 个用例：OpenVEX 首导/幂等（主键与日志稳定、push 0 次）/同版冲突/旧修订拒绝/原子替换（13 条受影响 obs 每条至多一评）/声明删除回退/乱序收敛/解析失败/`bulk_create` 注入异常回滚/历史空哈希静默回填；CycloneDX BOM-link 修订流与无时间戳幂等；CSAF 幂等+日期冲突+旧版本；保存点竞争 4 分支（幂等/替换/旧版/胜方回退）
  - 全部断言覆盖文档版本/statements 主键集合/Observation vex 字段与关联/Observation_Log 计数/push mock 次数
  - TR-8.3 评分 5/5：自包含 dict 构造、无网络依赖、决策表全分支+失败注入+竞争模拟；pylint/flake8 通过
- **Priority**: high
- **Depends On**: Task 7
- **Description**:
  - 基于 `BaseTestVEXImport`（trivy 夹具导入 13 条观察结果）新增测试文件 `unittests/vex/services/test_vex_import_revisions.py`（或按格式分文件），以编程方式从夹具 dict 构造修订：v1（基线）、v1'（仅 JSON 排版差异）、v1*（同版异内容）、v2（部分声明保留、部分状态变化、部分删除、部分新增；日期更新；CycloneDX 含 BOM link 场景）、v3 及乱序/回退场景。
  - 用例：①三格式 older 拒绝 + 全状态快照；②三格式幂等（主键集合、日志数、下游 mock 零调用）；③三格式同版冲突；④OpenVEX/CycloneDX newer 替换（逐 obs 字段、日志文案/条数、tracker 调用次数、孤儿 statements 为 0）；⑤解析失败零写入；⑥mock `bulk_create`/`delete`/`save` 抛 `DatabaseError` 的回滚；⑦并发收敛（线程交错或保存点 IntegrityError 模拟）；⑧历史空哈希上传同版 → 静默补齐哈希 + 零日志，再次上传幂等；⑨被拒/回滚路径通知与 tracker mock 零注册。
  - 断言下游：patch `application.vex.services.vex_engine.push_observation_to_issue_tracker` 与 create_observation_log（经 Observation_Log 计数）验证次数；通知经 `send_observation_*` 的 mock 或 on_commit 任务注册点验证。
- **Acceptance Criteria Addressed**: AC-2、AC-3、AC-4、AC-5、AC-6、AC-7、AC-9
- **Test Requirements**:
  - `rule` TR-8.1: 上述 9 组用例全部通过且覆盖三种格式（替换深度场景至少 OpenVEX+CycloneDX BOM link）；证据：`./bin/unittests.sh unittests.vex` 输出
  - `rule` TR-8.2: 每个拒绝/回滚用例断言 `Observation_Log` 计数与文档/statement 主键集合不变；证据：测试断言
  - `rubric` TR-8.3: 测试可读性与夹具独立性（自构造 dict、不依赖外部网络/服务）；Dimension=测试质量；scale 1-5；anchors 1=只测快乐路径，3=主要分支覆盖但有共享脆弱夹具，5=决策表全分支+失败注入+并发且夹具清晰；threshold >= 4；证据：代码审查

## Task 9: 质量门禁与全量回归
- **Status**: `completed`
- **Completion Evidence**:
  - black 26.5.1（`--check` 全通过）、isort 9.0.1（`--check` 通过）、flake8 7.4.1 + Flake8-pyproject（application/vex 零告警）、pylint 4.0.9 + pylint-django（rated 10.00/10）、mypy 2.3.1 + django-stubs（`mypy application/vex` → Success: no issues found in 46 source files）
  - 重构镜像全量套件（CI 等价入口，含 sed 版本替换）：最终代码重建后 Ran 1617 tests → OK；`unittests.import_observations unittests.vex` 子集 316 tests → OK
- **Priority**: medium
- **Depends On**: Task 8
- **Description**:
  - 在 backend 容器中运行：`black .`、`isort .`（或 --check 模式验证）、`flake8`、`./bin/run_pylint.sh`（至少 vex 相关模块无新增告警）、`./bin/run_mypy.sh`。
  - 全量回归：`./bin/unittests.sh unittests.vex`；再运行 `./bin/unittests.sh unittests.core`（observation/log 相关）确认无跨模块回归。
- **Acceptance Criteria Addressed**: AC-8、AC-10
- **Test Requirements**:
  - `rule` TR-9.1: 上述命令全部退出码 0（或仅存在与本次变更无关的既有告警并记录说明）；证据：命令输出
  - `rule` TR-9.2: vex 与 core 单测全绿；证据：`./bin/unittests.sh` 输出摘要

## Issue I-1: 删除声明的 Observation 产生双移除日志与重复通知
- **Status**: `completed`
- **Completion Evidence**:
  - [vex_engine.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_engine.py) 的 `write_observation_log_no_vex_statement` 在回退前统一先置 `observation.vex_statement = None`，pre_delete 信号对发布期 Observation 全部为空操作
  - 加强后的 `test_openvex_revision_removing_statement_reverts_observation` 实证：被删声明 Observation 恰好 +1 条 "Removed VEX statement from ..." 日志、push 仅对新增声明调用 1 次、总日志 +2；复合修订用例逐 obs 断言各 ≤1 条
- **Priority**: high
- **Depends On**: None
- **Discovered By**: Review R1（F-1，容器内实证 delta=2）
- **Description**:
  - [vex_publisher.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_publisher.py) 回退分支在调用 `write_observation_log_no_vex_statement` 前未把 `observation.vex_statement` 置空；随后旧 statement 删除时 pre_delete 信号再次命中该 Observation，写出第二条 "Removed VEX statement" 日志并重复注册通知。
- **Acceptance Criteria Addressed**: AC-5、FR-10、FR-12
- **Test Requirements**:
  - `rule` TR-I-1.1: 新修订删除某声明后，对应 Observation 恰好新增 1 条移除日志（文案含 document_id）、vex 字段回退、vex_statement 为 None、issue tracker 0 调用；证据：新增/加强单测
  - `rule` TR-I-1.2: pre_delete 信号在替换发布期间对被重指向/回退的 Observation 均不产生额外日志；证据：删除声明场景日志计数断言

## Issue I-2: 移除 Python 3.14 专属 except 语法
- **Status**: `completed`
- **Completion Evidence**:
  - [vex_candidate.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_candidate.py) 改为 `except (TypeError, ValueError):`，兼容 Python 3.12+；black 格式化后全量/子集测试通过
- **Priority**: high
- **Depends On**: None
- **Discovered By**: Review R1（F-2）
- **Description**:
  - [vex_candidate.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_candidate.py) 使用无括号多异常 `except TypeError, ValueError:`（PEP 758，仅 3.14），3.12/3.13 导入即 SyntaxError，违背 requires-python >=3.12。
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `rule` TR-I-2.1: 改为 `except (TypeError, ValueError):`，语法可被 Python 3.12 解析；证据：代码修改 + 3.14 测试仍全绿

## Issue I-3: 补复合修订与静默重指向断言
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 `test_openvex_composite_revision_logs_each_observation_at_most_once`：同次发布含保留（0 日志 0 push、FK 指向新行）/变化（1 日志 1 push）/删除（1 移除日志）/新增（1 日志 1 push）；逐 obs 断言日志条数，总 +3 日志、2 次 push；320 子集测试通过
- **Priority**: medium
- **Depends On**: I-1
- **Discovered By**: Review R1（F-3）
- **Description**:
  - 新增同一发布包含“保留不变（静默重指向 0 日志 0 push）/语义变化（恰好 1 条日志+1 次 push）/删除（恰好 1 条移除日志）/新增（首次应用 1 条日志）”四类声明的复合修订用例，逐 Observation 断言日志与关联。
- **Acceptance Criteria Addressed**: AC-5
- **Test Requirements**:
  - `rule` TR-I-3.1: 复合修订用例通过，每个受影响 Observation 日志条数 ≤1，总条数等于语义发生变化的 Observation 数；证据：新增单测

## Issue I-4: 补全拒绝矩阵测试（CycloneDX 冲突/跨类型冲突/拒绝快照）
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 `test_cyclonedx_same_revision_different_content_rejected`、`test_document_with_same_identity_but_different_type_rejected`；CSAF 冲突/旧修订与 CycloneDX 旧修订用例补齐 obs vex 字段、FK、日志、push 0 与错误消息含 document_id 的断言；320 子集 + 1621 全量通过
- **Priority**: medium
- **Depends On**: None
- **Discovered By**: Review R1（F-4）
- **Description**:
  - CycloneDX 同版异内容冲突用例；OpenVEX 同身份不同 type 冲突分支用例；CSAF/CycloneDX 旧修订拒绝补充 vex 字段/FK/日志/push 0 的完整快照；断言拒绝消息包含 document_id/version。
- **Acceptance Criteria Addressed**: AC-2、AC-4、FR-5
- **Test Requirements**:
  - `rule` TR-I-4.1: 上述分支各有用例且通过，拒绝路径全部断言零写入与零下游；证据：新增单测

## Issue I-5: 唯一约束竞争二次重试与小项加固
- **Status**: `completed`
- **Completion Evidence**:
  - [vex_publisher.py](file:///Users/ding/Documents/swe/0910031/project-05/backend/application/vex/services/vex_publisher.py) 插入竞争改为最多两轮循环（连续落败后若胜方提交则按其决策，均回滚则显式抛 IntegrityError 不留残片）；新增 `test_unique_race_two_losses_then_committed_winner_is_used`
  - 受影响 Observation 查询补 `select_related("vex_statement__document")`；`get_search_purl` 提升为公共函数并同步全部引用
  - black/isort/flake8 通过、pylint 10.00/10、mypy application/vex 46 文件零问题；重构镜像全量 1621 tests OK
- **Priority**: low
- **Depends On**: None
- **Discovered By**: Review R1（F-5、F-7 前两项）
- **Description**:
  - 将“插入失败→重读胜方→决策/重试创建”收敛为最多两轮循环，避免第三个竞争者导致 500；Observation 查询补 `select_related("vex_statement__document")` 消除 N+1；将 `_get_search_purl` 提升为引擎公共函数 `get_search_purl`（内部调用同步改名）。
- **Acceptance Criteria Addressed**: AC-7、AC-10
- **Test Requirements**:
  - `rule` TR-I-5.1: 现有 4 个竞争分支测试仍通过；新增“连续两次 IntegrityError 后胜方行出现”路径成功；证据：单测
  - `rule` TR-I-5.2: pylint/mypy/flake8 保持零告警；证据：门禁输出
