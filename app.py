#!/usr/bin/env python3
import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pyautogui
from PIL import Image


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
MIMO_PAYG_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_TOKEN_PLAN_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", MIMO_PAYG_BASE_URL).rstrip("/")
MIMO_MODEL = os.environ.get("MIMO_MODEL", "mimo-v2.5")
MIMO_TIMEOUT = int(os.environ.get("MIMO_TIMEOUT", "90"))
MIMO_NETWORK_RETRIES = max(1, int(os.environ.get("MIMO_NETWORK_RETRIES", "5")))
MAX_SCREENSHOT_EDGE = int(os.environ.get("MAX_SCREENSHOT_EDGE", "1440"))
RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
ORIGINAL_SCREENSHOT_PATH = RUNTIME_DIR / "last_screenshot_original.png"
MODEL_SCREENSHOT_PATH = RUNTIME_DIR / "last_screenshot.png"
MAX_AUTO_ROUNDS = int(os.environ.get("MAX_AUTO_ROUNDS", "30"))
AFTER_PYAUTOGUI_OBSERVE_SECONDS = float(os.environ.get("AFTER_PYAUTOGUI_OBSERVE_SECONDS", "0.5"))
DEFAULT_WAIT_SECONDS = float(os.environ.get("DEFAULT_WAIT_SECONDS", "2"))

