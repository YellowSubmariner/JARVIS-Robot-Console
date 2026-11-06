# 示范数据整理与 LeRobot 导出

这份说明对应 Robot Console 的 `dataset-preview` 版本。目标是让采集人员用现有手柄自然示范，软件负责记录和离线整理。无需先搭好 agent 框架，也无需先下载 π0.5。

## 1. 现场怎么用

1. 固定两路相机，确认画面、机器人反馈和磁盘空间正常。
2. 填写一句短任务，点击开始；等程序提示可以示范，再操作机械臂。
3. 完成动作并稳定停留约 1 秒，选择成功、失败或人工介入。
4. 等原始记录保存结束，继续下一条。同一任务文字会保留。
5. 在样本库中集中回看成功示范，确认三项检查后导出。

先录 5–10 条短样本，检查实际画面延迟、夹爪反馈范围、终态和存储占用，再扩大采集。机械臂复位和下一次摆物应在录制结束后完成。失败数据保留作分析，首版不自动作为成功模仿训练数据。

自动整理使用后台队列。已运行的整理可以与下一次录制并行；后续排队整理会避开正在录制的时段。大量导出及 LeRobot 转换期间不开始新录制。正常关闭会等待已有处理完成，不应强制结束进程。

## 2. 实际记录了什么

| 来源 | 原始内容 | 用途 |
| --- | --- | --- |
| 外部与腕部相机 | JPEG 图片、主机接收时间、序号、连接分段、写入情况 | 观察输入与时间检查 |
| Kortex 反馈 | 七关节实测角度、连续夹爪位置、请求和接收时间、故障/警告字段 | 状态与候选动作标签 |
| 采集界面 | 短任务、就绪时刻、结束请求、成功/失败/介入结果 | 每条示范的边界与任务 |
| 控制器事件（若能收到） | 原始通知 | 辅助分析；不视为已验证的控制指令 |

Kortex 读取进程不发送运动指令。程序没有从反馈中恢复原始控制目标，也没有测得相机曝光时间。请保留完整原始目录。

## 3. 时间对齐和标签生成规则

默认建立 20 Hz 的决策时间网格，即相邻格相差 50 ms。每格 `t` 的输入取**接收时间不晚于 t 的最近数据**，不把未来画面放进当前输入。

```text
observation(t) = 两路最近已接收画面 + [q1(t), …, q7(t), gripper(t)]
action(t)      = [q1(t + 50 ms), …, q7(t + 50 ms), gripper(t + 50 ms)]
```

这里的 q 和 gripper 都是对应网格处最近已接收的实测值。标签是“下一格实测位置重建”的候选目标，不是下发命令。实际动作采样延迟另记在 `source_ages_ms` 中；反馈不够密集时，连续格可能引用同一条反馈。

- 输入状态与动作均为 8 维，顺序为七个关节加一个夹爪。
- 关节角从度转换为弧度，保留原角度参考。遇到跨越 0°/360° 等大跳变时阻断，不对所有关节盲目展开角度。
- 夹爪百分比除以 100，保留连续 0–1 值：例如 35% 是 0.35，60% 是 0.60。现场需核实设备反馈的方向和量程。
- 夹到物体时，实测开度可能是 0.55，而控制器请求的闭合目标可能完全不同。若复现控制器必须使用请求开度或夹持力，需要从原控制程序另行记录这些量；不能用勾选检查代替这项验证。
- 第一版不做相机网络延迟补偿。共同主机时钟可以比较接收先后，但不能证明两路曝光同步。RTSP 缓存延迟须通过现场回看/标定检查。
- 较低帧率相机会在多个决策格复用最近画面；报告会记录复用次数。20 Hz 数据集不意味着每秒有 20 张不同图像。

整理区间取“允许开始示范”到“请求结束录制”之间，裁掉各数据流首尾没有共同覆盖的部分。裁剪时长写入报告。**中间缺口不会通过删掉坏帧并压缩时间来掩盖。**

## 4. 自动检查与人工检查

默认参数在 `PreparationConfig` 中定义，属于数据质量筛查阈值，不能代替机器人运动限制：

| 项目 | 默认值或规则 |
| --- | --- |
| 整理频率 | 20 Hz |
| 相机画面最大接收年龄 | 200 ms |
| 机器人反馈最大接收年龄 | 150 ms |
| 相邻原始关节反馈最大变化速度 | 120°/s，超过则需检查 |
| 角度跨界检查 | 相邻关节角差超过 180° 则阻断 |
| 末尾稳定窗口 | 0.5 s |
| 稳定窗口关节变化范围 | 每关节不超过 1° |
| 稳定窗口夹爪变化范围 | 不超过全量程的 0.02 |
| 最少候选样本 | 10；同时必须满足稳定窗口 |

还会检查任务文字、成功结果、完整结束、序号、时钟、反馈字段、图片文件、相机重连、丢帧/读取失败和关节故障。关节警告保留提示供检查。参数是首版候选设置，真实设备验证后可能需要调整；不要为了让坏数据通过而放宽阈值。

自动检查通过仅显示“等待检查”。回看界面提供两路图像、时间滑块、关节角和连续夹爪数值。三项检查默认不勾选：

1. 整段画面与关节/夹爪变化对应，没有明显时序错位。
2. 团队已经验证部署控制器能跟踪这种位置目标，特别是连续夹爪目标。
3. 示范实际成功，终态稳定，没有混入复位或人工介入。

