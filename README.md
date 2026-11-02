# JARVIS Robot Console

**JARVIS 桌面机器人工作台｜双相机监看软件**

当前版本：**v0.1.0**。这是为 [Project JARVIS：桌面积木共创与局部编辑的具身协作者](https://github.com/YellowSubmariner/JARVIS-An-Embodied-Co-Creator) 开发的原生 Windows 桌面辅助软件。主项目维护系统方案、机械臂接入与具身协作研究，本仓库维护可运行的桌面软件及其构建配置。

下载入口：[v0.1.0 Release](https://github.com/YellowSubmariner/JARVIS-Robot-Console/releases/tag/v0.1.0) · [Windows x64 压缩包](https://github.com/YellowSubmariner/JARVIS-Robot-Console/releases/download/v0.1.0/JARVIS-Robot-Console-v0.1.0-windows-x64.zip)

v0.1.0 提供 **Wrist Camera（腕部相机）** 和 **Astra Global Camera（外部全局相机）** 两个页面。软件界面目前为英文，本 README 以中文说明使用方法。

软件只接收相机图像，不发送机械臂运动、复位、固件升级或机器人配置命令，也不需要 Kortex 账号密码。

## 当前功能

- 在两个相机页面间切换，支持快捷键 `Ctrl+1` 和 `Ctrl+2`。
- 两个视频源使用独立采集进程；一个来源不可用时，不阻塞另一来源或界面。切换页面不会重新打开相机。
- 连接腕部 RTSP 视频流，默认地址为 `rtsp://192.168.1.10/color`。
- 按设备名称选择 USB 相机，默认名称为 `Astra Pro HD Camera`。找不到指定设备时，不会自动改用笔记本内置摄像头；可以刷新设备列表后明确选择其他已连接相机。
- 通过 `Connect`、`Disconnect` 和 `Reconnect` 连接、断开或重新连接视频源；连接中断后自动重试，并显示帧率和最近收帧时间。
- 通过 `Save snapshot` 保存当前实时画面。过期画面会变暗，并标明当前没有继续更新。

## 运行 Windows 发行版

从 [v0.1.0 Release](https://github.com/YellowSubmariner/JARVIS-Robot-Console/releases/tag/v0.1.0) 下载 Windows x64 压缩包，**完整解压**后运行 `JARVIS_Robot_Console.exe`。可执行文件与 `_internal` 文件夹必须放在一起。

打包后的应用不需要单独安装 Python。请将整个程序目录放在可写位置，以便保存设置、日志和截图。

如果将打包结果放在本仓库的 `Windows/` 目录中，也可以双击仓库根目录的 `launch.cmd`。启动器优先运行 `Windows/JARVIS_Robot_Console.exe`；找不到发行版时，依次尝试 `.venv`、Windows 的 Python 3.13 启动器，以及 `PATH` 上的 Python 3.13 解释器来运行源码。

## 设置、日志与截图

运行数据保存在可执行文件所在目录；从源码运行时，保存在本仓库根目录。

| 文件或目录 | 用途 |
| --- | --- |
| `data/settings.json` | 当前页面、腕部视频流地址和 USB 相机名称 |
| `data/app.log` | 应用诊断日志 |
| `screenshots/` | 用户保存的截图 |

例如，运行 `Windows/JARVIS_Robot_Console.exe` 时，设置位于 `Windows/data/settings.json`，截图位于 `Windows/screenshots/`。这些运行数据已排除在版本控制之外，可能包含私有视频流地址或现场图像，分享前请自行检查。

## 从源码运行

环境要求：**Windows、64 位 Python 3.13**，并安装官方 Python Windows 安装程序提供的 Tcl/Tk 支持。

在仓库根目录打开 PowerShell，执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

虚拟环境和依赖准备好后，也可以使用 `launch.cmd` 运行源码。应用不会自行安装依赖。

## 构建 Windows 可执行程序

在 Windows 上使用同一套 64 位 Python 3.13 环境执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath . JARVIS.spec
```

构建结果为 `Windows/JARVIS_Robot_Console.exe`，配套文件位于 `Windows/_internal/`。分发时应保留完整的 `Windows/` 目录。`requirements-build.txt` 将 PyInstaller 固定为 `6.22.3`。

GitHub Actions 的 Windows 工作流使用相同构建方式，并将结果上传为构建产物。构建成功只说明打包流程通过，不代表现场相机连接或实际图像输出已经验收。

## 验证记录与功能边界

已有本地验证记录覆盖 Astra USB 相机实时取图、页面切换、断开连接、保存截图，以及腕部视频源不可用时的处理。采集服务还验证过重连和采集进程的有界清理；英文发布副本通过了语法与视频流地址校验检查。

**v0.1.0 发布时尚未完成腕部实时画面验收，现场状态需另行确认。** 腕部相机页面和连接失败处理已经提供，机器人视觉模块能否上线及输出实际画面仍需单独验证；本软件不负责修复机器人内部视觉模块故障。

v0.1.0 尚未提供相机到机械臂的标定、物体识别、机械臂控制或 VLA 执行功能。

## 项目结构与后续扩展

```text
main.py                         桌面入口与多进程冻结支持
jarvis_camera/
  app.py                        窗口、导航、页面与画面更新
  capture.py                    视频采集进程、重试与清理
  settings.py                   本地设置与视频流地址校验
assets/                         应用图标等界面资源
requirements.txt                运行依赖
requirements-build.txt          构建依赖
JARVIS.spec                     Windows PyInstaller 构建配置
launch.cmd                      发行版或源码启动器
.github/workflows/windows-build.yml
```

Python 包名仍为 `jarvis_camera`，相机页面通过 `PAGE_DEFINITIONS` 定义。后续可以在导航中增加标定、机器人状态或任务页面，并复用现有采集服务。扩展时保持相机采集与 Tkinter 界面分离；新增机器人命令功能时，应单独设计控制路径，与视频监看区分。

主项目方案、硬件接入和阶段验收请查看 [JARVIS-An-Embodied-Co-Creator](https://github.com/YellowSubmariner/JARVIS-An-Embodied-Co-Creator)。

## 许可证

本仓库尚未选择许可证，目前未授予开源许可证。第三方依赖继续遵循各自的许可证。
