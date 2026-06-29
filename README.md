# PyAutoGUI 点击智能体

这个小工具会在本地打开一个网页：

1. 你在网页里输入目标任务。
2. 后端等待几秒，让你切回目标窗口。
3. 后端调用 macOS 自带 `/usr/sbin/screencapture` 抓取当前屏幕截图。
4. 后端把任务状态、最近上下文和截图发给 MiMo v2.5 多模态模型。
5. 模型返回 JSON 计划，程序按计划执行 `zsh`、截图观察、等待、PyAutoGUI 操作或向用户提问。
6. PyAutoGUI 操作后会自动等待并截图复查，直到任务完成、需要用户补充、暂停或达到轮次上限。

它支持同一窗口连续会话：模型需要验证码、确认或补充信息时，在同一个输入框回复后点击“发送并执行”，会继续当前上下文；左侧“+ 添加”才会创建新的智能体窗口。

## 上下文管理

后端会维护三层上下文：

- `active_stage_raw_events`：最近尚未压缩的完整上下文。
- `task_directory`：已压缩历史目录，只包含编号、标题、摘要和状态。
- `retrieved_step_details`：模型按编号请求后展开的历史详情。

模型可以通过 JSON 中的 `cu.compress=true` 让程序把当前阶段压缩成 `C1`、`C2` 这类编号；之后默认只发送目录。如果模型需要查看旧细节，会通过 `cr.requests` 请求编号，程序下一轮再把完整历史展开给模型。模型用完后可以通过 `ctx.release_retrieved` 释放展开内容。

## 启动

在这个目录运行：

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

然后打开：

```text
http://127.0.0.1:8765
```

网页里可以直接填写 MiMo API Key。也可以不在网页填，改用终端环境变量：

```bash
export MIMO_API_KEY="你的 MiMo API Key"
python3 app.py
```

网页中的“记住 API Key”会把 API Key、模型名和接口地址保存在当前浏览器的
`localStorage` 中；再次点击“取消记住”即可清除。

MiMo 的两类凭证使用不同接口，程序会根据 Key 前缀自动切换：

- `sk-...`：`https://api.xiaomimimo.com/v1`
- `tp-...`：`https://token-plan-cn.xiaomimimo.com/v1`

如果你使用的 MiMo 地址或模型名不同，可以这样改：

```bash
export MIMO_API_KEY="你的 MiMo API Key"
export MIMO_BASE_URL="https://api.xiaomimimo.com/v1"
export MIMO_MODEL="mimo-v2.5"
python3 app.py
```

## 使用方式

- 在网页输入目标任务，例如：`点击右上角登录按钮`。
- `截图前等待秒数` 建议设为 `3`。
- 点击 `发送并执行` 后，马上切回你要操作的窗口。
- 第一次建议先用 `只生成计划`，确认模型输出和下一步动作是否合理，再用自动执行。
- `暂停智能体` 会阻止后续轮次和动作；已经开始的单次命令完成后才会停下。
- 左侧 `+ 添加` 会创建新的智能体窗口，从空白上下文开始。

## 隐私与上传仓库

不要把 API Key、验证码、手机号、截图或运行日志提交到公开仓库。本项目的 `.gitignore` 默认排除了：

- `.env` / `.env.*`
- `runtime/`
- `server.log` / `*.log`
- Python 缓存目录

## 截图位置

每次请求都会保存两张图：

- 原始截图：`runtime/last_screenshot_original.png`
- 发给模型的压缩截图：`runtime/last_screenshot.png`

如果网页一直等待，先看这两张图有没有更新。能更新说明截屏没问题，问题多半在模型请求；不能更新说明是屏幕录制权限或 `screencapture` 阶段的问题。

## Mac 权限

如果不能截图或不能点击，需要在 macOS 打开权限：

- `系统设置 -> 隐私与安全性 -> 屏幕录制`
- `系统设置 -> 隐私与安全性 -> 辅助功能`

给你运行 `python3 app.py` 的终端或 IDE 授权。

## 坐标缩放

你的 Mac 当前情况是：

- PyAutoGUI 屏幕尺寸：`1440x900`
- 截图尺寸：`2880x1800`

也就是截图是 Retina 2 倍像素。脚本会让模型基于截图像素返回坐标，然后自动换算成 PyAutoGUI 能点击的坐标。
