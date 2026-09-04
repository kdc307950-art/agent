# VPN 质量基线快照（只读，用于 t5 完成后整体验收）

> 生成时间：2026-09-05（release-engineer，只读复验，未 git 提交）。
> 说明：本快照为**当前工作区某一时刻**的三绿/质量基线；因团队成员仍在并行修改
> backend/vpn（integration-engineer 的 t5、qa-engineer 的 B017 收尾等），计数可能随时间变化。
> 此文件仅用于验收参考，不作为冻结基准的最终唯一口径。

## 1. 静态质量计数

| 检查 | 命令 | 结果 | 说明 |
| --- | --- | --- | --- |
| mypy | `mypy --no-incremental src backend` | **0 errors**（120 source files） | 完全绿 |
| ruff | `ruff check src backend tests` | **4 errors** | 详见下方位置清单 |
| pytest | `pytest --co -q` | **658 collected**，0 收集错误 | >600，符合预期 |

### ruff 剩余 4 处（均在 qa-engineer 进行中的测试文件，未涉及 t1 冻结范围）

- `tests/test_vpn_diagnosis_closed_loop.py:14` I001 —— 导入排序（可自动修复）
- `tests/test_vpn_diagnosis_closed_loop.py:204` B017 —— 盲断言（qa-engineer 任务）
- `tests/test_vpn_diagnosis_closed_loop.py:220` B017 —— 盲断言（qa-engineer 任务）
- `tests/test_vpn_evidence_rules.py:16` I001 —— 导入排序（可自动修复）

> 备注：此前 captain 快照中的 `backend/vpn/approval.py:338 UP037`（`"ReissueActionRequest"` 引号）
> 与 `backend/vpn/reissue_service.py:47 F401`（`classify_reissue_outcome` 未用）均已清零
> （approval.py 已被 prior safe --fix 去引号，reissue_service.py 由 integration-engineer 在 t5 顺手清理）。

## 2. mypy 说明

- 全量 `mypy src backend` = **0 errors**（含 backend/vpn 全部模块：approval / reissue_service /
  closed_loop / sandbox_adapter / repository / diagnosis 等）。
- 若 t5 / qa 后续改动引入新的 mypy 错误，需即时上报 captain 定位归属。

## 3. Git 状态

- `git status --short` 当前共 **99 处更改**（大量 M 修改 + ? 未跟踪），包含：
  - VPN 相关（backend/vpn、run_vpn_*、vpn_eval_*、tests/test_vpn_*、frontend/src/api/vpn.* 等）；
  - 既有 helpdesk 基线（copilot / tickets / frontend / docs 等）；
  - 队友阶段内新增文件（closed_loop / sandbox_adapter / repository / evidence_rules 等）。
- **未提交**：按 captain 约定，git 分组提交待 t5 完成后统一进行。

### 已提交 commit（近 8 条，HEAD = `1cf40f9`）

| hash | message |
| --- | --- |
| 1cf40f9 | feat(vpn): VPN 故障智能服务台基线（版本冻结，阶段一）← HEAD |
| c6f391c | chore(v1): freeze demo scope, fixtures, gates and regression evidence |
| 2bcc943 | fix(demo/security/eval): credibility and demo hardening for V1 |
| d35e645 | feat(helpdesk): complete V1 internal IT service desk milestone |
| c2b5afa | fix(agent): harden workers, security, and data consistency |
| 3584241 | docs: README 新增工具调用与治理章节 |
| c59b10d | docs: 补充核心模块中文注释并同步项目文档 |
| 5e74355 | Merge remote-tracking branch 'origin/main' |

## 4. 结论 / 建议

- 三绿基线下线：mypy 0、pytest 658 收集无错、**ruff 仅剩 4 处且全在 qa 测试文件**
  （2 处 I001 可 safe `--fix`，2 处 B017 属 qa-engineer）。
- 无超过 10 处的 ruff/mypy 严重回退。
- 建议：qa 收尾 2 处 B017 并 safe `--fix` 2 处 I001 即可 `ruff` 归 0；t5 完成后由 captain
  统一做 git 分组提交。
