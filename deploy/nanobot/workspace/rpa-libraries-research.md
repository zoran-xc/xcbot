# RPA / 浏览器自动化库调研报告

> 调研目标：简单易用、可通过 API 远程调用、模拟用户操作的 RPA 库

---

## 📊 总体分类

| 类型 | 特点 | 代表项目 |
|------|------|----------|
| **AI Agent RPA** | LLM 驱动，自动理解页面 | browser-use, Skyvern |
| **传统 Web 自动化** | 需编写脚本，精确控制 | Playwright, Selenium |
| **有 REST API 的服务** | 独立运行，HTTP 调用 | Skyvern, browser-use (需二次开发) |
| **爬虫框架** | 专注数据提取 | Crawlee, SeleniumBase |

---

## ⭐ 推荐列表

### 🥇 Tier 1: 最推荐（有 REST API 或易封装）

| 项目 | ⭐ Stars | 语言 | API 能力 | 评价 |
|------|----------|------|----------|------|
| **browser-use** | 79,230 | Python | ⭐⭐⭐ 可封装 | 最流行的 AI 浏览器自动化，基于 Playwright。只需几行代码即可让 AI 操作网页。官方有 `Browser` 类，可轻松封装 HTTP API |
| **Skyvern** | 20,579 | Python | ⭐⭐⭐ 内置 REST API | 专为 AI 自动化设计，有完整的 HTTP API，可直接调用。支持浏览器控制、工作流编排 |
| **Playwright** (微软) | 83,226 | TS/Python | ⭐⭐ 需开启远程调试 | 官方支持远程调试模式（CDP），可封装成 API 服务。有 Python/Node/Go/Rust 多语言 SDK |

### 🥈 Tier 2: 也不错

| 项目 | ⭐ Stars | 语言 | API 能力 | 评价 |
|------|----------|------|----------|------|
| **crawlee-python** | 8,150 | Python | ⭐⭐ 需封装 | Apify 出品的爬虫框架，功能强大，支持 Playwright/httpx，有代理轮换 |
| **SeleniumBase** | 12,426 | Python | ⭐⭐ 需封装 | 老牌 Web 自动化，支持绕过机器人检测，文档丰富 |
| **puppeteer** (Node) | 86,000+ | Node.js | ⭐⭐ 需封装 | Google 出品，Chrome 官方支持，生态成熟 |

---

## 🔥 详细分析

### 1. browser-use ⭐ 79,230

**定位**：让 AI Agent 操作浏览器

**特点**：
- 基于 Playwright
- 只需描述任务，AI 自动完成点击、输入、滚动等操作
- 支持视觉模型（GPT-4V 等）

**简单示例**：
```python
from browser_use import Agent
from langchain_openai import ChatOpenAI

agent = Agent(
    task="打开小红书，搜索 'Vibe Coding'，点击第一个笔记",
    llm=ChatOpenAI(model="gpt-4"),
)
agent.run()
```

**封装 API 思路**：
```python
# 启动服务
from flask import Flask, request, jsonify
from browser_use import Agent

app = Flask(__name__)

@app.route('/execute', methods=['POST'])
def execute_task():
    task = request.json['task']
    agent = Agent(task=task)
    result = agent.run()
    return jsonify({"result": result})
```

**评价**：⭐⭐⭐⭐⭐ 最推荐，代码量最少，AI 理解力强

---

### 2. Skyvern ⭐ 20,579

**定位**：AI 自动化浏览器工作流

**特点**：
- 专为自动化设计，有完整 REST API
- 支持工作流编排
- 可视化任务配置

**API 示例**：
```bash
# 启动任务
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://xiaohongshu.com", "action": "click", "element": ".search-input"}'
```

**评价**：⭐⭐⭐⭐ 有内置 API，适合直接集成

---

### 3. Playwright ⭐ 83,226

**定位**：微软官方浏览器自动化框架

**特点**：
- 支持 Chromium/Firefox/WebKit
- 跨语言（Python/Node/Go/Rust）
- 稳定可靠

**远程调试模式**：
```bash
# 启动浏览器（带远程调试）
playwright codegen --browser=chromium
# 或
chromium --remote-debugging-port=9222
```

**Python 示例**：
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("https://xiaohongshu.com")
    page.click(".search-input")
    browser.close()
```

**封装 API 思路**：
```python
from flask import Flask, request
from playwright.sync_api import sync_playwright

app = Flask(__name__)

@app.route('/browser', methods=['POST'])
def browser_action():
    action = request.json
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # 执行 action['type'], action['selector']...
        result = page.evaluate(f"document.querySelector('{action['selector']}').{action['action']}()")
        browser.close()
        return result
```

**评价**：⭐⭐⭐⭐ 官方支持，稳定可靠，但需要自己封装 API

---

## 🛠 推荐方案

### 方案 A：快速上手（推荐）

使用 **browser-use**，几行代码即可让 AI 操作浏览器：
```python
from browser_use import Agent
# 启动服务封装成 API
```

### 方案 B：有 REST API

使用 **Skyvern**，开箱即用：
```bash
pip install skyvern
skyvern run
# 访问 http://localhost:8000/docs 查看 API
```

### 方案 C：自己封装

使用 **Playwright** + Flask/FastAPI：
```python
from fastapi import FastAPI
from playwright.sync_api import sync_playwright
# 封装成 REST API
```

---

## 📦 安装难度

| 项目 | 安装难度 | 依赖 |
|------|----------|------|
| browser-use | ⭐ 简单 | `pip install browser-use` |
| Skyvern | ⭐⭐ 中等 | Docker 或 pip |
| Playwright | ⭐⭐ 中等 | `pip install playwright` + `playwright install` |
| SeleniumBase | ⭐ 简单 | `pip install seleniumbase` |

---

## 🎯 结论

**最推荐**：**browser-use**
- 代码量最少
- AI 理解力强
- 基于 Playwright，稳定可靠
- 容易封装成 HTTP API

**次推荐**：**Skyvern**
- 有内置 REST API
- 专为自动化设计

**备选**：**Playwright**
- 官方支持
- 灵活可控
- 需要自己封装 API

---

*调研时间: 2026-03-01*