保存检查结果只表示允许本次候选样本导出，不代表训练效果或实机安全已经验证。原始 `metadata.json` 和整理清单中的 `training_ready` 保持 `false`；人工决定单独保存在 `review.json`。重新整理会使旧检查失效，导出时会重算原始文件指纹并复核检查记录。

## 5. 文件如何组织

每条原始示范增加独立的整理目录，原始记录不改写：

```text
recordings/<episode>/
  metadata.json
  events.jsonl
  robot_feedback.jsonl
  top_frames.jsonl / wrist_frames.jsonl
  top_recording.json / wrist_recording.json
  top_frames/ / wrist_frames/
  prepared/
    manifest.json     # 参数、问题、指标、来源指纹、整理签名
    samples.jsonl     # 对齐后的状态、候选动作、图片路径、时间来源
    review.json       # 仅在人工保存检查后存在
```

普通导出包包含 `bundle.json`、每条示范的样本与引用图片、来源校验信息、转换脚本和说明。不会复制机器人连接设置、密码、原始控制器日志或本机完整路径。任务文字和现场图像属于数据内容，分享前仍需自行确认。

默认按完整示范约 70%/15%/15% 划分 train/val/test，固定随机种子 42，小数据集会调整数量以保证三组非空。至少三条是格式下限，不是训练数据量建议。相同来源的重复副本会拒绝；相同场景的不同演示仍可能高度相似，如需检验跨场景泛化，应另外留出采集场景或日期。

导出会拒绝未检查、被修改或动作定义不一致的输入，不静默跳过。导出/转换先写临时目录，完整成功后才生成目标目录，不覆盖已有结果。导出时还会检查 JPEG 解码和跨示范分辨率一致性，LeRobot 转换时再次校验。

## 6. 可选：配置 LeRobot 转换环境

**现在只采集时可以跳过这一节。** 普通数据包可以先保存在本机，之后在本机独立环境或云端转换。格式转换不加载 π0.5，也不需要 GPU；LeRobot 软件依赖仍会占用额外磁盘和下载流量。

本版本对接固定 LeRobot 提交 `9e5ee9859480e425a97a6836bb3fee479539beb5`。建议用独立 Python 3.12 环境，安装 `dataset` 依赖，不与旧 Kortex SDK 或 Console 的 Python 3.13 环境混用。以下以已安装 Git 和 Python 3.12 的 Windows 为例：

```powershell
py -3.12 -m venv .venv-jarvis-data
.\.venv-jarvis-data\Scripts\python.exe -m pip install --upgrade pip
.\.venv-jarvis-data\Scripts\python.exe -m pip install "lerobot[dataset] @ git+https://github.com/huggingface/lerobot.git@9e5ee9859480e425a97a6836bb3fee479539beb5"
.\.venv-jarvis-data\Scripts\python.exe -c "from lerobot.datasets.lerobot_dataset import LeRobotDataset; print('dataset import OK')"
```

视频编解码依赖必须可用；若安装或导入报错，应按[该提交的官方安装说明](https://github.com/huggingface/lerobot/blob/9e5ee9859480e425a97a6836bb3fee479539beb5/docs/source/installation.mdx)配置对应平台的 FFmpeg、PyTorch/TorchCodec，或将数据包移到 Linux 环境转换。这里的离线接口测试并未覆盖所有电脑上的第三方视频依赖。

配置后，在样本库点“转为 LeRobot…”，选择普通数据包、`.venv-jarvis-data` 中的 Python 和一个输出父目录即可。软件不自动安装依赖、登录账号或下载权重。转换器会核对 LeRobot 固定提交，不能仅凭包版本号相同认为兼容。

也可以在命令行使用数据包中附带的脚本（替换示例路径）：

```powershell
.\.venv-jarvis-data\Scripts\python.exe D:\datasets\bundle\convert_to_lerobot.py --bundle D:\datasets\bundle --output D:\datasets\lerobot-ready
```

输出是三个独立的 LeRobot 数据目录：

```text
lerobot-ready/
  train/        # 独立统计，只来自训练示范
  val/
  test/
  export.json   # 划分和软件版本记录
```

每条示范写入后调用 `save_episode()`，每个集合完成后调用 `finalize()`。图片解码为 RGB，状态/动作转为 float32，任务文字一并写入。训练使用 train 的统计和预处理器，评估沿用训练保存的处理器，不用 val/test 重新归一化模型输入。

## 7. 接下来才是 π0.5 微调

数据收集、动作定义验证和小样本转换跑通后，再租云 GPU、下载权重并训练。π0.5 的相机键映射、动作维数填充、动作块长度、归一化和末尾处理仍需与数据保持一致。部署还需要实现从模型位置目标到 Kortex 控制的适配；本 Console 不负责执行预测动作。

详见主项目的[π0.5 数据采集与微调部署指南](https://github.com/YellowSubmariner/JARVIS-An-Embodied-Co-Creator/blob/main/docs/PI05_Kinova_%E6%95%B0%E6%8D%AE%E9%87%87%E9%9B%86%E4%B8%8E%E5%BE%AE%E8%B0%83%E9%83%A8%E7%BD%B2%E6%8C%87%E5%8D%97.md)。其中通用示例应以本次导出的实际字段和固定版本为准；普通数据包本身不是可直接交给训练器的 LeRobot 目录。

实现依据：[LeRobotDataset 写入接口](https://github.com/huggingface/lerobot/blob/9e5ee9859480e425a97a6836bb3fee479539beb5/src/lerobot/datasets/lerobot_dataset.py)、[Python 与 dataset 依赖](https://github.com/huggingface/lerobot/blob/9e5ee9859480e425a97a6836bb3fee479539beb5/pyproject.toml)。
