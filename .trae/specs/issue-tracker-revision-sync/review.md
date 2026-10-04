# 外部工单同步按修订收敛 - Independent Review

- **Result**: FAIL (3 critical, 7 major)
- **Reviewer**: 独立代码评审子代理
- **Date**: 2026-10-03

## 验证环境

- 单元测试：仓库根目录 `docker compose -f docker-compose/docker-compose-unittests.yml run --rm django [测试标签]`
  （标准入口跑镜像内 /app；镜像由 `... build django` 基于当前代码构建；HUEY_IMMEDIATE=true）
- 宿主代码直跑：`run --rm -e DJANGO_SETTINGS_MODULE=config.settings.unittests --entrypoint sh django -c "cd /home && python manage.py test ..."`（评审中的临时验证测试已全部删除，未留下任何源码改动）
- 前端：`cd frontend && npm run lint && npm run build`
- Django 版本：容器内确认 6.1.1

## 产品结果检查点（Run Rules）

- [x] CP-R1: 业务事务内只登记期望状态（Outbox 行），后台按修订顺序/最新修订收敛
- [x] CP-R2: 期望状态 OPEN/CLOSED/DELETED 的远端动作序列正确（创建/更新/重开/关闭/删除说明/无操作）
- [ ] CP-R3: 连续多次修订后只允许最新修订驱动远端写（旧任务/重复触发 0 远端写）— **FAIL，见 C1**
- [ ] CP-R4: 行级串行认领（skip_locked + IN_PROGRESS 租约），并发 worker 不重复写远端 — **FAIL，见 C1**
- [ ] CP-R5: 创建超时/5xx/429/连接错误：重试前先按关联标记查询，找到即认领，绝不重复创建（三平台）— **FAIL，见 C2/C3**
- [x] CP-R6: 关闭/重开幂等，Jira 已在目标状态不重复 transition；确定性 4xx 立即 FAILED（适配器层正确；但认领后重开路径失效，见 C2）
- [x] CP-R7: 404 清本地标识：期望开放重建、期望关闭/删除直接完成且无写调用
- [x] CP-R8: 进程重启后在途同步继续（租约回收 + 启动命令 + 周期扫描）
- [ ] CP-R9: 默认分支/最低严重性/停用/启用/重新启用事件最终收敛（旧分支关闭、阈值跨越、停用全关后零任务）— **FAIL：停用后仍可能新建工单，见 M1**
- [x] CP-R10: 删除观察结果：pre_delete 同事务登记 DELETED，行保留产品/issue 标识，远端加说明并关闭，404 完成，失败可重试
- [x] CP-R11: 未启用集成产品零状态行、零额外任务
- [x] CP-R12: 手动 synchronize issues 强制全部对账（失败行复位、已同步行再对账）— 行为代码正确，但无接口测试（M7）
- [x] CP-R13: 失败原因/待重试时间/状态在 API 与前端可见，失败/待重试可手动重试（偏差见 M5/M6）
- [x] CP-R14: 三平台标签（合并/add_labels/confidential/state_event）、描述（Markdown/Jira wiki）、关闭语义保持
- [ ] CP-R15: 端到端组合场景（连续修订×超时重启、往返、批量删除、停用）测试通过 — **FAIL：组合场景测试不存在，且存在重复工单路径，见 C1/C3/M7**

## 证据

### 执行命令与结果

1. 全量后端单元测试（标准镜像入口，baked /app）：
   `docker compose -f docker-compose/docker-compose-unittests.yml run --rm django`
   → `Ran 1621 tests in 55.972s` / `OK`，退出码 0，覆盖率报告 85%。
2. 前端：`cd frontend && npm run lint` → 退出 0（eslint 无输出）；`npm run build`（tsc + vite/rolldown）→ `built in 1.40s`，退出 0。
3. 代码风格（宿主代码 /home，临时 pip 安装工具）：`black --check` → 169 files unchanged；`isort --check-only` → ISORT_OK；`flake8 --max-line-length=120 --extend-ignore=I001,I003,I004,I005` → FLAKE8_CLEAN。
4. Django 删除收集器顺序（容器内源码 `/usr/local/lib/python*/site-packages/django/db/models/deletion.py`，Django 6.1.1）：`Collector.delete()` 中 pre_delete 信号先发送，随后 fast deletes、field_updates（SET_NULL），最后 DELETE 实例。SET_NULL 的更新以懒查询集在 UPDATE 执行时求值，因此 pre_delete 中新登记的 DELETED 行也会被一并置空 observation_id。
5. SQLite 特性（容器内验证）：`has_select_for_update=False`、`has_select_for_update_skip_locked=False`，编译器仅在 `has_select_for_update` 为真时输出 FOR UPDATE（compiler.py:859）。即 SQLite 测试环境下行锁/skip_locked 完全退化为无操作，并发互斥在 CI 中无任何覆盖。
6. 三个临时验证测试（评审后已删除）实测结果：
   - IN_PROGRESS 活租约期间 bump 修订后第二次 claim：`SECOND CLAIM AFTER BUMP: GRANTED (concurrent remote execution)`。
   - FAILED 不确定创建行手动重试：`CALLS AFTER MANUAL RETRY: ['create']`（无 search、无 get，盲目再创建）。
   - marker 检索认领回一个 CLOSED 工单（真实 GitHubIssueTracker + mock HTTP）：远端调用仅 `GET search/issues` 与 `GET issues/gh_found`，**没有 PATCH**；本地 `issue_tracker_issue_id=gh_found, closed=False`、行 SUCCEEDED，远端仍关闭。
   - 停用后遗留 PENDING OPEN 行：`create calls: {'create': 1} row state: SUCCEEDED OPEN`（停用后仍新建工单）。
   - 删除有 issue id 无状态行的观察结果（历史数据场景）：`ROWS AFTER DELETE: [(None, 'gh_legacy_1', 'DELETED')]`，不触发外键错误。

### 关键代码走查引用

