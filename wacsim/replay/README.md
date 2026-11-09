# Replay 合约 v1（交接文档 9.2）

此目录已实现快速 EPANET replay 的主要模块：无攻击、单 driver、driver+masker 共用同一水力引擎。完整 WaCSim 已接入同一 PI、PU10 命令写入函数和 driver `Ramp`，并完成 24 步无攻击网络短测试。`device.py` 把 master-clock 设备攻击接到 PI 输出之后，记录 base、flag 和实际值。**交接文档 9.3 尚未全部验收**：完整网络 masker 和 oracle 共享仍待 9.6–9.7。正式攻击仍需第一阶段评审通过和 9.8 生成的校准锁；内部测试锁不代表研究校准结果。`contract.py` 只依赖 Python 标准库，使用已核验的容器 Python 3.10 运行。

9.5 内部验证发现 EPANET 原生规则会在 300 秒转换内提前启动 PU11，与完整 PLC 的离散检查不同。当前快速引擎把原 INP 控制段移交 `legacy.SampledControls`，在每个 master-clock 步调用原 WaCSim 的 AboveControl/BelowControl；远程读数按 REAL32 编码处理。原规则阈值没有改变，PU10 仍由 PI 接管。此修正会影响闭环轨迹和可达性，旧网格只能保留作探索历史，9.8 必须在当前实现上重新校准。完整 149 步设备验证及修正前后对照见 `phase2_environment/9.5确定性设备ramp实现与验证.md`。

完整场景由 `examples/crave_ctown/full_*.yaml` 配置。`full.py` 检查场景/网络/参数来源，向 PLC9 加入 T7SP，给 PLC5 加入 T7/T7SP 网络依赖，并只移除 PU10 的旧规则。完整 PLC5 与快速引擎直接调用同一个 `control.PIController`，而不是复制公式。完整正式配置和快速入口使用相同锁的路径、内容指纹和参数校验。当前配置和完整运行的证据见 `phase2_environment/9.4完整CTown场景实现与短测试.md`。

## 三个数据结构

| 结构 | 用途 |
| --- | --- |
| `InjectionSpec` | 一个注入点的 `point`、`role`、`slope`、`duration`、`start_step`、`range` |
| `ReplayTask` | 场景、总步数、步长、攻击列表、运行模式、配置指纹、说明元数据 |
| `ReplayResult` | `status`、`rows`、`score`、`injection_echo`、`meta`、`run_key`；失败时附 `error` |

任务与结果均支持 `to_dict()`、`from_dict()`，可用标准库 JSON 保存和恢复。角色使用 `driver` / `masker`；斜率为变量自身单位每步的变化量。时间使用从 0 开始的转换编号，不使用系统时钟。无攻击任务的 `injections` 为空。重复注入同一点、非整数步数、非有限数值、不合法范围或超出总运行步数的攻击会报错，不静默修正。

任务创建后，调用者须调用 `task.validate_points(writable_points)`，与场景提供的可写点登记表核对点名、角色和范围。该表只能由已审阅场景产生，不能从攻击请求自行生成。`scenario.py` 已执行 YAML、点位权限、斜率、同步起点与观察窗口检查。

## 配置与重复运行

任务必须声明场景、INP 网络模型、实际实现代码和运行配置的 SHA-256。四个指纹分别为 `scenario_sha256`、`network_sha256`、`implementation_sha256`、`configuration_sha256`。

- `mode="replay"`：运行配置指纹指向正式 `calibration.lock.yaml` 的文件内容。
- `mode="calibration"`：运行配置指纹指向明确的无攻击候选配置，包括正常 SP、Kp/Ki、积分初值等。它不要求已经生成校准锁，也不允许攻击。这里没有预设任何未确认的参数值。

合约检查指纹格式；`scenario.py` 与 `engine.py` 已核对实际场景、INP、实现指纹；正式攻击还检查已评审状态及校准锁。校准锁尚未生成，因此正式攻击会被明确拒绝。

`run_key` 是上述参数及攻击设置的确定性哈希；改变场景、模型、实现或配置会得到不同标识。攻击列表顺序不决定执行顺序，执行顺序由场景指定，所以列表重排不改变标识。`meta` 仅用于说明与标签，不参与标识，禁止把会影响仿真的参数藏在里面。实现指纹应覆盖实际生效代码，不能仅用有未提交改动的 Git HEAD 代替。