SYSTEM_PROMPT = """你是电脑智能体规划器。只输出 JSON。

流程：用户目标 -> 先判断能否用 zsh 解决 -> zsh 输出回到下一轮模型；zsh 不适合/失败/需要看屏幕时调用 vision：截图 -> icon_detect 编号候选框 -> 多模态模型解析成 current_screen JSON -> 根据 current_screen 用 pyautogui 操作 -> 下一轮继续判断。

输入：user_goal 最终目标；current_focus 上轮目标；task_state 状态；task_directory 历史目录；current_screen 当前屏幕 JSON或未观察提示；active_stage_raw_events 含 user/zsh/vision/pyautogui 未压缩交流；retrieved_step_details 已展开历史。

同一 session 内后续 user 消息可能是补充信息、验证码、确认或修正，不一定是新任务；结合 user_goal、current_focus、task_state 和历史判断。needs_user 后的用户输入优先作为缺失信息继续当前任务。

上下文管理：
- active_stage_raw_events 只包含最近尚未压缩的完整上下文。
- task_directory 只包含已压缩历史的编号、标题、摘要和状态，例如 C1/C2；默认不要要求展开。
- retrieved_step_details 是你上轮通过 cr.requests 请求后，程序展开给你的完整历史。
- 当 active_stage_raw_events 中一段阶段性工作已经完成、失败、或可用一句话总结时，输出 cu.compress=true，并填写 cu.step.title/status/brief；程序会自动编号并压缩存储。
- 如果需要查看旧历史细节，输出 cr.need=true，并在 cr.requests 写 id 和原因；下一轮程序会把这些 id 放入 retrieved_step_details。
- 如果 retrieved_step_details 中某些 id 已经不再需要，输出 ctx.release_retrieved，例如 ["C2"]；仍需保留则放 ctx.retain_retrieved 或留空。
- 不要假设 task_directory 的标题摘要就是完整证据；证据不足时用 cr.requests 展开。

规则：
1. 每轮先判断 current_focus 是否完成，再决定继续、修正或进入下一目标。
2. 优先用 zsh 解决可由系统命令完成的事：打开应用、查文件、启动程序、读取本地信息、运行脚本、网络请求等。
3. zsh 不适合、zsh 失败、或必须知道屏幕里有什么时，输出 tool=vision；vision 只观察屏幕，不点击。
4. 通过 zsh 操作电脑后，zsh 成功只代表命令执行成功，不代表用户目标完成；下一轮必须先输出确认指令判断任务状态：优先用 zsh 查询应用/页面状态，zsh 查不到、结果不可信、或必须看视觉信息时才用 vision。确认成功后才可 finish.done=true；未确认前禁止重复同一条 zsh 操作。
5. 只有 current_screen 有元素坐标时才用 pyautogui；点击优先用 center，center 已是 PyAutoGUI 坐标。
6. PyAutoGUI 只执行，不观察；执行后如需确认，下一轮用 zsh 或 vision。
7. 程序执行 pyautogui 后会自动等待 0.5 秒、截图、再把新截图发回下一轮；0.5 秒截图只用于判断是否有即时反馈，不能因为 0.5 秒没完成就判断失败。
8. 如果 current_screen 显示正在加载、转圈、进度条、按钮灰化、页面骨架屏、处理中等状态，输出 wait：op.tool="none"，op.action={"type":"wait","seconds":2}；wait 不调用任何工具，由程序等待后截图再问你。
9. 连续多次截图无变化且累计超过 8-15 秒，才判断可能卡住；如果出现错误弹窗/超时文案，直接处理错误。
10. 不重复已成功步骤，除非当前结果显示失效。
11. 遇到错误、失败或没有进展时，不要机械重复上一动作；先根据最新截图、工具结果、last_error 和历史步骤判断最可能的阻塞原因，再选择能验证该原因或绕过问题的新动作。
12. 处理问题时，r 用短句写明“当前问题、判断依据、新策略”，但不要输出详细思维过程；op 必须落实这个新策略。
13. 如果连续两轮 current_focus 相同且界面/状态没有实质变化，必须主动切换解决方式，例如查询真实应用状态、重新观察界面、处理遮挡弹窗、刷新/后退/重新打开页面、改用另一条菜单或命令路径、检查权限/网络/输入格式；根据具体问题选择，不要固定套用。
14. 除非涉及登录、验证码、授权、隐私、高风险确认或只能由用户完成的操作，否则 ask_user 前至少尝试一种安全且不同的自助恢复方法；仍失败时说明已经尝试什么以及需要用户具体做什么。
15. last_error 不空先处理错误；task_state 与 current_screen 矛盾时，以最新 zsh/vision 结果为准。
16. 发送、删除、购买、付款、授权、密码、验证码、隐私信息必须 ask_user。
17. 只有在关键信息缺失、登录/权限/验证码/人工确认、任务风险高、连续观察仍无法可靠判断、或工具失败且无法自行恢复时，才 ask_user；ask.q 用一句话说明需要用户做什么或确认什么，ask.missing 填缺失信息/需要用户动作。
18. 需要向用户说明进度、结果、错误或请求协助时填写 user_message：show=true，text 使用一句简短自然语言，kind 取 info/success/warning/question/error；不要在其中输出内部推理。没有必要对用户说话时 show=false、text=""。
19. ask_user 时 user_message.kind="question" 且内容应与 ask.q 一致；任务完成时 user_message.kind="success" 并在 text 简述结果。
20. 历史默认只看 task_directory；仅错误/矛盾/目录不足时请求展开历史 id。
21. 阶段完成、失败、等待用户、或可一句话总结时 cu.compress=true；title 写做了什么，brief 写关键结果/错误/下一步。
22. 无法判断时用低风险动作：zsh 查询、vision 观察、wait、ask_user 或请求历史。
23. 短句；空值用 ""/false/[]/null。

Zsh：{"tool":"zsh","i":"目的","command":"open -a 'Google Chrome'","cwd":"","timeout_ms":60000}
zsh确认示例：{"tool":"zsh","i":"确认Chrome当前页面","command":"osascript -e 'tell application \\"Google Chrome\\" to get URL of active tab of front window' -e 'tell application \\"Google Chrome\\" to get title of active tab of front window'","cwd":"","timeout_ms":60000}
Vision：{"tool":"vision","i":"观察当前屏幕","action":{},"actions":[],"ask":{"q":"","missing":""}}
PyAutoGUI 动作：click,double_click,right_click,move,drag,scroll,type_text,press_key,hotkey,wait。
Wait：{"tool":"none","i":"等待加载后复查","action":{"type":"wait","seconds":2},"actions":[],"ask":{"q":"","missing":""}}
Ask_User：{"tool":"ask_user","i":"请求用户帮助","action":{},"actions":[],"ask":{"q":"请先在目标窗口完成验证码，然后告诉我继续。","missing":"验证码需要人工处理"}}

输出：
{"r":"判断","fe":{"pf":"上轮focus","done":false,"ev":"证据","err":""},"nf":"下一focus","cr":{"need":false,"requests":[{"id":"C1","r":"原因"}]},"op":{"tool":"zsh|vision|pyautogui|none|ask_user","i":"目的","command":"","cwd":"","timeout_ms":60000,"action":{"type":"","x":0,"y":0},"actions":[],"ask":{"q":"","missing":""}},"user_message":{"show":false,"text":"","kind":"info|success|warning|question|error"},"cu":{"compress":false,"step":{"id":"","title":"","status":"success|failed|in_progress","brief":""}},"ctx":{"retain_retrieved":[],"release_retrieved":[]},"state":{"app":"","page":"","browser":"not_opened|opening|opened|failed|unknown","found":false,"type":"","error":null},"finish":{"done":false,"answer":""}}

zsh 示例：{"tool":"zsh","i":"打开Chrome","command":"open -a 'Google Chrome'","cwd":"","timeout_ms":60000,"action":{},"actions":[],"ask":{"q":"","missing":""}}
vision 示例：{"tool":"vision","i":"zsh无法判断当前界面，观察屏幕","command":"","action":{},"actions":[],"ask":{"q":"","missing":""}}
ask_user 示例：{"op":{"tool":"ask_user","i":"需要用户完成登录验证","command":"","action":{},"actions":[],"ask":{"q":"请在浏览器里完成登录验证，然后告诉我继续。","missing":"登录验证需要人工确认"}},"user_message":{"show":true,"text":"请在浏览器里完成登录验证，然后告诉我继续。","kind":"question"}}
请求历史示例：{"cr":{"need":true,"requests":[{"id":"C3","r":"需要查看之前打开网站的具体 URL"}]},"ctx":{"retain_retrieved":[],"release_retrieved":[]}}
释放历史示例：{"ctx":{"retain_retrieved":[],"release_retrieved":["C3"]}}
单动作 op：{"tool":"pyautogui","i":"点击 current_screen.elements 中的 E3","action":{"type":"click","x":520,"y":88,"button":"left","clicks":1},"actions":[],"ask":{"q":"","missing":""}}
多动作 op：{"tool":"pyautogui","i":"输入并确认","action":{},"actions":[{"type":"click","x":520,"y":88},{"type":"type_text","text":"天眼查"},{"type":"press_key","key":"return"}],"ask":{"q":"","missing":""}}"""

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>电脑智能体规划器</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #20242a;
      --muted: #68707c;
      --line: #dfe4ea;
      --accent: #1f7a5a;
      --accent-strong: #166246;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }
    .app-shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 230px minmax(0, 1fr);
    }
    aside {
      border-right: 1px solid var(--line);
      background: #fff;
      padding: 16px 12px;
    }
    .sidebar-title {
      font-size: 13px;
      font-weight: 750;
      color: var(--muted);
      margin: 0 0 10px;
    }
    .add-session {
      width: 100%;
      margin-bottom: 12px;
    }
    .session-list {
      display: grid;
      gap: 8px;
    }
    .session-item {
      width: 100%;
      text-align: left;
      background: #f7f9fb;
      color: var(--text);
      border: 1px solid var(--line);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .session-item:hover {
      background: #eef2f6;
    }
    .session-item.active {
      background: #e7f5ee;
      border-color: #8fc5ad;
      color: #14543d;
    }
    main {
      width: min(1040px, calc(100vw - 32px));
      margin: 28px auto;
      display: grid;
      gap: 16px;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
    }
    .sub {
      color: var(--muted);
      margin: 4px 0 0;
      font-size: 14px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    label {
      display: block;
      font-size: 13px;
      font-weight: 650;
      margin-bottom: 8px;
    }
    textarea, input {
      width: 100%;
      border: 1px solid #cbd3dc;
      border-radius: 6px;
      padding: 10px 12px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }
    textarea {
      min-height: 112px;
      resize: vertical;
    }
    .row {
      display: grid;
      grid-template-columns: 140px 1fr;
      gap: 12px;
      align-items: end;
      margin-top: 12px;
    }
    .field {
      margin-top: 12px;
    }
    .key-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
    }
    .key-row button {
      min-width: 130px;
      white-space: nowrap;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: flex-end;
    }
    button {
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
    }
    button:hover { background: var(--accent-strong); }
    button.secondary {
      background: #eef2f6;
      color: #1f2933;
      border: 1px solid #cbd3dc;
    }
    button.secondary:hover { background: #e2e8ef; }
    button:disabled {
      cursor: wait;
      opacity: .65;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #101820;
      color: #e7eef7;
      border-radius: 6px;
      padding: 12px;
      min-height: 120px;
      font-size: 13px;
    }
    .preview {
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      display: none;
    }
    .hint {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .error { color: var(--danger); }
    .agent-message {
      margin: 0;
      min-height: 64px;
      padding: 12px 14px;
      border-left: 4px solid #64748b;
      background: #f6f8fa;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .agent-message[data-kind="success"] {
      border-left-color: #15803d;
      background: #f0fdf4;
    }
    .agent-message[data-kind="warning"],
    .agent-message[data-kind="question"] {
      border-left-color: #b45309;
      background: #fffbeb;
    }
    .agent-message[data-kind="error"] {
      border-left-color: var(--danger);
      background: #fef2f2;
    }
    @media (max-width: 720px) {
      .app-shell { grid-template-columns: 1fr; }
      aside {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .session-list {
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      }
      .row { grid-template-columns: 1fr; }
      .key-row { grid-template-columns: 1fr; }
      .actions { justify-content: stretch; }
      button { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
  <aside>
    <p class="sidebar-title">智能体窗口</p>
    <button class="add-session" id="addSessionBtn" type="button">+ 添加</button>
    <div class="session-list" id="sessionList"></div>
  </aside>
  <main>
    <header>
      <h1>电脑智能体规划器</h1>
      <p class="sub">输入目标任务，后端会持续截图、询问 MiMo 并执行 JSON 计划；点击动作后 0.5 秒会截图复查。</p>
    </header>

    <section class="panel">
      <label for="task">目标 / 回复</label>
      <textarea id="task" placeholder="输入目标任务；如果智能体向你提问，就在这里回复并继续发送。"></textarea>
      <div class="field">
        <label for="apiKey">MiMo API Key</label>
        <div class="key-row">
          <input id="apiKey" type="password" autocomplete="off" placeholder="可留空；留空时使用终端里的 MIMO_API_KEY">
          <button class="secondary" id="rememberApiBtn" type="button">记住 API Key</button>
        </div>
        <p class="hint" id="apiKeyStatus">保存后仅存放在当前浏览器中。</p>
      </div>
      <div class="row">
        <div>
          <label for="model">模型名</label>
          <input id="model" value="mimo-v2.5">
        </div>
        <div>
          <label for="baseUrl">接口地址</label>
          <input id="baseUrl" value="https://api.xiaomimimo.com/v1">
        </div>
      </div>
      <div class="row">
        <div>
          <label for="delay">截图前等待秒数</label>
          <input id="delay" type="number" min="0" max="30" step="1" value="3">
        </div>
        <div class="actions">
          <button class="secondary" id="dryRunBtn">只生成计划</button>
          <button class="secondary" id="pauseBtn">暂停智能体</button>
          <button id="runBtn">发送并执行</button>
        </div>
      </div>
      <p class="hint">建议保留 3 秒：点击按钮后立刻切到你想操作的应用窗口。</p>
    </section>

    <section class="panel" aria-live="polite">
      <label>智能体消息</label>
      <p class="agent-message" id="agentMessage" data-kind="info">模型暂时没有需要告诉你的消息。</p>
    </section>

    <section class="panel">
      <label>运行结果</label>
      <pre id="log">等待任务...</pre>
    </section>

    <section class="panel">
      <label>本次发送给模型的截图</label>
      <img class="preview" id="preview" alt="截图预览">
      <p class="hint">如果这里显示的是本网页，说明截图前没有及时切回目标窗口。</p>
    </section>
  </main>
  </div>

  <script>
    const taskEl = document.getElementById('task');
    const apiKeyEl = document.getElementById('apiKey');
    const modelEl = document.getElementById('model');
    const baseUrlEl = document.getElementById('baseUrl');
    const delayEl = document.getElementById('delay');
    const logEl = document.getElementById('log');
    const agentMessageEl = document.getElementById('agentMessage');
    const previewEl = document.getElementById('preview');
    const runBtn = document.getElementById('runBtn');
    const dryRunBtn = document.getElementById('dryRunBtn');
    const pauseBtn = document.getElementById('pauseBtn');
    const addSessionBtn = document.getElementById('addSessionBtn');
    const sessionListEl = document.getElementById('sessionList');
    const rememberApiBtn = document.getElementById('rememberApiBtn');
    const apiKeyStatusEl = document.getElementById('apiKeyStatus');
    const API_CONFIG_STORAGE_KEY = 'mimo_agent_api_config_v1';
    const SESSION_STORAGE_KEY = 'mimo_agent_sessions_v1';
    const CURRENT_SESSION_STORAGE_KEY = 'mimo_agent_current_session_v1';
    const PAYG_BASE_URL = 'https://api.xiaomimimo.com/v1';
    const TOKEN_PLAN_BASE_URL = 'https://token-plan-cn.xiaomimimo.com/v1';
    let clientRunVersion = 0;
    let sessions = readSessions();
    let currentSessionId = localStorage.getItem(CURRENT_SESSION_STORAGE_KEY);

    function readSavedApiConfig() {
      try {
        return JSON.parse(localStorage.getItem(API_CONFIG_STORAGE_KEY) || 'null');
      } catch {
        return null;
      }
    }

    function syncBaseUrlForKey() {
      const key = apiKeyEl.value.trim().toLowerCase();
      const current = baseUrlEl.value.trim().replace(/\\/+$/, '');
      if (key.startsWith('tp-') && (!current || current === PAYG_BASE_URL)) {
        baseUrlEl.value = TOKEN_PLAN_BASE_URL;
      } else if (key.startsWith('sk-') && (!current || current === TOKEN_PLAN_BASE_URL)) {
        baseUrlEl.value = PAYG_BASE_URL;
      }
    }

    function refreshRememberState(message = '') {
      const saved = readSavedApiConfig();
      const isCurrentSaved = Boolean(
        saved &&
        saved.apiKey &&
        saved.apiKey === apiKeyEl.value.trim()
      );
      rememberApiBtn.textContent = isCurrentSaved ? '取消记住' : '记住 API Key';
      apiKeyStatusEl.textContent = message || (
        isCurrentSaved ? 'API Key、模型和接口地址已保存在当前浏览器中。' : '保存后仅存放在当前浏览器中。'
      );
    }

    function makeSessionId() {
      return 's_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
    }

    function readSessions() {
      try {
        const parsed = JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY) || '[]');
        return Array.isArray(parsed) ? parsed : [];
      } catch {
        return [];
      }
    }

    function saveSessions() {
      localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(sessions));
      localStorage.setItem(CURRENT_SESSION_STORAGE_KEY, currentSessionId);
    }

    function createSession(title = '新任务') {
      const session = {
        id: makeSessionId(),
        title,
        draft: '',
        lastLog: '等待任务...',
        lastMessage: null,
        screenshotDataUrl: '',
        createdAt: Date.now(),
        updatedAt: Date.now()
      };
      sessions.unshift(session);
      currentSessionId = session.id;
      saveSessions();
      return session;
    }

    function currentSession() {
      let session = sessions.find(item => item.id === currentSessionId);
      if (!session) {
        session = sessions[0] || createSession('新任务');
        currentSessionId = session.id;
        saveSessions();
      }
      return session;
    }

    function titleFromText(text) {
      const compact = text.replace(/\\s+/g, ' ').trim();
      return compact ? compact.slice(0, 18) : '新任务';
    }

    function persistCurrentSession() {
      const session = currentSession();
      session.draft = taskEl.value;
      session.lastLog = logEl.textContent;
      session.lastMessage = {
        text: agentMessageEl.textContent,
        kind: agentMessageEl.dataset.kind || 'info'
      };
      session.screenshotDataUrl = previewEl.getAttribute('src') || '';
      session.updatedAt = Date.now();
      if (taskEl.value.trim()) {
        session.title = titleFromText(taskEl.value);
      }
      saveSessions();
      renderSessions();
    }

    function restoreSession(session) {
      taskEl.value = session.draft || '';
      logEl.className = '';
      logEl.textContent = session.lastLog || '等待任务...';
      const message = session.lastMessage;
      if (message && message.text) {
        agentMessageEl.dataset.kind = message.kind || 'info';
        agentMessageEl.textContent = message.text;
      } else {
        showAgentMessage(null);
      }
      if (session.screenshotDataUrl) {
        previewEl.src = session.screenshotDataUrl;
        previewEl.style.display = 'block';
      } else {
        previewEl.removeAttribute('src');
        previewEl.style.display = 'none';
      }
    }

    function renderSessions() {
      sessionListEl.innerHTML = '';
      sessions.forEach(session => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'session-item' + (session.id === currentSessionId ? ' active' : '');
        button.textContent = session.title || '新任务';
        button.title = session.title || '新任务';
        button.addEventListener('click', () => {
          persistCurrentSession();
          currentSessionId = session.id;
          saveSessions();
          restoreSession(session);
          renderSessions();
        });
        sessionListEl.appendChild(button);
      });
    }

    if (!sessions.length || !currentSessionId || !sessions.some(item => item.id === currentSessionId)) {
      createSession('新任务');
    }

    const savedApiConfig = readSavedApiConfig();
    if (savedApiConfig && savedApiConfig.apiKey) {
      apiKeyEl.value = savedApiConfig.apiKey;
      modelEl.value = savedApiConfig.model || modelEl.value;
      baseUrlEl.value = savedApiConfig.baseUrl || baseUrlEl.value;
    }
    syncBaseUrlForKey();
    refreshRememberState();

    function setBusy(busy) {
      runBtn.disabled = busy;
      dryRunBtn.disabled = busy;
      pauseBtn.disabled = false;
    }

    function writeLog(value, isError = false) {
      logEl.className = isError ? 'error' : '';
      logEl.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      persistCurrentSession();
    }

    function showAgentMessage(message, fallbackKind = 'info') {
      const payload = typeof message === 'string'
        ? { show: Boolean(message), text: message, kind: fallbackKind }
        : (message || {});
      agentMessageEl.dataset.kind = payload.kind || fallbackKind;
      agentMessageEl.textContent = payload.show && payload.text
        ? payload.text
        : '模型暂时没有需要告诉你的消息。';
      persistCurrentSession();
    }

    async function act(dryRun) {
      const task = taskEl.value.trim();
      if (!task) {
        writeLog('请先输入目标任务。', true);
        return;
      }
      const runVersion = ++clientRunVersion;
      const delay = Number(delayEl.value || 0);
      const apiKey = apiKeyEl.value.trim();
      const model = modelEl.value.trim();
      const baseUrl = baseUrlEl.value.trim();
      const session = currentSession();
      session.draft = task;
      session.title = titleFromText(task);
      session.updatedAt = Date.now();
      saveSessions();
      renderSessions();
      setBusy(true);
      writeLog(`已提交。${delay > 0 ? delay + ' 秒内请切回目标窗口...' : '正在截图...'}`);
      showAgentMessage('任务已提交，正在等待模型判断。');
      try {
        const response = await fetch('/act', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: currentSessionId, task, api_key: apiKey, model, base_url: baseUrl, delay, dry_run: dryRun })
        });
        const data = await response.json();
        if (runVersion !== clientRunVersion) {
          return;
        }
        if (!response.ok || !data.ok) {
          const errorMessage = data.error || '请求失败。';
          writeLog(errorMessage, true);
          showAgentMessage(errorMessage, 'error');
          return;
        }
        if (data.screenshot_data_url) {
          previewEl.src = data.screenshot_data_url;
          previewEl.style.display = 'block';
          persistCurrentSession();
        }
        showAgentMessage(data.user_message);
        writeLog(data);
      } catch (error) {
        if (runVersion !== clientRunVersion) {
          return;
        }
        const errorMessage = String(error);
        writeLog(errorMessage, true);
        showAgentMessage(errorMessage, 'error');
      } finally {
        if (runVersion === clientRunVersion) {
          setBusy(false);
        }
      }
    }

    pauseBtn.addEventListener('click', async () => {
      pauseBtn.disabled = true;
      showAgentMessage('正在暂停智能体；已开始的单次操作完成后将停止。', 'warning');
      try {
        const response = await fetch('/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'pause' })
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || '暂停失败。');
        }
        showAgentMessage(data.user_message, 'warning');
      } catch (error) {
        showAgentMessage(String(error), 'error');
      } finally {
        pauseBtn.disabled = false;
      }
    });

    addSessionBtn.addEventListener('click', async () => {
      persistCurrentSession();
      ++clientRunVersion;
      try {
        await fetch('/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'new_round' })
        });
      } catch (error) {
        console.warn(error);
      }
      const session = createSession('新任务');
      restoreSession(session);
      renderSessions();
      showAgentMessage('已添加新的智能体窗口；这里会从空白上下文开始。', 'info');
    });

    rememberApiBtn.addEventListener('click', () => {
      const apiKey = apiKeyEl.value.trim();
      const saved = readSavedApiConfig();
      if (saved && saved.apiKey === apiKey && apiKey) {
        localStorage.removeItem(API_CONFIG_STORAGE_KEY);
        refreshRememberState('已从当前浏览器清除保存的 API 配置。');
        return;
      }
      if (!apiKey) {
        refreshRememberState('请先输入 API Key。');
        return;
      }
      syncBaseUrlForKey();
      localStorage.setItem(API_CONFIG_STORAGE_KEY, JSON.stringify({
        apiKey,
        model: modelEl.value.trim(),
        baseUrl: baseUrlEl.value.trim()
      }));
      refreshRememberState('已记住 API Key、模型和接口地址。');
    });
    apiKeyEl.addEventListener('input', () => {
      syncBaseUrlForKey();
      refreshRememberState();
    });
    taskEl.addEventListener('input', persistCurrentSession);
    runBtn.addEventListener('click', () => act(false));
    dryRunBtn.addEventListener('click', () => act(true));
    restoreSession(currentSession());
    renderSessions();
  </script>