- 状态机：[issue_sync_executor.py](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L59-L93)（claim 守卫仅在 sync_state==IN_PROGRESS 时看租约）、[L121-L135](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L121-L135)（marker 检索门槛 attempts>0；认领后不回写 observation 身份）、[L276-L320](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L276-L320)（错误处理不比较 revision）。
- 登记：[issue_sync_registration.py L299-L310](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L299-L310)（bump 无条件 PENDING/locked_at=None/attempts=0）、[L225-L260](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L225-L260)（停用只关有 issue id 的观察结果）。
- 调度：[issue_sync_dispatcher.py L32-L46](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_dispatcher.py#L32-L46)（即时任务无 lock_task；周期扫描只认 PENDING）。
- Tracker 早退：[github L79](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/github_issue_tracker.py#L79)、[gitlab L66](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/gitlab_issue_tracker.py#L66)、[jira L76](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L76)；Jira 误认兜底：[jira L73](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L73)。

## Findings

### CRITICAL

- **C1（AC-3/AC-4/FR-6，CP-R3/CP-R4）：IN_PROGRESS 期间的修订 bump 击穿行级互斥，两个执行者可并发收敛同一行并重复建工单。**
  新修订登记（[_bump_status_row](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L299-L310)）无条件把行重置为 `sync_state=PENDING, locked_at=None`，覆盖正在执行的活租约；而 claim 的租约检查只在状态为 IN_PROGRESS 时生效（[executor L75-L87](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L75-L87)）。即时任务 `trigger_observation_sync` 没有计划中承诺的 `@lock_task`（[dispatcher L32-L34](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_dispatcher.py#L32-L34)），周期扫描（dispatcher L41-L54）也只选 PENDING。时序：worker A claim 后在 get/create 慢 HTTP 期间（最长约 60s/次）用户连续保存（正是 AC-3 场景）→ bump 为 PENDING → 即时触发或周期扫描第二次 claim 成功（已实测 GRANTED）→ 两者都在"远端无工单"时 POST create → 两个永久重复工单（后续 marker 检索只认领最新一个，另一个成为永久孤儿）。修订号检查（executor L253）只在远端调用之后阻止误报 SUCCEEDED，不能阻止旧/并发修订的远端写。
- **C2（AC-5/AC-6，CP-R5）：marker 检索认领回的工单不会被真正收敛。** 执行器认领后只设置局部变量 issue_id（[executor L130-L144](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L130-L144)），不回写 `observation.issue_tracker_issue_id`；而 GitHub/GitLab/Jira 的 `update_issue` 首行均在该字段为空时直接 return（[gh L79](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/github_issue_tracker.py#L79)、[gl L66](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/gitlab_issue_tracker.py#L66)、[jr L76-L93](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L76-L93)）。实测真实 GitHubIssueTracker：认领回 CLOSED 工单后无任何 PATCH，本地却记为 open/SUCCEEDED；陈旧标题/描述同样不更新；Jira 还因 `issue_tracker_jira_initial_status` 为空而不重开。
- **C3（AC-5/AC-11，CP-R5）：去重信号 attempts 被复位后盲目重复创建。** marker 检索的唯一门槛是 `status_row.attempts > 0`（[executor L126](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L121-L127)），但新修订 bump 会把 attempts 清零（[registration L305](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L305)）：不确定创建后的退避窗口内只要有一次编辑，下一轮就跳过检索直接 create；手动重试复位同样清零（[registration L263-L271](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L263-L271)），实测 FAILED 行手动重试调用序列仅 `['create']`。两种路径都会对"实际已创建"的工单产生重复工单。

### MAJOR

- **M1（AC-9/FR-8，CP-R9）：停用集成后仍会创建新工单。** `close_all_for_deactivation` 只覆盖 `issue_tracker_issue_id` 非空的观察结果（[registration L233](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L233-L246)）；对"创建尚未执行/不确定创建"留下的 PENDING OPEN 无 issue_id 行不动。执行器在 `compute_desired_state` 返回 None（已停用）时保留行内 OPEN 期望（[executor L102-L107](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L102-L107)，注释意图与实现相反），实测停用后 create=1 且行 SUCCEEDED。
- **M2（FR-12/AC-3）：错误处理不感知并发新修订。** [_handle_uncertain_error](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L276-L304)/[_handle_deterministic_error](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L307-L320) 不像成功路径那样比较 revision：尝试期间有新修订时，不确定失败会把新修订推迟整个退避间隔（最多 24h，低延迟触发被 next_attempt_at 挡住）；确定失败会把新修订直接打成终态 FAILED 且不再自动运行。
- **M3（FR-7）：标签漂移永不收敛。** `_issue_is_current` 只比较标题与描述（[executor L210-L215](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L210-L215)），不比较标签。产品 `issue_tracker_labels` 变更虽触发 resync bump，但执行器判为 current 后 no-op，新标签不会被合并到既有工单。
- **M4（AC-5）：Jira marker 检索可误认无关工单。** [jira L73](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L65-L73) 在所有命中项描述都不含 token 时仍 `return issues[0].key`；JQL `text~` 同时搜索评论/摘要，命中后执行器会对无关工单更新摘要/描述甚至重开/关闭。
- **M5（AC-12/FR-18）：手动 retry 不限制行状态。** [views.py L43-L48](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/api/views.py#L43-L48) 对 SUCCEEDED/IN_PROGRESS 行也接受 reset；前端按钮对所有行显示。对活 IN_PROGRESS 行复位会主动制造 C1 的并发窗口。设计（Task 7）仅允许 FAILED/PENDING。
- **M6（AC-12/FR-18）：后台管理 UI 缺产品筛选。** [IssueTrackerSyncEmbeddedList.tsx L45-L58](file:///Users/ding/Documents/swe/0910031/project-06/frontend/src/background_tasks/issue_tracker_sync/IssueTrackerSyncEmbeddedList.tsx#L45-L58) 只有 sync_state/desired_state 两个过滤器；后端 filterset 已支持 product。FR-18 明确要求可按产品筛选。
- **M7（测试质量，AC-4/AC-5/AC-13/AC-15/TR-6.1/TR-9.1）：关键行为缺少有效测试。** ① 不存在 skip_locked 并发认领测试（dispatcher 测试把 execute_sync 整体 mock）；② 无"旧修订零远端写"断言；③ marker 认领后需要更新/重开的分支无测试（C2 因此漏网），三平台未各测编排；④ 无"bump/手动重试后仍先 marker 检索"测试（C3 漏网）；⑤ 无停用后执行器行为测试（M1 漏网）；⑥ synchronize issues 接口（AC-13）与产品 serializer 停用/启用钩子（TR-3.3）无接口/服务测试；⑦ Task 9 声称的 6 个组合场景测试文件不存在（`unittests/issue_tracker/` 下无任何 scenario 测试）。

### MINOR

- m1（FR-7）：删除说明不完全幂等——远端已有删除说明但工单仍 open 时会再次调用 close_for_deleted 并二次追加删除头（[executor L181-L184](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L177-L184)，tracker 侧不去重）。
- m2（FR-15）：未分类异常（如 requests.TooManyRedirects、KeyError）逃逸 execute_sync：行停留 IN_PROGRESS 等租约、dispatch_pending 整批中断、无 last_error 落库、无异常通知（旧机制由 Huey 兜底通知）。
- m3：死代码/风格——[apps.py L9-L12](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/apps.py#L9-L12) import 永远不存在的 `application.issue_tracker.signals`（静默 ImportError）；[registry.py L14-L16](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/background_tasks/services/registry.py#L14-L16) 把一个字符串拆成两个相邻字面量。
- m4（NFR-3）：register_product_resync/close_all 对既有行逐行 save（N 次 UPDATE），仅新行用了 bulk_create。
- m5：不确定创建（本地无 issue_id）期间删除观察结果时，register_observation_deleted 直接删行（[registration L119-L123](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L119-L123)），远端"可能已存在"的工单成为永久孤儿，没有 marker 对账机会。
- m6：[registration L252](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L250-L254) `issue_id__isnull=False` 对 NOT NULL CharField 恒真，属冗余条件。
- m7：错误分类参数化不全（仅 404/403/429/500/连接错误），缺 408/400/401/422/502/503/504/ReadTimeout 的断言。
- m8：TR-7.1 要求的 N+1 断言缺失（代码已加 select_related）。
- m9：成功路径 revision mismatch 分支不重置 attempts（executor L253-L262），下一轮携带历史计数。

### ADVISORY

- 租约 5 分钟相对最坏情况偏紧：Jira 客户端构造本身发 serverInfo 请求，收敛可能串行 3-4 次 60s HTTP；活 worker 超过租约被误回收时会与 C1 叠加产生并发。
- SQLite 下 select_for_update/skip_locked 完全无效（已验证 feature flags），互斥正确性必须在 MySQL/PostgreSQL 或线程级测试中验证，当前 CI 对此零防护。
- 已知项确认：mypy + django-stubs 在 Python 3.14 镜像的插件初始化崩溃属环境问题，本次未作为缺陷；test_version 仅宿主挂载运行受影响，baked 镜像全绿。

## Checkpoints

### AC 覆盖（Rules）

- [ ] AC-1 ~ AC-16 均有独立证据（见 spec.md）

逐项裁定：

| AC | 裁定 | 依据 |
|---|---|---|
| AC-1 | PASS | 同事务 upsert、revision 递增、回滚不留行（test_product_delete L109-L110）；登记服务 19 个单测 |
| AC-2 | PASS | inactive 全触发点短路；停用后仅余既有行（M1 场景除外） |
| AC-3 | **FAIL** | C1：bump 重置 PENDING 击穿租约，旧/并发执行可写远端甚至双创建 |
| AC-4 | **FAIL** | C1；且无 skip_locked 并发认领测试（TR-6.1 缺失） |
| AC-5 | **FAIL** | C2（认领后不收敛）、C3（attempts 复位后盲建）；首个退避 60s 与错误分类本身正确 |
| AC-6 | **FAIL（部分）** | 适配器幂等关闭/重开语义正确（GitHub state=open、GitLab state_event、Jira transition guard 均有测试），但认领后重开失效（C2） |
| AC-7 | PASS | 404 OPEN 重建/CLOSED 成功两分支均有测试，清身份逻辑正确 |
| AC-8 | PASS | pre_delete 顺序经 Django 源码+实测验证；DELETED×open/closed-with-note/404 矩阵齐全；行保留 product/issue_id |
| AC-9 | **FAIL** | M1：停用后 PENDING OPEN 行仍创建工单；分支/阈值/重启用 wiring 与登记集合正确 |
| AC-10 | PASS | stale 租约回收（执行器+命令+周期）、entrypoint 双环境接入、fresh 不抢占均有测试；成功不丢失 |
| AC-11 | PASS（附注） | 10 次预算、1m 首退避、终态 FAILED、手动复位逻辑正确；但手动复位 blind create（C3）、错误处理不识新修订（M2） |
| AC-12 | PASS（偏差） | API 三字段/管理列表/403/retry 202 与前端 Chip/Tooltip 均就位；M5（retry 过宽）、M6（产品筛选缺失） |
| AC-13 | PARTIAL | force=True wiring 正确且 registration 层有测试；接口本身零测试（M7） |
| AC-14 | PASS | 与 HEAD 基线逐字节对比：GitHub labels 合并/Markdown、GitLab confidential/add_labels/state_event、Jira labels/issue type/wiki/initial status 重开/closed guard 全部保持，仅追加 marker；Jira 灰色标记选择合理 |
| AC-15 | **FAIL（rubric ≤3）** | 连续修订×超时×重启组合下重复工单路径成立（C1/C3），且无组合场景测试 |
| AC-16 | PASS（rubric 4+，附注） | 分层/命名/测试基类与既有模式一致，black/isort/flake8 干净；m3/m4 等小瑕疵 |

### 质量（Rubric）
- [x] CP-U1: 分层/命名/类型标注与既有模式一致，最小侵入
- [x] CP-U2: 服务层无快照入队、无阻塞 HTTP 在事务中；查询集有索引支撑
- [x] CP-U3: 无残留旧任务引用、无重复工单回归机制（但存在 C1/C3 新风险）；迁移与 SET_NULL/CASCADE 设计验证通过
- [x] CP-U4: black/isort/flake8 干净；pylint 未在本次复跑；mypy 环境崩溃为已知接受项

### 回归
- [x] 全量 `unittests` 通过（标准镜像入口）：1621 tests OK；前端 lint/build 通过

## Review History

- 2026-10-03 独立子代理：**FAIL（3 critical, 7 major）**。门禁全绿（1621 后端测试 OK；前端 lint/build OK；black/isort/flake8 干净），但走查与容器内实测发现三类可导致重复工单/误收敛的正确性缺陷（C1 bump 击穿租约并发执行、C2 marker 认领后不收敛、C3 attempts 复位盲目重建），停用收敛缺口（M1），错误路径不识新修订（M2），标签不收敛（M3），Jira 误认兜底（M4），retry/筛选/测试覆盖缺口（M5-M7）。详见上表。
- 2026-10-03 R2 独立复审：**FAIL（1 critical, 3 major）**。I-1..I-11 的修复在"租约对登记事务可见"的常规时序下全部成立并有回归测试，C2 GitHub/GitLab 以真实 tracker 端到端复测通过；但主动构造的对抗探针发现 4 个残余正确性缺口：MySQL 默认 REPEATABLE READ 下请求快照早于 worker claim 时 bump 仍可擦除活租约（N1，可重复建工单）、停用与在途 create 竞争产生孤儿工单（N2）、create_uncertain 在期望已变 CLOSED/DELETED 时不对账（N3）、Jira 认领回的 CLOSED 工单因 initial status 缺失不重开（N4）。门禁：1639 测试（仅已知 test_version 宿主挂载环境项失败）、issue_tracker 131 测试 OK、前端 lint/build OK、black/flake8 干净、pylint issue_tracker 10.00/10。

---

# R2 独立复审（2026-10-03）

- **Result**: FAIL (1 critical, 3 major)
- **Reviewer**: 独立代码评审子代理（R2，全新视角，不信任实现说明）

## R2 验证环境与门禁证据

1. 全量后端（宿主代码直跑、SQLite 文件库）：
   `docker compose -f docker-compose/docker-compose-unittests.yml run --rm -e DJANGO_SETTINGS_MODULE=config.settings.unittests --entrypoint sh django -c "cd /home && rm -f /var/lib/sqlite/secobserve.db && python manage.py migrate --run-syncdb --noinput >/dev/null 2>&1; python manage.py test unittests --noinput"`
   → `Ran 1639 tests` / `FAILED (failures=1)`，唯一失败为 `unittests.commons.api.test_views.test_version`（已知宿主挂载环境项，baked 镜像通过，R1 已记录为非阻塞）。
2. issue_tracker 模块：`python manage.py test unittests.issue_tracker --noinput` → `Ran 131 tests ... OK`。
3. 前端：`npm run lint` → 退出 0；`npm run build`（tsc + vite/rolldown）→ `built in 1.72s`，退出 0。
4. 代码风格（容器内临时 pip 安装 black==26.5.1 / flake8==7.4.1 / flake8-isort / Flake8-pyproject / pylint==4.0.9 + pylint-django==2.8.0）：`black --check` 44 个应用文件 unchanged；`flake8 --max-line-length=120 --extend-ignore=I001,I003,I004,I005` 零输出；`pylint application/issue_tracker` → `10.00/10`。
5. 对抗性临时探针（评审结束后已删除，未留下任何源码改动；输出见下）：`unittests/issue_tracker/services/test_r2_adversarial.py`，9 个用例中 4 个 FAIL（N1/N2/N3/N5 证据），另加 1 个 Jira 专项探针 FAIL（N4 证据）；5 个用例通过（真实 GitHub 认领后 PATCH、手动重试先检索后创建、双 bump 恰好一次重排、stale 租约回收、不确定 update 退避）。

## R1 Findings 逐项裁定

| R1 | 裁定 | 证据 |
|---|---|---|
| **C1** 活租约被 bump 击穿 | **PARTIAL** | 常规时序修复成立：[_bump_status_row](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L322-L350) 对"可见的"新鲜 IN_PROGRESS 租约只更新 desired/issue_id/revision/last_error，保持 IN_PROGRESS+locked_at 且不调度（[L338-L342](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L338-L342)）；所有入口对活租约零远端写入：即时 trigger→claim 拒绝（[executor L80-L84](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L80-L84)）、周期 PENDING/stale 两条查询都不含新鲜 IN_PROGRESS（[dispatcher L41-L54](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_dispatcher.py#L41-L54)）、手动 retry 对 IN_PROGRESS 返回 409（[registration L283-L284](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L283-L284)、[views.py L47-L51](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/api/views.py#L47-L51)）；finish/error 路径 revision 不一致时重排 PENDING（[executor L287-L297](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L287-L297)、[L337-L347](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L337-L347)），双 bump 实测只触发一次重排、不会卡 PENDING。**但残余 N1：bump 的租约判定基于非锁定读取的行对象且随后盲写，MySQL 默认 RR 快照下仍可擦除活租约。** |
| **C2** marker 认领不收敛 | **PARTIAL** | [_adopt_issue](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L168-L175) 在 converge 前于事务内持久化 status_row.issue_id 与 observation.issue_tracker_issue_id；R1 原探针（真实 GitHubIssueTracker+mock HTTP，认领回 CLOSED 工单）复测调用序列现为 `GET search/issues → GET issues/42 → PATCH issues/42`，R1 的"无 PATCH"已修复；三平台 tracker 均在标记检索命中后再校验 token 在 body/description 中（[gh L73-L75](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/github_issue_tracker.py#L73-L75)、[gl L60-L62](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/gitlab_issue_tracker.py#L60-L62)、[jr L70-L74](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L70-L74)），恶意/无关工单不会被认领。**残余 N4：Jira 认领回的 CLOSED 工单因 `issue_tracker_jira_initial_status` 随不确定 create 丢失而不会被 transition，远端保持关闭却记 SUCCEEDED/open。** |
| **C3** attempts 复位盲建 | **PARTIAL** | 新增持久列 `create_uncertain`（[models L44-L46](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/models.py#L44-L46)、迁移 0001 L46）：不确定失败置位（[executor L360-L363](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L360-L363)）、确认成功清除（[L308](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L308)）、revision bump 与手动 retry 均保留（[registration L335-L336](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L335-L336)、[L286-L291](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L286-L291)）；检索门槛只看该标志、与 attempts 无关（[executor L131-L147](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L131-L147)），确定性失败后手动重试也先检索后创建（实测调用序 `get_correlation_token → find → create`）。**残余 N3：检索只在 desired=OPEN 时发生。** |
| **M1** 停用后仍建工单 | **PARTIAL** | PENDING 无 issue 行（非 DELETED）删除（[registration L239-L244](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L239-L244)）；executor 对 inactive+无 issue 删行取消、inactive+有 issue 收敛关闭（[executor L107-L117](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L107-L117)），两条均有测试；停用/启用钩子在产品 serializer 事务内（[serializers_product.py L275-L282](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/core/api/serializers_product.py#L275-L282)），重启用走 register_product_resync；代码走查未发现其它为 inactive 产品创建工单的路径。**残余 N2：create 在途时停用产生孤儿工单。** |
| **M2** 错误不感知新修订 | **FIXED** | 两个错误分支均在锁内重读并比较 revision，新修订立即回 PENDING（清 attempts/退避/错误）并重排（[executor L337-L347](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L337-L347)）；test_newer_revision_during_uncertain_failure_requeues_immediately 通过。 |
| **M3** 标签漂移 | **FIXED** | [_labels_satisfied](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L245-L250) 以产品标签为远端标签子集判据，OPEN/CLOSED 两个 currentness 分支均生效；test_label_drift_triggers_update 通过。 |
| **M4** Jira 误认兜底 | **FIXED** | [jira L70-L75](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L70-L75) 仅当 description 含 token 才返回，去掉 issues[0] fallback；GitHub/GitLab 同样二次校验 body/description。 |
| **M5** retry 状态门禁 | **FIXED** | reset 仅接受 FAILED/PENDING，否则 API 409（[views.py L47-L51](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/api/views.py#L47-L51)），running/succeeded 两个拒绝测试通过；前端按钮仅 FAILED/PENDING 渲染（[EmbeddedList L97](file:///Users/ding/Documents/swe/0910031/project-06/frontend/src/background_tasks/issue_tracker_sync/IssueTrackerSyncEmbeddedList.tsx#L94-L101)）。 |
| **M6** 产品筛选 | **FIXED** | 前端新增 Product id 过滤框（[EmbeddedList L59](file:///Users/ding/Documents/swe/0910031/project-06/frontend/src/background_tasks/issue_tracker_sync/IssueTrackerSyncEmbeddedList.tsx#L59)），后端 filterset 早支持 product（[filters.py L23](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/api/filters.py#L23)）。 |
| **M7** 测试缺口 | **FIXED（附注）** | issue_tracker 现 131 个测试：bump 保租约+二次 claim 被拒、认领后真实收敛、inactive 执行器、错误期新修订、synchronize 接口、停用/启用 serializer 钩子、409 均覆盖。仍无 MySQL/PG 上 skip_locked 真实交错测试（SQLite 下 FOR UPDATE/SKIP LOCKED 为 no-op，CI 结构性无法覆盖），沿用 R1 advisory。 |
| **m1** 删除说明重复 | **FIXED** | [executor L203-L211](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L203-L211)：说明已在且仍 open 时只调 close_issue 做状态转换，不二次追加；test_deleted_note_present_but_open_only_closes_state 通过。 |
| **m2** 未预期异常逃逸 | **FIXED** | execute_sync 兜底 `except Exception` 包成确定性错误落终态 FAILED（[executor L57-L61](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L57-L61)），dispatch 整批不再中断；test_unexpected_exception_becomes_terminal_failed 通过。 |
| **m3** 死代码 | **PARTIAL** | apps.py 永不存在的 signals 导入已移除（[apps.py L8-L17](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/apps.py#L8-L17)）；但 registry.py 把一个 dotted path 拆成两个相邻字符串仍在（[registry.py L14-L16](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/background_tasks/services/registry.py#L14-L16)），见 N6。 |
| **m4** 批量逐行 save | 未处理（MINOR，性能项，非正确性） | close_all/resync 仍逐行 upsert，仅新行 bulk_create。 |
| **m5** 删除吞掉不确定创建行 | **未修复，见 N5** | [registration L121-L125](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L121-L125) 实测删除期间无 issue_id 时直接删 row，探针实测 `rows after delete: []`。 |
| **m6** 冗余 isnull | **FIXED** | 已改为 `issue_id=""` / `.exclude(issue_id="")`。 |
| **m7** 错误分类参数化 | **FIXED** | 408/400、503/422、连接错误、403、500 均有 tracker 层断言。 |
| **m8** N+1 断言 | **PARTIAL** | 代码已 select_related（views_observation L110/L306），但测试仍无查询数断言，见 N7。 |
| **m9** mismatch 不重置 attempts | **FIXED** | 成功 mismatch（L293）与错误 mismatch（L341）均重置 attempts=0。 |

## R2 NEW Findings

### CRITICAL

- **N1（AC-3/AC-4/FR-6/NFR-2）：bump 对租约的判定基于可能过期的行视图并盲写，MySQL（默认 REPEATABLE READ）下活租约仍可被擦除，第二执行者可并发写远端、重复建工单。**
  位置：登记路径全程无行锁（grep 确认 select_for_update 只在 executor），行对象由普通 SELECT 取得（[registration L84](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L84)），[_has_fresh_lease](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L353-L358) 判定后 [_bump_status_row L344-L348](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L344-L348) 无条件盲写 `sync_state=PENDING, locked_at=None`。
  生产时序（仓库未设置隔离级别，MySQL 即 RR；ATOMIC_REQUESTS 下请求事务的一致性快照在首个读操作——认证/对象读取——时建立）：R1 提交后 Huey 消费者积压（3 worker、单次远端最长 60s，AC-3 正是"慢远端期间连续保存"场景），worker A 在 R2 请求开始之后才完成 claim 并提交；R2 事务快照中该行仍是 claim 前的 PENDING → `_has_fresh_lease=False`；InnoDB 的 UPDATE 为当前读，等待 claim 锁释放后直接把最新行覆盖为 PENDING/locked_at=None → R2 提交后 on_commit trigger（或周期扫描/手动 retry）被第二个执行者成功 claim，与 A 同时执行；两者在"远端无工单"时都 POST create → 永久重复工单。PostgreSQL（READ COMMITTED）下同一机制的窗口缩小为 SELECT 与 save() 之间的微秒级，但同样存在。注意 trigger 无 `@lock_task` 兜底（[dispatcher L32-L34](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_dispatcher.py#L32-L34)）。
  复现（临时探针，已删除；机制等价于 MySQL RR 快照过期）：取行的 PENDING 内存副本 → 另一已提交事务执行 claim（IN_PROGRESS+fresh locked_at）→ 对旧副本调 `_bump_status_row`。实测输出：`R2 PROBE C1 post-bump state: PENDING locked_at: None rev: 2`，随后 `_claim_status_row` 被 GRANTED（断言 IN_PROGRESS 失败）。
  建议：bump 在事务内用 `select_for_update()` 锁定该行并以锁定当前读重新判定（锁定读在 MySQL RR 下也是当前读），或改为单条条件 UPDATE（在 SQL 层按 sync_state/locked_at CASE 赋值）并按影响行数决定是否调度。

### MAJOR

- **N2（AC-9/FR-8）：停用集成与在途 create 竞争时，新创建的远端工单成为永久孤儿，重新启用后会再建一个。**
  [close_all_for_deactivation 的取消删除（registration L239-L244）不看活租约](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L239-L244)；worker A 在 claim 时缓存了 active 的 product（[executor L68-L69](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L68-L69)），停用在其 HTTP 期间提交：行被删除，A 仍按缓存配置 create；[_finish_success L273-L275](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L273-L275) 发现行已删除即 return，observation.issue_tracker_issue_id 不回写。探针实测：`Deactivation canceled 1 ...` / `R2 PROBE M1 create calls: 1 observation issue_id:`（空）。远端工单无任何本地记录，产品已停用不再登记；重启用后 resync 以无 issue 身份再 create → 重复工单。
- **N3（FR-10/FR-7/AC-5）：create_uncertain 的标记对账只在 desired=OPEN 时发生；不确定创建之后期望已转为 CLOSED（用户解决/状态变非 active、默认分支迁出、最低严重性上调）时，直接以"缺席即一致"成功，可能已创建的远端工单永远开放，且标志在 SUCCEEDED 时被清除，以后重开只会再建工单。**
  门槛条件硬编码 OPEN（[executor L131-L137](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L131-L137)），CLOSED+缺席直接返回成功（[L229-L230](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L229-L230)）。探针（resolved 观察结果 + create_uncertain + 远端实际有开放工单）实测：`R2 PROBE C3-closed find calls: 0 close calls: 0`。FR-10 要求"后续重试必须先按关联标记检索"，不区分当前期望；正确行为应是检索→认领→关闭。
- **N4（AC-5/AC-6）：Jira 认领回的 CLOSED 工单不会被重开——C2 在 Jira 平台仅部分修复。** 不确定 create 的响应丢失，`issue_tracker_jira_initial_status` 从未落库；认领只回填 issue id（[executor L168-L175](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L168-L175)），而 Jira update_issue 的重开 transition 以该字段为前提（[jira L90-L95](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L90-L95)），为空即跳过。真实 JiraIssueTracker + mock 客户端探针实测：`R2 PROBE C2 Jira transitions: [] initial status:`（空），断言 1 次 transition 失败；本地记 open/SUCCEEDED，远端保持 Closed。GitHub/GitLab 重开为无条件 state=open/state_event=reopen，不受此影响。

### MINOR

- **N5（=R1 m5，未修复）**：观察结果在不确定创建窗口内被删除时，[register_observation_deleted L121-L125](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L121-L125) 直接删 row，探针实测 `rows after delete: []`，远端"可能已存在"的工单失去 marker 对账与"删除说明+关闭"机会。N3 的 DELETED 同源问题。
- **N6（=R1 m3 残留）**：[registry.py L15](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/background_tasks/services/registry.py#L15) 仍把一个 dotted path 拆成两个相邻字符串字面量。
- **N7（=R1 m8）**：select_related 已加（views_observation.py L110/L306），但仍无 N+1 查询数断言测试。

## R2 CP/AC 核验

- [x] CP-R1 事务内只登记期望、后台按修订收敛
- [x] CP-R2 OPEN/CLOSED/DELETED 动作矩阵正确
- [ ] CP-R3 旧修订/重复触发零远端写——**可见租约下成立（有测试）；MySQL RR 快照过期窗口下不成立（N1）**
- [ ] CP-R4 skip_locked 串行认领——claim 机制在 MySQL 8/PostgreSQL 正确（skip_locked + IN_PROGRESS 守卫，双相同 Huey 消息由 claim 串行化）；bump 盲写可绕过（N1）；SQLite 无 FOR UPDATE，CI 零覆盖（沿用 advisory）
- [ ] CP-R5 不确定创建先标记检索——OPEN 路径三平台正确（真实 GitHub 端到端复测）；CLOSED/DELETED 不对账（N3）、Jira 认领后不重开（N4）
- [x] CP-R6 关闭/重开幂等（GitHub/GitLab 认领后收敛实测通过；Jira 认领 CLOSED 不重开见 N4）；确定性 4xx 立即 FAILED
- [x] CP-R7 404 清身份、OPEN 重建/CLOSED 成功
- [x] CP-R8 重启恢复（命令+entrypoint dev/prod+周期 stale 回收，fresh 不抢占）
- [ ] CP-R9 配置事件收敛——分支/阈值/重启用 wiring 与登记集合正确；停用与在途 create 竞争有孤儿（N2）
- [x] CP-R10 删除：pre_delete 同事务登记、行保留、说明+关闭幂等、404 完成
- [x] CP-R11 未启用集成零新行零任务
- [x] CP-R12 手动 synchronize 强制对账（接口测试就位）
- [x] CP-R13 API/前端可观察、retry 409 门禁、产品筛选
- [x] CP-R14 三平台标签/描述/关闭语义保持（pylint 10/10、逐适配器测试）
- [ ] CP-R15 组合场景端到端一致性——N1/N2/N3/N4 均为组合时序下的一致性缺口

| AC | R2 裁定 | 备注 |
|---|---|---|
| AC-1 | PASS | 同事务 upsert/revision/回滚 |
| AC-2 | PASS | inactive 全触发点短路 |
| AC-3 | **PARTIAL** | N1 |
| AC-4 | **PARTIAL** | N1；真实 skip_locked 交错仍无测试（SQLite 限制） |
| AC-5 | **PARTIAL** | N3、N4 |
| AC-6 | **PARTIAL** | N4（仅 Jira 认领场景） |
| AC-7 | PASS | |
| AC-8 | PASS（附注 N5） | |
| AC-9 | **PARTIAL** | N2 |
| AC-10 | PASS | |
| AC-11 | PASS | 退避/预算/手动复位、标志保留实测 |
| AC-12 | PASS | |
| AC-13 | PASS | |
| AC-14 | PASS | |
| AC-15 | **FAIL（rubric ≤3）** | 慢远端+连续修订（N1）、停用竞争（N2）下仍可重复/孤儿 |
| AC-16 | PASS（附注 N6/N7） | |

REVIEW: FAIL (1 critical, 3 major)

---

# R3 独立复审（2026-10-03）

- **Result**: FAIL (0 critical, 3 major)
- **Reviewer**: 独立代码评审子代理（R3，全新视角，不信任修复说明，全部结论经代码走查 + 容器内临时探针实测）

## R3 门禁证据（独立执行）

1. 全量后端（宿主代码直跑、SQLite 文件库，命令同 R2）：`Ran 1646 tests in 29.154s` / `FAILED (failures=1)`，唯一失败 `unittests.commons.api.test_views.test_version`（已知宿主挂载环境项，baked 镜像通过）。
2. issue_tracker 模块（删除全部临时探针后）：`Ran 138 tests in 0.525s ... OK`。
3. 前端：`npm run lint` 退出 0；`npm run build`（tsc + vite/rolldown）`✓ built in 1.93s`，退出 0（仅既有的 chunk 体积提示）。
4. 风格（容器内临时 pip 安装）：`black --check application/issue_tracker` → `24 files would be left unchanged`；`flake8 -j1 --max-line-length=120 --extend-ignore=I001,I003,I004,I005 application/issue_tracker` → `FLAKE8_CLEAN`。
5. 对抗探针（临时文件 `test_r3_adversarial.py` / `test_r3_p7.py`，评审后已删除，工作区无残留）：7 个用例，结果 P1/P2/P2b/P3 FAIL（新缺陷证据），P4/P5/P6 PASS（N1 与两个端到端组合场景）。关键输出：
   - `R3 P1 end state: row_state=SUCCEEDED row_desired=OPEN obs_issue=gh_new obs_closed=False close_calls=0`
   - `R3 P2 after inactive run: rows=0 find_calls=0 close_calls=0`
   - `R3 P2b: find_calls=0 create_calls=1 obs_issue=`
   - `R3 P3: raised=None rows=[] close_calls=0`，日志含 `Observation.NotUpdated: Save with update_fields did not affect any rows.`
   - `R3 P4: state=IN_PROGRESS locked_at=... rev=2`（二次 claim 被拒）
   - `R3 P5: create_count=1 close_calls=1 state=SUCCEEDED`（场景 a）
   - `R3 P6: find_calls=1 close_calls=1 create_retries=1 state=SUCCEEDED issue=gh_found`（场景 b）
   - `R3 P7 fallback row rev: 2`（SQLite 下 IntegrityError 回退有效，反证 CI 无法暴露 PG 问题）

## R2 Findings 逐项裁定

| R2 | 裁定 | 证据 |
|---|---|---|
| **N1** MySQL RR 下 bump 擦除活租约 | **FIXED** | 所有既有行变更均在 `select_for_update()` 锁内重读最新提交版本后再判定：[_bump_existing_status_row L357-L366](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L357-L366)（bump）、[reset_for_manual_retry L295-L312](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L295-L312)（手动重试）、[close_all L248-L255](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L248-L255)（停用取消）、[executor claim L67-L99 / finish L301-L302 / error L359-L360](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L67-L99)。锁范围：单行；批量仅停用路径一条锁定 SELECT，claim 端 `skip_locked=True` 且锁只在毫秒级短事务内持有（HTTP 在锁外执行），无多行行序交叉，无死锁面。on_commit 回调由最外层提交（即行锁释放）后触发，时序正确。MySQL RR 下 FOR UPDATE 为当前读、PG RC 下同样安全；探针 P4 模拟"claim 提交后到达的 bump"，行保持 IN_PROGRESS+locked_at、第二次 claim 被拒；场景 a（P5）实测只创建一次。**残余锁覆盖缺口见 R3-3（删除路径仍有未锁定的盲 DELETE）。** |
| **N2** 停用与在途 create 竞争孤儿 | **PARTIAL（新 R3-1）** | 行被"取消删除"的情形已修复：close_all 不再删活租约/uncertain 行；行确被取消（如租约过期后被删）而 worker 成功创建时，[_reregister_canceled_row L177-L204](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L177-L204) 按产品**当前**状态重建 CLOSED（停用）/OPEN（重启用），产品级联删除时跳过，正常 DELETED 成功（行在锁内 L329-L331 自删）不会误触发；测试 test_row_canceled_while_creating_is_reregistered_for_closing 通过。**但最常见的"新鲜租约"交错未覆盖**：close_all 保留活租约行不动，worker 用缓存在 claim 时的 product 计算 desired（[L106-L111](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L106-L111)），finish 也不复查产品状态（[L301-L339](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L301-L339)）→ 工单以 SUCCEEDED/OPEN 留在已停用产品，FR-8 的"关闭全部"被违反。见 R3-1。 |
| **N3** create_uncertain 对账硬编码 OPEN | **FIXED（残余归 R3-2）** | 标记对账对三种期望全部生效（[executor L132-L146](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L132-L146)）；observation=None 时 token 仅由 `status_row.observation_id` 构造（[L139-L140](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L139-L140)，token 助手 [base L14-L18](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/base_issue_tracker.py#L14-L18)）；三平台命中后二次校验 body/description 防误认（[gh L73-L75](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/github_issue_tracker.py#L73-L75)、[gl L60-L62](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/gitlab_issue_tracker.py#L60-L62)、[jr L70-L75](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L70-L75)）；find 命中但 GET 404 时才重建（无重复）；`create_uncertain` 生命周期正确：不确定置位（[L390-L394](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L390-L394)）、终态成功清除（[L338](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L338)）、revision bump/手动重试保留（[registration L382-L383](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L382-L383)、[L289-L310](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L289-L310)）、revision 竞争保活（[L317-L327](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L317-L327)）；CLOSED 找不到即完成且清标志、DELETED 找不到行删除，均有测试（P6 实测 find=1/close=1/无二次 create）。残余：停用取消路径绕过对账，见 R3-2。 |
| **N4** Jira 认领 CLOSED 工单不重开 | **FIXED（附注 R3-5）** | [_reopen_status L98-L110](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L98-L110)：有 initial_status 用之；否则取可用 transitions 中第一个名字不等于已配置关闭状态的目标；transitions 为 None/空时返回 `""`，[update_issue L94-L96](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L94-L96) 按真值跳过，不崩溃；重开成功后下一轮状态 OPEN 不再 transition，update 幂等；测试 test_update_issue_reopens_via_transitions_without_initial_status 通过。选择启发式的残余风险见 R3-5。 |
| **N5** 删除窗口吞掉不确定创建 | **PARTIAL（新 R3-3）** | 无 issue_id 但 `create_uncertain=True` 的行删除时保留为 DELETED（[registration L122-L126](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L122-L126)），DELETED×observation=None 的 token 对账闭环有测试。但**第一次 create 在途（尚未产生不确定失败、无标志、行带活租约）期间删除观察结果**时，[L127-L130](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L127-L130) 仍是未加锁的盲 DELETE，直接抹掉活租约行。见 R3-3。 |

## R3 NEW Findings

### CRITICAL

无。N1 类"租约击穿→并发双创建"主路径已闭合（P4/P5 实测），未发现可在常规分析师操作时序下重复建工单的新路径。

### MAJOR

- **R3-1（AC-9/FR-8，N2 残余）：在途 create 期间停用集成（新鲜租约交错），新建工单以 SUCCEEDED/OPEN 永久留在已停用产品，停用"关闭全部工单"的承诺被违反。**
  close_all 对活租约行的正确处理是"保留不删"（[registration L248-L254](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L248-L254)），但既不把它 bump 为 CLOSED，worker 一侧也没有任何补偿：执行器在 converge 开头用 claim 时缓存的 product 计算 desired（[executor L103-L111](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L103-L111)），[_finish_success L301-L339](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L301-L339) 只锁读状态行、不复查产品当前 active 状态；revision 未变（close_all 未碰该行）→ SUCCEEDED/OPEN。停用时 observation 的 issue id 尚为空（create 在途），故 observations_with_issue 循环（L258-L271）也漏掉它。产品保持停用期间所有登记短路、周期扫描跳过 SUCCEEDED，工单永不关闭（重启用后 OPEN 恰为期望，状态又"自愈"，故仅限持续停用场景可见）。
  复现（探针 P1，mock tracker，create 的 side_effect 内调用真实 `close_all_for_deactivation`）：实测 `row_state=SUCCEEDED row_desired=OPEN obs_issue=gh_new obs_closed=False close_calls=0`。
  建议：finish 在写入"新创建/新认领"的 issue_id 后用新鲜 product 判断；若已停用则将行重置为 CLOSED PENDING 再调度一次（所需新鲜 product 读取已在 `_reregister_canceled_row` 中存在）。

- **R3-2（FR-10/FR-8/AC-5，N3/N2 残余）：停用后 executor 取消 `create_uncertain` 行时不做标记对账，远端实际已建的工单成为孤儿；重新启用后以无标志新行盲目再建 → 重复工单。**
  [executor L112-L118](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L112-L118) 在"集成已停用且无 issue_id"时无条件 `status_row.delete()`，该早退发生在标记对账块（[L132-L146](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L132-L146)）之前，`create_uncertain` 被忽略。时序：create 超时（标志置位、退避中）→ 管理员停用 → 下一轮执行直接删行，远端可能已存在的工单失去对账；重新启用时 [register_product_resync L204-L215](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L204-L215) 为默认分支观察结果新建 `create_uncertain=False` 的行 → 对账块不触发 → 盲目 create → 同一观察结果两个远端工单（FR-10 明令该标志下"后续重试必须先检索"，不区分集成开关）。
  复现（探针 P2/P2b）：P2 模拟远端确有 `gh_orphan`，停用后执行 → `rows=0 find_calls=0 close_calls=0`；随后重启用 + resync + 执行 → `find_calls=0 create_calls=1`（重复工单）。
  建议：inactive 取消分支对 `create_uncertain` 行先跑标记检索——找到则认领并按 CLOSED 收敛一次，确认不存在才删行。

- **R3-3（FR-6/N1 锁覆盖 + N5 残余）：观察结果在第一次 create 在途期间被删除时，登记路径用未加锁的盲 DELETE 抹掉活租约行；worker 收尾对已删除观察结果保存身份字段抛出 NotUpdated 并被静默吞掉，远端工单成为开放孤儿且无通知。**
  [register_observation_deleted L120-L130](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L120-L130)：普通 SELECT 取行后，无 issue_id 且 `create_uncertain=False`（首次在途必然如此）时直接 `status_row.delete()`——这是 N1 加固后**唯一**仍对既有行无锁盲写、可擦除活租约的变更路径。worker create 成功后 finish 锁读发现行已不存在（[L302-L305](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L302-L305)）→ `_reregister_canceled_row`：claim 时 `select_related` 缓存的 observation 对象在 Python 中仍非 None（[L70](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L70)），守卫 `observation is None`（[L184](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L184)）判不出来；[L190-L191](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L190-L191) 对已删除行 `save(update_fields=...)` 在 Django 6 上抛 `Observation.NotUpdated`（0 行受影响），异常被 execute_sync 兜底（[L58-L61](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L58-L61)）包成确定性错误，而 `_handle_error` 对已消失的行直接 return（[L361-L365](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L361-L365)），无 FAILED 落库、无异常通知。
  复现（探针 P3，create 的 side_effect 内 `observation.delete()` 触发真实 pre_delete）：`R3 P3: raised=None rows=[] close_calls=0`，容器日志见 `Issue tracker synchronization 1 failed deterministically: NotUpdated: Save with update_fields did not affect any rows.`。窗口约单次 create HTTP（≤60s），但后果是永久性开放孤儿工单（删除说明+关闭永不发生）。
  建议：删除登记对活租约行（与 create_uncertain 同等处理）在锁内 bump 为 DELETED 而非删除——worker finish 的 revision-mismatch 路径会自动重排并以 DELETED 收敛；另 `_reregister_canceled_row` 应以 DB 存在性（`Observation.objects.filter(pk=...).exists()`）而非缓存对象判空。

### MINOR

- **R3-4（NFR-2/AC-1，PostgreSQL）**：[_upsert_status_row L334-L341](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L334-L341) 的 `except IntegrityError` 回退未包 savepoint；[bulk_create L218/L224](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L218-L224) 对并发首插碰撞无处理。PostgreSQL 下唯一约束违例会令整个事务进入 aborted 状态，catch 后的 `select_for_update()` 必抛 `current transaction is aborted`，在 ATOMIC_REQUESTS 中表现为整个业务请求 500/回滚（如双击保存同一观察结果、手动 synchronize 与分析师保存并发）；MySQL 语句级回滚下行为正确，SQLite 探针 P7（`fallback row rev: 2`）证实 CI 结构性测不到。不产生远端错误状态，仅请求失败可重试。建议插入包 `with transaction.atomic():` 形成 savepoint。
- **R3-5（AC-6）**：Jira [_reopen_status L106-L109](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/issue_trackers/jira_issue_tracker.py#L106-L109) 以"transition 名字不等于关闭状态名"选择目标，多终态工作流里可能选到非重开的转换（如另一个 Done 类状态）；更稳的判据是 transitions 的 `to.statusCategory`。典型工作流（关闭态仅放出 Reopen 类转换）不受影响。
- **R3-6（=R2 N6，未处理）**：[registry.py L14-L16](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/background_tasks/services/registry.py#L14-L16) 仍把一个 dotted path 拆成两个相邻字符串字面量。
- **R3-7（健壮性）**：[close_all_for_deactivation L275-L284](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_registration.py#L275-L284) 的 `select_for_update()` 查询集在函数自有原子块之外求值，依赖调用方事务（当前唯一调用方 serializer.update 在 ATOMIC_REQUESTS 内，故生产可用）；若未来在 autocommit 上下文复用将直接 TransactionManagementError。建议整体纳入显式 atomic。
- 沿用既有 advisory：SQLite 下 FOR UPDATE/SKIP LOCKED 为 no-op，真实锁交错仍无 CI 覆盖；mypy+django-stubs 在 Python 3.14 镜像崩溃为已知环境项；N+1 查询数断言（R2 N7）仍缺。

## R3 端到端走查（要求项 5）

- **场景 a：慢远端期间分析师连续保存 3 次（MySQL 语义）**——探针 P5 实测：worker claim rev1 后在 create HTTP 内收到两次 bump（锁内当前读，行保持 IN_PROGRESS、revision=3、不调度）→ create 恰好 1 次 → finish 发现 revision 不符，写入 issue_id 后重排 PENDING（[L317-L327](file:///Users/ding/Documents/swe/0910031/project-06/backend/application/issue_tracker/services/issue_sync_executor.py#L317-L327)）→ rev3 期望 CLOSED，远端 open → close 恰好 1 次 → SUCCEEDED。`create_count=1 close_calls=1`。旧修订零远端写、无重复工单。**通过**。
- **场景 b：create 超时→分析师解决→进程重启→退避调度**——探针 P6 实测：不确定失败置 create_uncertain+退避；解决事件 bump 为 CLOSED（标志保留、立即 PENDING）；模拟崩溃（IN_PROGRESS+30 分钟前 locked_at）→ recover_after_restart 复位 → dispatch_pending 认领 → 先 marker 检索（find=1）→ 认领 gh_found → close=1 → SUCCEEDED，create 仅最初失败的 1 次、无重建。手动重试复位路径代码同理（标志保留、409 门禁）。**通过**。

## R3 AC 裁定

| AC | R3 裁定 | 备注 |
|---|---|---|
| AC-1 | PASS（附注 R3-4） | 同事务登记；PG 并发首插的请求 500 为 MINOR |
| AC-2 | PASS | |
| AC-3 | PASS（升级） | N1 修复经 P4/P5 实测：锁内重读保租约、旧修订零写、create 仅一次 |
| AC-4 | PASS（沿用 SQLite advisory） | skip_locked+租约守卫；批量锁无死锁面 |
| AC-5 | PARTIAL | OPEN/CLOSED/DELETED 三期望对账全部正确（P6）；停用取消绕过对账（R3-2） |
| AC-6 | PASS（附注 R3-5） | 三平台重开/关闭幂等，Jira 无 initial status 回退有效 |
| AC-7 | PASS | |
| AC-8 | PARTIAL | 首次 create 在途删除窗口仍出孤儿（R3-3）；其余矩阵（含 uncertain 删除）通过 |
| AC-9 | PARTIAL | 在途 create×停用的新鲜租约交错留 OPEN 孤儿（R3-1）、uncertain×停用孤儿+重启用重复（R3-2） |
| AC-10 | PASS | P6 重启恢复实测通过 |
| AC-11 | PASS | |
| AC-12 | PASS | |
| AC-13 | PASS | |
| AC-14 | PASS | black/flake8 干净，适配器语义保持 |
| AC-15 | **FAIL（rubric 3）** | 主组合场景（连续修订×超时×重启，P5/P6）全部收敛且可观测；但停用/删除与在途 create 的三类时序组合仍可产生开放孤儿或重复工单（R3-1/2/3），未达"组合场景全部收敛"的 4 分线 |
| AC-16 | PASS（rubric 4） | 分层/命名/类型标注/测试基类一致；R3-4/R3-6/R3-7 为局部瑕疵 |

## Review History（追加）

- 2026-10-03 R3 独立复审：**FAIL（0 critical, 3 major）**。N1（锁）与 N4（Jira 重开）判定已修复；N3 所要求的 OPEN/CLOSED/DELETED 标记对账修复完成（P6 实测）；N2/N5 的修复在"行已被取消/删除"分支有效，但对抗探针发现 3 个残余时序缺口：新鲜租约 create 完成于停用之后仍记 SUCCEEDED/OPEN（R3-1）、停用取消 create_uncertain 行不对账且重启用盲目重建产生重复工单（R3-2）、观察结果在首次 create 在途被删除时未加锁的盲删抹掉活租约并引发 NotUpdated 静默失败（R3-3）；另记录 PG savepoint（MINOR）等 4 项小问题。门禁：全量 1646 测试（仅已知 test_version 宿主挂载项失败）、issue_tracker 138 测试 OK、前端 lint/build OK、black/flake8 干净；全部临时探针已删除。

REVIEW: FAIL (0 critical, 3 major)