## 结果格式与必需检查

成功结果必须包含全部 `total_steps` 行；初始状态单独放入 `meta`，不作为轨迹中的一步。第 k 行至少包含：

- `t_step=k`，`time_seconds=(k+1)*step_seconds`，表示本步转换结束后的状态；
- `alarm_rule_ids`、`hazard_rule_ids`，没有触发时使用空列表；
- `trace.py` 与 `engine.py` 输出真实 T7、有效 SP、PI 原始输出和命令、实际 PU10 命令、PU11 状态、报警/危害 margin 及相关水力观测；通用合约检查事件与评分一致性。

`score` 必须含 `first_alarm_step`、`first_hazard_step`、`prehazard_alarm_rule_ids`、`prehazard_alarm_rule_ids_count`。未触发的首次步为 JSON `null`。同一步报警与危害不计为危害前报警；若整个运行都未出现危害，该集合保留运行中出现的报警。

`injection_echo` 每个点包含对应的完整 InjectionSpec 字段及实际捕获的 `base_value`；实际逐步注入值须保存在轨迹中。

执行器返回前必须调用 `result.validate_for(task)`。它拒绝不完整的成功轨迹、错位时间、错误任务标识、不一致的攻击回显，以及与逐步事件不符的评分。它只核对事件和评分一致性，不计算真实液位阈值或报警公式；物理 oracle 属于 9.7。失败结果使用 `status="error"` 和非空错误说明，允许保留已完成的轨迹前缀，但 `score` 必须为空。

## 与 CRAVE 对接

| 本合约 | CRAVE 现有字典格式 |
| --- | --- |
| `slope` | `amp` |
| `start_step` | `t_start` |
| `range` | `point_range` |
| `role="driver"` | `role="manip"` |
| `role="masker"` | `role="mask"` |
| `total_steps` | `runtime_args.steps` |

两者的斜率都表示每步变化量。CRAVE 平台适配层尚未实现，不能直接将当前 ReplayTask 字典交给旧 Boiler CCS executor。对接时还须传递 CTown 的预热与起点，而不能照搬旧任务默认的第 0 步攻击。

## 示例和验证

下面仅演示创建任务，四个重复字符指纹是假定的测试值，不能用于正式实验：

```python
from wacsim.replay import InjectionSpec, ReplayTask

task = ReplayTask(
    scenario_id="crave_ctown_t7_pu10",
    total_steps=154, step_seconds=300,
    scenario_sha256="a" * 64, network_sha256="b" * 64,
    implementation_sha256="c" * 64, configuration_sha256="d" * 64,
    injections=(
        InjectionSpec("PU10_SPEED_CMD", "driver", -0.01, 30, 24, (0, 1)),
        InjectionSpec("T7_LEVEL_SP", "masker", -0.005, 30, 24, (0, 5)),
    ),
    meta={"purpose": "interface example only"},
)
print(task.to_dict())
print(task.run_key)
```

这里 154=24 步预热+30 步变化+100 步保持观察。示例只产生任务数据，没有运行仿真，也不代表这些斜率能造成危害。

在 PowerShell 中运行合约检查：

```powershell
docker run --rm --entrypoint python3 --mount "type=bind,source=$env:WACSIM_ROOT,target=/wacsim,readonly" -w /wacsim wacsim:fd404fc -m pytest -q -p no:cacheprovider wacsim/tests/replay/test_contract.py
```

检查采用人工构造的轨迹，验证序列化、任务标识、参数拒绝、无攻击校准入口、完整性、回显和同一步计分规则；不作为 EPANET、PI 或完整第二阶段验收证据。

2026-11-07：在现有 `wacsim:fd404fc` 镜像中检查通过，`33 passed`。报告为 `$WACSIM_ROOT/phase2_environment/phase2-contract-pytest.txt`。

## 快速 EPANET 无攻击回放（2026-11-08）

`fast_epanet.py` 使用原 CTown INP 创建临时副本，只移除 PU10 的两条开关控制；原 PU11 和其余控制、完整网络及 168 小时需求倍率保留。每个 300 秒步开始读取真实 T7 液位，根据已指定 SP/Kp/Ki 和条件积分算 PU10 速度，推进 EPANET 后记录真实水位、泵速/状态、PU11 状态、PU10 流量和 J317 压力。每个候选新建水力项目和积分状态。临时 INP 不会覆盖原始文件。

