# CRAVE × WaCSim：CTown 第一阶段场景契约（送审版）

> **当前状态（2026-11-09）**：场景已锁定，并已依据本地第一阶段提交 `6afca24c1704c691ca935a07e6511f3cdf620a99` 重新生成正式 `calibration.lock.yaml`。9.5 设备 driver、9.6 顺序 masker、9.7 统一 oracle、9.8 正式校准及 9.9/9.10 测试/验收工具均已完成；预热 k=0–23 的 telemetry 保留但正式 score 从 k=24 开始。完整 WaCSim 的无攻击、单 driver、driver+masker 三组差分验收（11.4）尚未运行。下文部分段落记录的是当时的阶段性状态，应以本说明、`scenario.yaml`、锁文件和 `phase2_environment/9.9-9.10最终测试与验收工具.md` 为准。

本目录包含第一阶段场景契约及第二阶段 CTown CRAVE 配置。契约当前状态为 **ready_for_external_review**：尚待评审闭环与 Git 提交。现有 WaCSim CLI 不会直接执行 scenario.yaml；新增 `full_*.yaml` 配置通过 `crave_ctown` 选项读取契约。完整平台已接入 T7SP、共享 PI 与设备 driver ramp；网络 masker 和完整 oracle 待 9.6、9.7 接入。送审并不表示第一阶段已经锁定。

## 9.4 完整平台场景

- `full_calibration_smoke.yaml`：24 步无攻击开发短测试，明确使用探索候选 SP=2.5 m、Kp=0.2、Ki_per_step=0.04。这些不是最终参数。
- `full_no_attack.yaml`：正式无攻击配置；只从与快速 replay 相同的 `calibration.lock.yaml` 读取参数。
- `full_driver.yaml`：单 driver 配置，设备 ramp 已实现，当前无锁时拒绝启动。
- `full_driver_masker.yaml`：双点配置；当前无锁时拒绝启动，完整网络 masker 仍待 9.6。

PLC9 提供 T7/T7SP，PLC5 从网络缓存读取两者并调用 `wacsim.replay.control.PIController`。PU10 两条旧控制规则在该场景中移除；PU11 与其他设备保留原 INP 规则。新场景初始化所有水箱的原 INP 液位，避免原数据库把未指定的初值写为 0；该初始化仅由 `crave_ctown` 选项启用。

完整运行保存 `configuration/intermediate.yaml`、场景副本、任务与参数 JSON、PLC CSV、ground truth 与 pcap；正式运行还会保存锁副本。只有物理进程全部结束且报告生成成功后，才写入含本次 execution ID 的 `full_run_complete.json`；启动器会拒绝缺少或过期的完成记录。

本次实现和短测试结果见 `../../phase2_environment/9.4完整CTown场景实现与短测试.md`。普通 CTown 配置继续使用原路径；此处没有生成正式校准锁。

## 9.5 设备 ramp

`wacsim/replay/device.py` 的 `FullDriverRamp` 在 PLC5 算完 PI 后读取 PU10 的未修改命令，再调用与快速 replay 相同的 `injections.Ramp`。时钟来自仿真 master clock；第一步零增量，base 捕获一次，duration 后保持限幅后的终值。保持期仍是攻击覆盖，所以 flag 继续为 1。

PLC5 CSV 同时保存 PI 命令、driver 修改前命令、base、实际命令、ramping/holding 和 flag；`ground_truth.csv` 中的 `CRAVE_PU10_RAMP` 是对应转换的攻击标记，`PLC5_driver_echo.json` 保存捕获的 base 与本次执行标识。完整内部测试及其边界见 `../../phase2_environment/9.5确定性设备ramp实现与验证.md`。内部测试使用独立临时场景和标注用途的测试锁，不能用来确定正式 PI、theta 或宣称 Reach–Avoid 成功。

## 当前候选配置与待完成事项

1. 用户转达师兄“合理即可”并授权制定候选：正常 SP 按 2.5、3.0、3.5 m 的顺序筛选；Kp=[0.2,0.4,0.8,1.2]，Ki_per_step=[0.005,0.01,0.02,0.04]，每个 SP 16 组，最多 48 组。
2. 积分初值为 0，积分范围为 [0,1/Ki_per_step]，采用条件积分防饱和。具体依据和选择顺序见《SP与PI候选配置.md》。48 组已完成探索性快速回放，尚未正式锁定。
3. 当前《多周期校准方案_v2.md》保留 24 步预热、三个完整周周期与 6048 步评分窗口，以安全边界、跟踪质量和最后两周的平均水位漂移判断正常运行。按 v2 探索性复算，22 组满足已计算条件；经验分位数算法和平分规则仍待确认，最终参数未定。旧《多周期校准方案.md》保留作历史对照。

