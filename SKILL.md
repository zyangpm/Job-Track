---
name: qiuzhao-tracker
description: 秋招投递追踪工具。自动扫描邮箱（Gmail/QQ/163/126/Outlook）招聘邮件，AI 识别公司/岗位/笔试面试状态，生成可视化追踪看板。当用户提到"秋招投递""招聘邮件""校招状态追踪""笔试面试提醒""Gmail 招聘邮件整理""投递台账""job tracker""campus recruitment email"时使用。
compatibility: Python 3.10+，需 playwright（chromium）
---

# 秋招投递追踪（Job Track）

自动读取邮箱招聘邮件 → AI 提取公司/岗位/状态 → 生成日历看板。全程只读邮件，不发送不删除；所有数据只存在用户本地。

> 安装：用户只需说「安装 GitHub 上的 zyangpm/Job-Track 项目」，由 agent 完成下载、装依赖、启动看板（详见 `README.md`）。

## 适用场景

- 用户想整理/追踪秋招投递记录、扫描招聘邮件、查笔试面试状态、生成投递看板/日历
- 用户提到"帮我扫邮箱""投递状态""校招进度""招聘邮件整理"
- **与 [JobHuntBot](https://github.com/DanielPan12/JobHuntBot) 配合**：JobHuntBot 负责找岗位/执行投递，本 skill 负责投递后的记录与追踪（扫邮箱 → 状态 → 日历看板）。用户提到"配合 JobHuntBot 使用""投递后追踪"时，明确分工：一个管投、一个管追。

## 依赖与兼容

- Python 3.10+（bat 会自动找 `python` / `py -3`）
- 依赖：`pip install -r requirements.txt`；浏览器自动化需 `playwright install chromium`
- 服务端口：**8788**（被占用自动往上找 8788-8799），与原版 job_track 的 8768 彻底隔离

## Agent 执行流程

1. **确认配置**：读取 `秋招投递追踪表.html` 内嵌数据（看 version/records 是否已初始化）；检查本机 `.env`（由页面「设置」自动写入）是否含邮箱和模型配置。未配置时引导用户在页面右上角「设置」填写：
   - 邮箱：选提供商（Gmail/QQ/163/126/Outlook），填邮箱地址 + 16 位应用专用密码/授权码（生成教程在设置弹窗内，Gmail 需先开两步验证并开启 IMAP）
   - AI 模型：选提供商（DeepSeek/OpenAI/Kimi/通义千问/智谱/火山豆包/硅基流动/Ollama），只需填 API Key，模型名和接口地址自动填好
2. **启动服务**：双击 `启动追踪表.bat`，或运行 `python server.py`（bat 会自动挑空闲端口并打开浏览器）
3. **打开看板**：浏览器访问 `http://localhost:8788`（bat 会自动打开）
4. **执行用户请求**：
   - 扫描邮箱 → 读最近 30 天邮件，AI 提取公司/岗位/状态写入看板
   - AI 添加 → 粘贴招聘 URL，自动识别公司岗位
   - 官网巡检 → 自动打开浏览器查官网投递状态（4 小时间隔保护账号）
5. **验证**：扫描/添加后刷新页面确认记录出现；状态修改后确认 `秋招投递追踪表.html` 内嵌数据已更新（服务端保存链路：页面 → POST /api/save → 写回 html）

## 故障处理

| 现象 | 处理 |
|---|---|
| bat 双击没反应 | 检查 Python 是否安装并加入 PATH；确认 `pip install -r requirements.txt` 和 `playwright install chromium` 已执行 |
| 扫描邮箱没反应 | 检查「设置」里邮箱/授权码是否填对（16 位无空格）、IMAP 是否开启 |
| 端口被占用 | bat 会自动换端口（8788-8799），看黑窗口显示的实际地址 |
| 删除的公司又出现 | 服务端 `deleted_ids.json` 墓碑机制会自动过滤，任何扫描/保存都不会复活；若复现检查该文件存在 |

## 数据与隐私

- 所有数据（记录、配置、登录信息、巡检状态）只存用户本机文件夹
- `.env`（含邮箱密码/API Key）已被 `.gitignore` 排除，**永不进入 GitHub**
- 邮箱只读（IMAP），不发送不删除任何邮件
