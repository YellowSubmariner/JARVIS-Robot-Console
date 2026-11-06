# JARVIS Robot Console

Windows 机械臂现场工具，支持腕部相机和 Astra 外部相机监看，以及手柄示范录制。主项目见 [JARVIS 项目仓库](https://github.com/YellowSubmariner/JARVIS-An-Embodied-Co-Creator)。

## 下载

从本仓库 [Actions](https://github.com/YellowSubmariner/JARVIS-Robot-Console/actions) 中选择成功的构建，下载 `JARVIS-Robot-Console-recording-preview-windows-x64`。构建产物在生成后、保留期内可下载，可能需要登录 GitHub。解压后运行 `JARVIS_Robot_Console.exe`，请保留同目录的 `_internal`。

原 [v0.1.0 Release](https://github.com/YellowSubmariner/JARVIS-Robot-Console/releases/tag/v0.1.0) 只有相机监看，不包含录制功能。

## 首次设置

1. 连接机械臂和两路相机，在软件中确认画面正常。
2. 打开“示范录制”，在“Kortex Python”处选择已经安装兼容 `kortex_api` 的 Python 环境中的 `python.exe`。软件会记住路径；相机监看本身不需要该环境。
3. 首次录制时输入机械臂密码。密码保存在当前 Windows 账户的凭据管理器中，之后使用相同机器人地址和用户名时不必重复输入；可通过页面按钮更新或清除。

## 录制示范

1. 在“示范录制”填写本次短任务，例如“把红色积木放到盒子里”，并选择保存位置。
2. 点击“开始录制”，等待界面提示相机和机器人反馈已启动。
3. 使用手柄完成一次示范，再根据实际结果点击“结束：成功”“结束：失败”或“中止／人工接管”。
4. 整理场景后录制下一条，重新填写本次任务。

软件只读取相机和机械臂状态，不发送机械臂运动命令。每条示范保存为一个独立文件夹，包含任务与结果、两路相机图像及时间索引、七关节和夹爪的实际反馈。默认保存在程序目录的 `recordings/` 中，也可以自行选择位置；请保留整个文件夹。

软件会尝试记录手柄事件，但当前尚未验证能收到手柄按键或原始控制命令。保存的是原始采集数据；密码不会写入采集文件。请不要将现场视频、设备记录或密码上传到代码仓库。

## 从源码运行

使用 Windows 64 位 Python 3.13：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

机器人录制所需的 Kortex Python 环境需单独准备。

## 许可证

本仓库尚未选择许可证，第三方依赖遵循各自的许可证。