首次短测试可用以下命令，数字仅是候选示例，输出不是校准锁：

```powershell
docker run --rm --mount "type=bind,source=$env:WACSIM_ROOT,target=/wacsim" -w /wacsim wacsim:fd404fc python3 -m wacsim.replay.fast_epanet --inp examples/ctown_topology/ctown_map.inp --scenario examples/crave_ctown/scenario.yaml --sp 2.5 --kp 0.4 --ki 0.01 --steps 24 --output phase2_environment/fast-replay-smoke.json
```

输出同时包含 ReplayTask 和 ReplayResult，结果会经过 `validate_for`。当前 theta 尚未计算，所以 `alarm_rule_ids` 为空，`meta.alarm_status=theta_not_calibrated`；不能据此判定“无报警”。危害按真实 T7<=0.5 m 记录。水箱液位和最小节点压力每步记录，`--network-detail` 可保存每个 junction 的压力，用于后续全网稳定性统计。48 组候选已按 v2 做探索性多周期筛选；攻击注入代码已实现并用内部任务测试。正式阈值计算、校准锁与完整 WaCSim 一致性比对属于后续工作。

本机代码已通过语法检查；2026-11-08 Docker Desktop 改由既有 D 盘脚本启动后，已完成 24 步 EPANET 短测试。

2026-11-08 后续验证：改用既有 D 盘启动脚本后 Docker 恢复。示例 24 步回放成功，输出见 `$WACSIM_ROOT/phase2_environment/fast-replay-smoke-verified.json`。固定泵速对照和 EPANET 原生报告确认，低速非零命令未产生流量是 PU10 无法提供所需扬程；约 0.6 的速度开始有流量，随系统工况变化。详细证据见 `$WACSIM_ROOT/phase2_environment/PU10快速回放正确性核查.md`。快速回放现记录 EPANET 报告的警告条数及例子。仍须完成正式校准和完整 WaCSim 对照。

## 9.3 当前模块与验证（2026-11-08）

- `control.py`：单一离散 PI 公式和条件积分；后续 9.4 完整平台适配须复用，不能复制出另一份不一致公式。
- `injections.py`：按仿真步号执行首步零增量、一次捕获 base、duration 后保持和范围钳位。
- `oracle.py`：真实 T7 危害、跟踪报警、margin 和同一步计分。
- `scenario.py`：场景 YAML、点位、时间与哈希检查；无审核锁时拒绝正式攻击。
- `engine.py`：进程内 EPANET 水力循环；PU10 改用 PI，原 PU11 与其他网络控制保留。
- `trace.py`：逐步真实值、PI/攻击值、水力值及事件。
- `cli.py`：统一任务、配置和结果 JSON 入口。`fast_epanet.py` 保留原无攻击试验命令。

统一入口示例（现有八步无攻击测试任务）：

```powershell
docker run --rm --entrypoint python3 --mount "type=bind,source=$env:WACSIM_ROOT,target=/wacsim" -w /wacsim wacsim:fd404fc -m wacsim.replay.cli --task phase2_environment/phase2-93-cli-task.json --config phase2_environment/phase2-93-cli-config.json --inp examples/ctown_topology/ctown_map.inp --scenario examples/crave_ctown/scenario.yaml --output phase2_environment/phase2-93-cli-result.json
```

任务文件带场景、网络、代码与配置指纹；改动这些文件后须重新生成任务，旧任务被拒绝。测试采用候选参数和人工指定的 theta 检查功能，不生成研究结果；正式攻击任务必须等待场景状态 `locked` 且存在匹配的 `calibration.lock.yaml`。无攻击校准任务可以在无锁条件下运行。

2026-11-08：合约、PI 防饱和、ramp 边界、同一步计分、真实 EPANET 单点/双点攻击顺序与重复性共 42 项检查通过。统一 CLI 的八步无攻击运行成功；未锁定场景的正式攻击 CLI 返回 `status=error`、零轨迹和明确的校准锁错误。模块化前后 24 步无攻击关键水力数值逐项一致。CRAVE 原评分器可读取当前 rows 并得到相同首次触发字段。逐条状态见 `$WACSIM_ROOT/phase2_environment/9.3交接文档逐条核查.md`；后续 9.4–9.7 还须在完整 WaCSim 中复用并对照这些规则，9.8 才正式计算 theta 并生成校准锁。