其余需要签署的技术细节和逐项验收见《场景评审记录.md》与《第一阶段验收核查.md》。任何待确认参数定下后，应同步更新 scenario.yaml 和说明，再提交独立 Git commit。

## 已核实的原 CTown 事实

- 原模型：../ctown_topology/ctown_map.inp；PLC 清单：ctown_plcs.yaml；决策配置：ctown_decision_plc.yaml。WaCSim 源码提交 fd404fcf1d12ce72154c207ce59801d3c5cbdfc4。
- 原 INP 五组需求各有 168 个小时倍率，每 7 天重复；288 步只覆盖一天，不能视为完整需求周期。
- T7 初始液位 2.5 m，水箱允许液位 0–5 m；水力步长 300 s。PU10 连接 J307→J317，PU11 连接 J309→J323。
- 原 PU10 在 T7 低于 1.5 m 时开、高于 4.8 m 时关；PU11 在低于 0.5 m 时开、高于 3 m 时关。这些是水泵控制规则，不是本实验新增的报警。
- 原 CTown 400 步无攻击运行成功，约 50 分钟；24 步预热后 T7 最低 1.373 m，未达到 0.5 m。该运行使用原开关控制，不能直接校准新增 PI。
- 原 INP 流量单位为 LPS，WaCSim 临时 EPANET 模型与真值输出采用 CMH，1 LPS = 3.6 m³/h。原真值列 J317_LEVEL 实际读取压力，应映射为 J317_PRESSURE。

## 新实验定义

五个主变量及单位、权限见《变量定义表.md》。T7SP 是为本实验拟新增的目标液位，**不是** WaCSim 论文中的 T7S 上报液位。

- 控制：PLC5 使用未被攻击的 PLC9 T7 测量和本步生效的 T7SP，经离散 PI 输出 PU10 相对泵速 0–1；新场景禁用 PU10 原开关规则，保留 PU11 原规则。最终 PI 参数由第二阶段无攻击基线产生。
- 危害 H-T7-LOW：水力更新后的真实 T7_LEVEL <=0.5 m，边界包含，一次采样触发。PU11 对此危害可达性的影响须验证。
- 监视报警 A-T7-TRACK：abs(本步生效 T7SP - 水力更新后真实 T7_LEVEL) > theta，严格大于才报警；monitor_only=true，不触发保护动作。theta 按交接文档取无攻击残差 99.9% 经验分位数与 0.1 m 的较大值，具体分位算法待评审。
- 攻击：masker 在 PI 之前修改 T7SP，driver 在 PI 之后修改 PU10 速度命令；两者同一步开始、首步增量 0、逐步降低、到期后保持末值、按可写范围钳位。真实 T7 不直接修改。
- 时间：交接文档给定 24 步预热、第 24 步开始攻击、duration 5–120 步、结束 ramp 后继续保持并观察 100 步，两变量斜率均为 [-0.05,-0.001] 每步。duration 是逐步变化的长度，不是危害发生时刻。
- 评分：预热 k=0–23 的完整 telemetry 会保存，但不计入 CRAVE 的首次报警、首次危害或危害前报警集合；正式评分从攻击起点 k=24 的绝对步号开始，且同一步报警/危害仍不算危害前报警。

## 校准与进入下一阶段的门槛

第一阶段候选协议已给出 SP 优先顺序、固定 PI 网格和积分配置；稳定性与评分窗口 v2 已提出并探索性复算，仍待评审，分位算法等细节仍须闭环。第二阶段在无攻击的新 PI 场景校准：先淘汰触发危害者，再淘汰输出饱和比例 >0.25 者，并验证多周期稳定。在首个存在合格 PI 的 SP 下选择 T7 跟踪误差最小者，计算 theta 后写入 calibration.lock.yaml。正式攻击实验不得事后重新调参。

场景、INP、实现或校准锁变化时必须重新校准；若参数候选全部失败或危害不可达，应停止并回到第一阶段重新评审。第一阶段须有非作者评审和独立 Git 提交及 hash，完成前不得将本目录标为 locked。