</body>
</html>
"""


def log_step(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class AgentController:
    def __init__(self):
        self.lock = threading.Lock()
        self.generation = 0
        self.active_run_id = None
        self.paused = False

    def start_run(self):
        with self.lock:
            self.generation += 1
            self.active_run_id = self.generation
            self.paused = False
            return self.active_run_id

    def pause_current(self):
        with self.lock:
            if self.active_run_id is None:
                return {"status": "idle", "run_id": None}
            self.paused = True
            return {"status": "pausing", "run_id": self.active_run_id}

    def replace_current(self):
        with self.lock:
            previous_run_id = self.active_run_id
            self.generation += 1
            self.active_run_id = None
            self.paused = False
            return {"status": "ready", "previous_run_id": previous_run_id}

    def stop_reason(self, run_id):
        if run_id is None:
            return None
        with self.lock:
            if self.active_run_id != run_id:
                return "replaced"
            if self.paused:
                return "paused"
            return None

    def finish_run(self, run_id):
        if run_id is None:
            return
        with self.lock:
            if self.active_run_id == run_id:
                self.active_run_id = None
                self.paused = False


AGENT_CONTROLLER = AgentController()
AGENT_SESSION_LOCK = threading.Lock()
AGENT_SESSIONS = {}
MAX_ACTIVE_EVENTS = 24
MAX_SESSION_DIRECTORY = 80
MAX_CONTEXT_EVENTS = 80


def make_empty_session(session_id):
    now = time.time()
    return {
        "session_id": session_id,
        "user_goal": "",
        "active_events": [],
        "task_directory": [],
        "context_store": {},
        "context_counter": 0,
        "active_retrieved_ids": [],
        "task_state": {"status": "new", "last_error": None, "round": 0},
        "current_focus": "",
        "created_at": now,
        "updated_at": now,
    }


def get_agent_session(session_id):
    normalized = str(session_id or "default").strip() or "default"
    with AGENT_SESSION_LOCK:
        session = AGENT_SESSIONS.get(normalized)
        if session is None:
            session = make_empty_session(normalized)
            AGENT_SESSIONS[normalized] = session
        return session


def trim_session_history(session_state):
    active_events = session_state.get("active_events") or []
    task_directory = session_state.get("task_directory") or []
    if len(active_events) > MAX_ACTIVE_EVENTS:
        del active_events[:-MAX_ACTIVE_EVENTS]
    if len(task_directory) > MAX_SESSION_DIRECTORY:
        del task_directory[:-MAX_SESSION_DIRECTORY]
    session_state["active_events"] = active_events
    session_state["task_directory"] = task_directory
    session_state["updated_at"] = time.time()


def json_clone(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except TypeError:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def normalize_context_id(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"\bC\d+\b", text, re.IGNORECASE)
    if match:
        return match.group(0).upper()
    return text.upper()


def text_clip(value, limit):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def next_context_id(session_state):
    session_state["context_counter"] = int(session_state.get("context_counter", 0)) + 1
    return f"C{session_state['context_counter']}"


def build_retrieved_step_details(session_state):
    store = session_state.setdefault("context_store", {})
    details = []
    kept_ids = []
    for raw_id in session_state.get("active_retrieved_ids", []):
        context_id = normalize_context_id(raw_id)
        item = store.get(context_id)
        if not item:
            continue
        kept_ids.append(context_id)
        details.append({
            "id": item.get("id", context_id),
            "title": item.get("title", ""),
            "status": item.get("status", ""),
            "brief": item.get("brief", ""),
            "round_start": item.get("round_start"),
            "round_end": item.get("round_end"),
            "events": item.get("events", []),
        })
    session_state["active_retrieved_ids"] = kept_ids
    return details


def add_retrieval_request(session_state, context_id):
    context_id = normalize_context_id(context_id)
    if not context_id:
        return False
    if context_id not in session_state.setdefault("context_store", {}):
        return False
    active_ids = session_state.setdefault("active_retrieved_ids", [])
    if context_id in active_ids:
        return False
    active_ids.append(context_id)
    return True


def apply_retrieval_updates(session_state, planner):
    ctx = planner.get("ctx") or {}
    release_ids = {
        normalize_context_id(item)
        for item in (ctx.get("release_retrieved") or [])
        if normalize_context_id(item)
    }
    if release_ids:
        session_state["active_retrieved_ids"] = [
            item for item in session_state.get("active_retrieved_ids", [])
            if normalize_context_id(item) not in release_ids
        ]

    added = []
    cr = planner.get("cr") or {}
    for request in cr.get("requests") or []:
        if not isinstance(request, dict):
            continue
        context_id = normalize_context_id(request.get("id", ""))
        if add_retrieval_request(session_state, context_id):
            added.append(context_id)
    return {
        "requested": added,
        "released": sorted(release_ids),
        "active": list(session_state.get("active_retrieved_ids", [])),
    }


def maybe_compress_active_context(session_state, planner, active_events, round_index):
    cu = planner.get("cu") or {}
    if not bool(cu.get("compress", False)):
        return None
    if not active_events:
        return None

    step = cu.get("step") or {}
    context_id = next_context_id(session_state)
    title = text_clip(
        step.get("title")
        or ((planner.get("op") or {}).get("i", ""))
        or planner.get("nf")
        or f"上下文片段 {context_id}",
        80,
    )
    brief = text_clip(step.get("brief") or planner.get("r") or "", 500)
    status = str(step.get("status") or "success").strip() or "success"
    events = json_clone(active_events[-MAX_CONTEXT_EVENTS:])
    rounds = [
        item.get("round")
        for item in events
        if isinstance(item, dict) and isinstance(item.get("round"), int)
    ]
    compressed = {
        "id": context_id,
        "title": title,
        "status": status,
        "brief": brief,
        "round_start": min(rounds) if rounds else None,
        "round_end": max(rounds) if rounds else round_index,
        "event_count": len(events),
        "events": events,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    session_state.setdefault("context_store", {})[context_id] = compressed
    session_state.setdefault("task_directory", []).append({
        "id": context_id,
        "title": title,
        "status": status,
        "brief": brief,
        "round_start": compressed["round_start"],
        "round_end": compressed["round_end"],
        "event_count": len(events),
    })
    active_events.clear()
    return {
        "compressed": True,
        "id": context_id,
        "title": title,
        "event_count": len(events),
    }


def apply_context_manager_updates(session_state, planner, active_events, round_index):
    retrieval = apply_retrieval_updates(session_state, planner)
    compression = maybe_compress_active_context(session_state, planner, active_events, round_index)
    trim_session_history(session_state)
    return {
        "retrieval": retrieval,
        "compression": compression,
        "active_event_count": len(active_events),
        "directory_count": len(session_state.get("task_directory", [])),
    }


def wait_with_agent_control(seconds, run_id):
    deadline = time.monotonic() + max(0, seconds)
    while True:
        stop_reason = AGENT_CONTROLLER.stop_reason(run_id)
        if stop_reason:
            return stop_reason
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.2, remaining))


def control_user_message(stop_reason):
    if stop_reason == "paused":
        return {
            "show": True,
            "text": "智能体已暂停。继续发送会沿用当前窗口；左侧“添加”会开启新窗口。",
            "kind": "warning",
            "round": None,
        }
    return {
        "show": True,
        "text": "旧轮次已结束，正在切换到新一轮。",
        "kind": "info",
        "round": None,
    }


def resolve_mimo_base_url(api_key, requested_base_url):
    base_url = (requested_base_url or MIMO_BASE_URL).strip().rstrip("/")
    normalized = base_url.lower()
    if api_key.lower().startswith("tp-") and normalized in {
        MIMO_PAYG_BASE_URL.lower(),
        MIMO_TOKEN_PLAN_BASE_URL.lower(),
    }:
        return MIMO_TOKEN_PLAN_BASE_URL
    if api_key.lower().startswith("sk-") and normalized in {
        MIMO_PAYG_BASE_URL.lower(),
        MIMO_TOKEN_PLAN_BASE_URL.lower(),
    }:
        return MIMO_PAYG_BASE_URL
    return base_url


def screenshot_png():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    command = ["/usr/sbin/screencapture", "-x", "-t", "png", str(ORIGINAL_SCREENSHOT_PATH)]
    log_step(f"截图开始：{' '.join(command)}")
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"macOS screencapture 截图失败：{details or '无错误详情'}")
    if not ORIGINAL_SCREENSHOT_PATH.exists() or ORIGINAL_SCREENSHOT_PATH.stat().st_size == 0:
        raise RuntimeError(f"macOS screencapture 没有生成截图：{ORIGINAL_SCREENSHOT_PATH}")

    image = Image.open(ORIGINAL_SCREENSHOT_PATH)
    image.load()
    if max(image.size) > MAX_SCREENSHOT_EDGE:
        image.thumbnail((MAX_SCREENSHOT_EDGE, MAX_SCREENSHOT_EDGE), Image.LANCZOS)
    image.save(MODEL_SCREENSHOT_PATH, format="PNG", optimize=True)
    png_bytes = MODEL_SCREENSHOT_PATH.read_bytes()
    log_step(
        "截图完成："
        f"原图 {ORIGINAL_SCREENSHOT_PATH}，模型图 {MODEL_SCREENSHOT_PATH}，"
        f"尺寸 {image.size[0]}x{image.size[1]}，大小 {len(png_bytes)} bytes"
    )
    return image, png_bytes, MODEL_SCREENSHOT_PATH


def call_mimo(
    task,
    image_base64,
    screenshot_size,
    screen_size,
    api_key,
    model,
    base_url,
    active_events=None,
    task_state=None,
    task_directory=None,
    current_focus="",
    round_index=1,
    user_goal=None,
    retrieved_step_details=None,
):
    if not api_key:
        raise RuntimeError("缺少 MiMo API Key。请在网页输入 API Key，或在终端设置 MIMO_API_KEY。")

    screenshot_width, screenshot_height = screenshot_size
    screen_width, screen_height = screen_size
    planner_input = {
        "user_goal": user_goal or task,
        "current_focus": current_focus or "",
        "task_state": task_state or {"status": "new", "last_error": None, "round": round_index},
        "task_directory": task_directory or [],
        "current_screen": {
            "observed": True,
            "source": "macos_screencapture",
            "screenshot_path": str(MODEL_SCREENSHOT_PATH),
            "width": screenshot_width,
            "height": screenshot_height,
            "pyautogui_width": screen_width,
            "pyautogui_height": screen_height,
            "elements": [],
            "note": "已附当前屏幕截图；本地暂未接入 icon_detect，因此没有编号候选框。若必须点击可见目标，请基于截图给出 pyautogui 动作坐标。",
        },
        "active_stage_raw_events": (
            active_events if active_events is not None else [{"role": "user", "content": task}]
        ),
        "retrieved_step_details": retrieved_step_details or [],
    }
    user_text = json.dumps(planner_input, ensure_ascii=False)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_base64}"
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
        "max_completion_tokens": 1200,
        "thinking": {"type": "disabled"},
    }
    log_step(f"请求 MiMo：base_url={base_url}, model={model}, timeout={MIMO_TIMEOUT}s")
    raw = ""
    request_url = f"{base_url.rstrip('/')}/chat/completions"
    for attempt in range(1, MIMO_NETWORK_RETRIES + 1):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(MIMO_TIMEOUT),
                trust_env=False,
                transport=httpx.HTTPTransport(retries=1),
            ) as client:
                response = client.post(
                    request_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Connection": "close",
                    },
                )
            if response.status_code >= 400:
                if response.status_code in (401, 403):
                    hint = "请确认 API Key 类型与接口地址匹配：sk- 使用按量接口，tp- 使用 Token Plan 接口。"
                elif response.status_code == 429:
                    hint = "请求过于频繁或额度不足，请稍后再试并检查账户额度。"
                elif response.status_code >= 500:
                    hint = "MiMo 服务暂时异常。"
                else:
                    hint = "请检查模型名、请求参数和接口地址。"
                raise RuntimeError(
                    f"MiMo API HTTP {response.status_code}: {response.text[:2000]} {hint}"
                )
            raw = response.text
            break
        except httpx.RequestError as error:
            if attempt >= MIMO_NETWORK_RETRIES:
                raise RuntimeError(
                    f"MiMo API 网络错误（已重试 {MIMO_NETWORK_RETRIES} 次）：{error}"
                ) from error
            retry_delay = min(2 ** attempt, 12)
            log_step(
                f"MiMo 网络连接失败：{error}；"
                f"{retry_delay}s 后重试（{attempt + 1}/{MIMO_NETWORK_RETRIES}）"
            )
            time.sleep(retry_delay)

    data = json.loads(raw)
    try:
        content = data["choices"][0]["message"]["content"].strip()
        log_step(f"MiMo 返回：{content!r}")
        return content, data
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"无法读取 MiMo 返回内容：{raw}") from error


def parse_decision(text, screenshot_size):
    normalized = text.strip().replace("，", ",")
    if normalized == "已经完成" or "已经完成" in normalized:
        return {"completed": True}
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", normalized)
    if not match:
        raise ValueError(f"模型没有按 x,y 或 已经完成 返回：{text!r}")
    x = float(match.group(1))
    y = float(match.group(2))
    width, height = screenshot_size
    if not (0 <= x <= width and 0 <= y <= height):
        raise ValueError(f"模型返回坐标超出截图范围：{x},{y}，截图尺寸 {width}x{height}")
    return {"completed": False, "screenshot_x": x, "screenshot_y": y}


def convert_to_pyautogui_coords(decision, screenshot_size, screen_size):
    screenshot_width, screenshot_height = screenshot_size
    screen_width, screen_height = screen_size
    scale_x = screen_width / screenshot_width
    scale_y = screen_height / screenshot_height
    return {
        "pyautogui_x": round(decision["screenshot_x"] * scale_x),
        "pyautogui_y": round(decision["screenshot_y"] * scale_y),
        "scale_x": scale_x,
        "scale_y": scale_y,
    }


def parse_planner_json(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"模型没有返回 JSON：{text!r}")
    return json.loads(cleaned[start : end + 1])


def run_zsh(command, cwd, timeout_ms):
    if not command:
        raise ValueError("zsh command 不能为空")
    timeout = max(1, min(300, int(timeout_ms or 60000) / 1000))
    log_step(f"执行 zsh：{command}")
    result = subprocess.run(
        ["/bin/zsh", "-lc", command],
        cwd=cwd or None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def run_pyautogui_action(action):
    action_type = str(action.get("type", "")).strip()
    if not action_type:
        return {"status": "skipped", "reason": "empty action"}

    if action_type == "click":
        pyautogui.click(
            int(action.get("x", 0)),
            int(action.get("y", 0)),
            clicks=int(action.get("clicks", 1)),
            button=action.get("button", "left"),
        )
    elif action_type == "double_click":
        pyautogui.doubleClick(int(action.get("x", 0)), int(action.get("y", 0)))
    elif action_type == "right_click":
        pyautogui.rightClick(int(action.get("x", 0)), int(action.get("y", 0)))
    elif action_type == "move":
        pyautogui.moveTo(int(action.get("x", 0)), int(action.get("y", 0)))
    elif action_type == "drag":
        pyautogui.moveTo(int(action.get("x", 0)), int(action.get("y", 0)))
        pyautogui.dragTo(
            int(action.get("to_x", action.get("x", 0))),
            int(action.get("to_y", action.get("y", 0))),
            duration=float(action.get("duration", 0.2)),
            button=action.get("button", "left"),
        )
    elif action_type == "scroll":
        pyautogui.scroll(int(action.get("clicks", action.get("amount", 0))))
    elif action_type == "type_text":
        pyautogui.write(str(action.get("text", "")), interval=float(action.get("interval", 0)))
    elif action_type == "press_key":
        pyautogui.press(str(action.get("key", "")))
    elif action_type == "hotkey":
        keys = action.get("keys")
        if not keys:
            keys = str(action.get("key", "")).split("+")
        pyautogui.hotkey(*[str(key).strip() for key in keys if str(key).strip()])
    elif action_type == "wait":
        time.sleep(float(action.get("seconds", action.get("duration", 1))))
    else:
        raise ValueError(f"不支持的 pyautogui 动作：{action_type}")
    return {"status": "success", "action": action}


def get_program_wait_seconds(op):
    action = op.get("action") or {}
    actions = op.get("actions") or []
    if str(action.get("type", "")).strip() == "wait":
        return max(0.2, min(15, float(action.get("seconds", action.get("duration", DEFAULT_WAIT_SECONDS)))))
    for item in actions:
        if str(item.get("type", "")).strip() == "wait":
            return max(0.2, min(15, float(item.get("seconds", item.get("duration", DEFAULT_WAIT_SECONDS)))))
    return None


def execute_planner_op(planner, dry_run):
    op = planner.get("op") or {}
    tool = str(op.get("tool", "none")).strip()
    wait_seconds = get_program_wait_seconds(op)
    if dry_run:
        return {"executed": False, "dry_run": True, "tool": tool, "wait_seconds": wait_seconds, "reason": "只生成计划模式不执行工具"}

    if tool in ("none", "") and wait_seconds is not None:
        return {"executed": False, "tool": "none", "program_wait": True, "wait_seconds": wait_seconds}

    if tool == "zsh":
        return {"executed": True, "tool": "zsh", "result": run_zsh(op.get("command", ""), op.get("cwd", ""), op.get("timeout_ms", 60000))}
    if tool == "vision":
        image, png_bytes, screenshot_path = screenshot_png()
        return {
            "executed": True,
            "tool": "vision",
            "screenshot_path": str(screenshot_path),
            "screenshot_size": {"width": image.size[0], "height": image.size[1]},
            "bytes": len(png_bytes),
        }
    if tool == "pyautogui":
        results = []
        actions = op.get("actions") or []
        if not actions and op.get("action"):
            actions = [op.get("action")]
        for action in actions:
            results.append(run_pyautogui_action(action))
        return {"executed": True, "tool": "pyautogui", "results": results}
    if tool in ("none", "ask_user", ""):
        return {"executed": False, "tool": tool, "ask": op.get("ask", {})}
    raise ValueError(f"不支持的工具：{tool}")


def get_screenshot_context():
    image, png_bytes, screenshot_path = screenshot_png()
    screenshot_size = image.size
    screen_size_obj = pyautogui.size()
    screen_size = (screen_size_obj.width, screen_size_obj.height)
    image_base64 = base64.b64encode(png_bytes).decode("ascii")
    return {
        "image": image,
        "png_bytes": png_bytes,
        "screenshot_path": screenshot_path,
        "screenshot_size": screenshot_size,
        "screen_size": screen_size,
        "image_base64": image_base64,
    }


def compact_event_payload(payload):
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= 4000:
        return payload
    return {"truncated": True, "brief": text[:4000]}


def extract_user_message(rounds):
    valid_kinds = {"info", "success", "warning", "question", "error"}
    for round_record in reversed(rounds):
        planner = round_record.get("planner") or {}
        explicit = planner.get("user_message") or {}
        text = str(explicit.get("text", "")).strip()
        if text and bool(explicit.get("show", True)):
            kind = str(explicit.get("kind", "info")).strip()
            return {
                "show": True,
                "text": text,
                "kind": kind if kind in valid_kinds else "info",
                "round": round_record.get("round"),
            }

        ask_text = str((((planner.get("op") or {}).get("ask") or {}).get("q", ""))).strip()
        if ask_text:
            return {
                "show": True,
                "text": ask_text,
                "kind": "question",
                "round": round_record.get("round"),
            }

        finish = planner.get("finish") or {}
        answer = str(finish.get("answer", "")).strip()
        if answer:
            return {
                "show": True,
                "text": answer,
                "kind": "success" if bool(finish.get("done", False)) else "info",
                "round": round_record.get("round"),
            }

        if round_record.get("completed"):
            summary = str(planner.get("r", "")).strip()
            if summary:
                return {
                    "show": True,
                    "text": summary,
                    "kind": "success",
                    "round": round_record.get("round"),
                }

    return {"show": False, "text": "", "kind": "info", "round": None}


def run_agent_loop(task, api_key, model, base_url, dry_run, run_id=None, session_state=None):
    if session_state is None:
        user_goal = task
        active_events = [{"role": "user", "content": task}]
        task_directory = []
        task_state = {"status": "new", "last_error": None, "round": 0}
        current_focus = ""
    else:
        if not session_state.get("user_goal"):
            session_state["user_goal"] = task
        user_goal = session_state.get("user_goal") or task
        active_events = session_state.setdefault("active_events", [])
        task_directory = session_state.setdefault("task_directory", [])
        session_state.setdefault("context_store", {})
        session_state.setdefault("active_retrieved_ids", [])
        task_state = session_state.get("task_state") or {"status": "new", "last_error": None, "round": 0}
        current_focus = session_state.get("current_focus", "")
        previous_status = str(task_state.get("status", ""))
        active_events.append({
            "role": "user",
            "content": task,
            "kind": "reply" if previous_status == "needs_user" else "message",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if previous_status == "needs_user":
            task_state = {
                "status": "user_replied",
                "last_error": None,
                "round": task_state.get("round", 0),
                "last_user_reply": task,
            }
    rounds = []
    final_context = None
    control_stop_reason = None
    limit_reached = False

    for round_index in range(1, MAX_AUTO_ROUNDS + 2):
        control_stop_reason = AGENT_CONTROLLER.stop_reason(run_id)
        if control_stop_reason:
            break

        confirmation_only = round_index > MAX_AUTO_ROUNDS
        if confirmation_only:
            log_step("达到操作轮次上限，执行最终截图确认")
        else:
            log_step(f"自动循环第 {round_index}/{MAX_AUTO_ROUNDS} 轮")
        context = get_screenshot_context()
        final_context = context
        screenshot_size = context["screenshot_size"]
        screen_size = context["screen_size"]
        retrieved_step_details = (
            build_retrieved_step_details(session_state)
            if session_state is not None
            else []
        )

        model_text, _raw = call_mimo(
            task,
            context["image_base64"],
            screenshot_size,
            screen_size,
            api_key,
            model,
            base_url,
            active_events=active_events,
            task_state=task_state,
            task_directory=task_directory,
            current_focus=current_focus,
            round_index=round_index,
            user_goal=user_goal,
            retrieved_step_details=retrieved_step_details,
        )
        planner = parse_planner_json(model_text)
        current_focus = planner.get("nf") or current_focus
        completed = bool((planner.get("finish") or {}).get("done", False))
        planned_tool = str(((planner.get("op") or {}).get("tool", "none"))).strip()
        control_stop_reason = AGENT_CONTROLLER.stop_reason(run_id)
        if control_stop_reason:
            execution = {
                "executed": False,
                "tool": "none",
                "control_stop": control_stop_reason,
                "reason": "智能体控制状态已改变，未执行本轮模型动作。",
            }
        elif confirmation_only and not completed and planned_tool != "ask_user":
            limit_reached = True
            execution = {
                "executed": False,
                "tool": planned_tool,
                "limit_reached": True,
                "reason": "最终确认轮只观察结果，不再执行新的操作。",
            }
        else:
            execution = execute_planner_op(planner, dry_run)

        round_record = {
            "round": round_index,
            "model_output": model_text,
            "planner": planner,
            "execution": execution,
            "completed": completed,
            "confirmation_only": confirmation_only,
            "retrieved_step_details": [
                {
                    "id": item.get("id", ""),
                    "title": item.get("title", ""),
                    "event_count": len(item.get("events", [])),
                }
                for item in retrieved_step_details
            ],
            "screenshot_path": str(context["screenshot_path"]),
            "screenshot_size": {"width": screenshot_size[0], "height": screenshot_size[1]},
        }
        rounds.append(round_record)

        active_events.append({"role": "model", "round": round_index, "content": compact_event_payload(planner)})
        active_events.append({"role": "program", "round": round_index, "content": compact_event_payload(execution)})
        if session_state is not None:
            round_record["context_update"] = apply_context_manager_updates(
                session_state,
                planner,
                active_events,
                round_index,
            )

        if control_stop_reason:
            break

        if dry_run or completed:
            break

        if limit_reached:
            break

        op = planner.get("op") or {}
        tool = execution.get("tool", op.get("tool", "none"))
        if tool == "ask_user":
            task_state = {"status": "needs_user", "last_error": None, "round": round_index}
            break

        if execution.get("program_wait"):
            wait_seconds = float(execution.get("wait_seconds", DEFAULT_WAIT_SECONDS))
            log_step(f"模型判断仍在加载，程序等待 {wait_seconds:.1f}s 后复查")
            control_stop_reason = wait_with_agent_control(wait_seconds, run_id)
            if control_stop_reason:
                break
            task_state = {
                "status": "waited_for_loading",
                "last_error": None,
                "round": round_index,
                "last_wait_seconds": wait_seconds,
            }
            continue

        if tool == "pyautogui" and execution.get("executed"):
            log_step(f"PyAutoGUI 执行后等待 {AFTER_PYAUTOGUI_OBSERVE_SECONDS:.1f}s 截图复查")
            control_stop_reason = wait_with_agent_control(
                AFTER_PYAUTOGUI_OBSERVE_SECONDS,
                run_id,
            )
            if control_stop_reason:
                break
            task_state = {
                "status": "after_pyautogui",
                "last_error": None,
                "round": round_index,
                "after_action_wait_ms": int(AFTER_PYAUTOGUI_OBSERVE_SECONDS * 1000),
            }
            continue

        if tool == "zsh" and execution.get("executed"):
            task_state = {
                "status": "after_zsh",
                "last_error": None,
                "round": round_index,
                "zsh_returncode": ((execution.get("result") or {}).get("returncode")),
            }
            continue

        if tool == "vision" and execution.get("executed"):
            task_state = {"status": "after_vision", "last_error": None, "round": round_index}
            continue

        context_update = round_record.get("context_update") or {}
        retrieval_requested = ((context_update.get("retrieval") or {}).get("requested") or [])
        compression = context_update.get("compression")
        if retrieval_requested:
            task_state = {
                "status": "after_context_retrieval_request",
                "last_error": None,
                "round": round_index,
                "requested_context_ids": retrieval_requested,
            }
            continue
        if compression:
            task_state = {
                "status": "after_context_compression",
                "last_error": None,
                "round": round_index,
                "compressed_context_id": compression.get("id"),
            }
            continue

        break

    if control_stop_reason:
        stopped_reason = control_stop_reason
    elif rounds and rounds[-1]["completed"]:
        stopped_reason = "completed"
    elif dry_run:
        stopped_reason = "dry_run"
    elif rounds and (rounds[-1].get("execution") or {}).get("tool") == "ask_user":
        stopped_reason = "needs_user"
    elif limit_reached:
        stopped_reason = "max_rounds_reached"
    else:
        stopped_reason = "max_rounds_or_no_action"

    if session_state is not None:
        if stopped_reason == "completed":
            task_state = {
                "status": "completed",
                "last_error": None,
                "round": rounds[-1]["round"] if rounds else task_state.get("round", 0),
            }
        elif stopped_reason == "needs_user":
            task_state = {
                "status": "needs_user",
                "last_error": None,
                "round": rounds[-1]["round"] if rounds else task_state.get("round", 0),
            }
        elif stopped_reason in ("paused", "replaced"):
            task_state = {
                "status": stopped_reason,
                "last_error": None,
                "round": rounds[-1]["round"] if rounds else task_state.get("round", 0),
            }
        session_state["user_goal"] = user_goal
        session_state["active_events"] = active_events
        session_state["task_directory"] = task_directory
        session_state["task_state"] = task_state
        session_state["current_focus"] = current_focus
        trim_session_history(session_state)

    return {
        "rounds": rounds,
        "final_context": final_context,
        "max_rounds": MAX_AUTO_ROUNDS,
        "stopped_reason": stopped_reason,
    }


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_control(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            action = str(payload.get("action", "")).strip()
            if action == "pause":
                result = AGENT_CONTROLLER.pause_current()
                if result["status"] == "idle":
                    message = {
                        "show": True,
                        "text": "当前没有正在运行的智能体。",
                        "kind": "info",
                    }
                else:
                    message = {
                        "show": True,
                        "text": "已收到暂停请求；当前单次操作结束后将停止。",
                        "kind": "warning",
                    }
            elif action == "new_round":
                result = AGENT_CONTROLLER.replace_current()
                message = {
                    "show": True,
                    "text": "旧运行已结束；左侧“添加”会创建新的智能体窗口。",
                    "kind": "info",
                }
            else:
                json_response(self, 400, {"ok": False, "error": "不支持的控制动作。"})
                return
            json_response(
                self,
                200,
                {"ok": True, "action": action, "result": result, "user_message": message},
            )
        except Exception as error:
            traceback.print_exc()
            json_response(self, 500, {"ok": False, "error": str(error)})

    def do_POST(self):
        if self.path == "/control":
            self.handle_control()
            return
        if self.path != "/act":
            self.send_error(404)
            return
        run_id = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            task = str(payload.get("task", "")).strip()
            api_key = str(payload.get("api_key", "")).strip() or MIMO_API_KEY
            model = str(payload.get("model", "")).strip() or MIMO_MODEL
            requested_base_url = str(payload.get("base_url", "")).strip() or MIMO_BASE_URL
            base_url = resolve_mimo_base_url(api_key, requested_base_url)
            if base_url != requested_base_url.rstrip("/"):
                log_step(f"已根据 API Key 类型自动切换接口地址：{base_url}")
            delay = max(0, min(30, float(payload.get("delay", 0))))
            dry_run = bool(payload.get("dry_run", False))
            session_id = str(payload.get("session_id", "default")).strip() or "default"
            session_state = get_agent_session(session_id)
            if not task:
                json_response(self, 400, {"ok": False, "error": "目标任务不能为空。"})
                return
            if not api_key:
                json_response(self, 400, {"ok": False, "error": "缺少 MiMo API Key。请在网页输入 API Key，或在终端设置 MIMO_API_KEY。"})
                return

            run_id = AGENT_CONTROLLER.start_run()
            if delay:
                stop_reason = wait_with_agent_control(delay, run_id)
                if stop_reason:
                    json_response(
                        self,
                        200,
                        {
                            "ok": True,
                            "task": task,
                            "dry_run": dry_run,
                            "model": model,
                            "base_url": base_url,
                            "session_id": session_id,
                            "run_id": run_id,
                            "rounds": [],
                            "stopped_reason": stop_reason,
                            "user_message": control_user_message(stop_reason),
                            "completed": False,
                            "clicked": False,
                        },
                    )
                    return

            log_step(f"收到任务：session={session_id!r}, task={task!r}, dry_run={dry_run}")
            loop_result = run_agent_loop(
                task,
                api_key,
                model,
                base_url,
                dry_run,
                run_id=run_id,
                session_state=session_state,
            )
            rounds = loop_result["rounds"]
            if not rounds:
                stop_reason = loop_result["stopped_reason"]
                if stop_reason in ("paused", "replaced"):
                    json_response(
                        self,
                        200,
                        {
                            "ok": True,
                            "task": task,
                            "dry_run": dry_run,
                            "model": model,
                            "base_url": base_url,
                            "session_id": session_id,
                            "run_id": run_id,
                            "rounds": [],
                            "stopped_reason": stop_reason,
                            "user_message": control_user_message(stop_reason),
                            "completed": False,
                            "clicked": False,
                        },
                    )
                    return
                raise RuntimeError("没有生成任何规划轮次")
            last_round = rounds[-1]
            final_context = loop_result["final_context"]
            screenshot_size = final_context["screenshot_size"]
            screen_size = final_context["screen_size"]
            image_base64 = final_context["image_base64"]
            screenshot_path = final_context["screenshot_path"]

            response = {
                "ok": True,
                "task": task,
                "dry_run": dry_run,
                "model": model,
                "base_url": base_url,
                "session_id": session_id,
                "user_goal": session_state.get("user_goal", task),
                "run_id": run_id,
                "model_output": last_round["model_output"],
                "planner": last_round["planner"],
                "execution": last_round["execution"],
                "rounds": rounds,
                "stopped_reason": loop_result["stopped_reason"],
                "max_rounds": loop_result["max_rounds"],
                "context": {
                    "active_event_count": len(session_state.get("active_events", [])),
                    "directory": session_state.get("task_directory", []),
                    "active_retrieved_ids": session_state.get("active_retrieved_ids", []),
                    "store_count": len(session_state.get("context_store", {})),
                },
                "user_message": (
                    control_user_message(loop_result["stopped_reason"])
                    if loop_result["stopped_reason"] in ("paused", "replaced")
                    else {
                        "show": True,
                        "text": (
                            f"已完成最终截图确认，但任务在 {MAX_AUTO_ROUNDS} 个操作轮次内仍未完成。"
                            "你可以继续发送补充信息，或点左侧“添加”开新窗口。"
                        ),
                        "kind": "warning",
                        "round": last_round["round"],
                    }
                    if loop_result["stopped_reason"] == "max_rounds_reached"
                    else extract_user_message(rounds)
                ),
                "screenshot_path": str(screenshot_path),
                "original_screenshot_path": str(ORIGINAL_SCREENSHOT_PATH),
                "screenshot_size": {"width": screenshot_size[0], "height": screenshot_size[1]},
                "pyautogui_screen_size": {"width": screen_size[0], "height": screen_size[1]},
                "screenshot_data_url": f"data:image/png;base64,{image_base64}",
            }
            response["completed"] = bool(last_round["completed"])
            response["clicked"] = any(
                item.get("execution", {}).get("tool") == "pyautogui" and item.get("execution", {}).get("executed", False)
                for item in rounds
            )
            json_response(self, 200, response)
        except Exception as error:
            traceback.print_exc()
            json_response(self, 500, {"ok": False, "error": str(error)})
        finally:
            AGENT_CONTROLLER.finish_run(run_id)


def main():
    print(f"PyAutoGUI 点击智能体已启动：http://{HOST}:{PORT}")
    print(f"模型：{MIMO_MODEL}")
    print("按 Ctrl+C 停止。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
