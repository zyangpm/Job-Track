# 秋招投递追踪表

自动读取邮箱（Gmail / QQ / 163 / 126 / Outlook 等）里的招聘邮件 → AI 提取公司/岗位/状态 → 生成日历看板，还能自动巡检招聘官网更新投递状态。

**全程只读邮件**，不发送、不删除、不修改任何邮件。所有数据只存在你本地。

---

## 和 JobHuntBot 配合使用

本 skill 的定位是**秋招投递的记录与追踪**：投递完之后，自动扫邮箱里的招聘邮件，AI 提取公司/岗位/笔试面试状态，生成日历看板，帮你盯住每一家的进度。

找岗位、投递这步，配合 **[JobHuntBot](https://github.com/DanielPan12/JobHuntBot)** 使用：

| | JobHuntBot | 本 skill（Job-Track） |
|---|---|---|
| 负责 | 找岗位、筛岗、执行投递 | 投递后的记录与追踪 |
| 产出 | 岗位池 / 投递记录（`job_pool.csv` / `application_log.csv`） | 邮箱通知 → 追踪表 → 日历看板 |
| 时机 | 投之前 | 投之后 |

**组合用法**：**JobHuntBot** 负责找和你简历匹配的岗位（网申表用**牛客网申助手**，上面有现成的网申列表）；本 skill 负责投递后的记录与追踪。一个管"投"、一个管"追"，全程数据只存本地。

### 投递后怎么记录（用「AI 添加」）

1. 在官网投递成功后，把**招聘网址复制下来**（岗位不用复制，AI 会自动识别；内推码可填可不填）
2. 打开本 skill 看板，点 **「AI 添加」**，把网址粘贴进去
3. AI 自动识别公司、岗位，加入追踪表
4. 之后点「扫描邮箱」，笔试/面试/测评通知自动进表 → 日历看板盯进度

---

## 一、你需要准备什么

### 1. Python 3.10 或更高版本

下载地址：[python.org/downloads](https://www.python.org/downloads/)

安装时**务必勾选 "Add Python to PATH"**。

### 2. 一个常用邮箱

用来收招聘邮件的邮箱。**Gmail、QQ 邮箱、163、126、Outlook 都支持**（页面「设置」里选中提供商后，收信服务器会自动填好，不用自己查）。

### 3. 一个 AI 模型的 API Key（识别公司/岗位/状态用）

页面「设置」里选一个提供商，**只需填 API Key**，接口地址和模型名会自动填好：

| 提供商 | 怎么拿 Key | 默认模型 |
|---|---|---|
| **DeepSeek**（推荐，便宜够用） | [platform.deepseek.com](https://platform.deepseek.com) → API Keys → 创建 | `deepseek-chat` |
| OpenAI（ChatGPT） | [platform.openai.com](https://platform.openai.com/api-keys) | `gpt-4o-mini` |
| Kimi（月之暗面） | [platform.moonshot.cn](https://platform.moonshot.cn) | `moonshot-v1-32k` |
| 通义千问（阿里） | [bailian.console.aliyun.com](https://bailian.console.aliyun.com) → API-KEY | `qwen-plus` |
| 智谱 GLM | [open.bigmodel.cn](https://open.bigmodel.cn) | `glm-4-flash` |
| 火山方舟 / 豆包 | [console.volcengine.com/ark](https://console.volcengine.com/ark) → API Key 管理 | `doubao-1.5-pro-32k` |
| 硅基流动 SiliconFlow | [cloud.siliconflow.cn](https://cloud.siliconflow.cn)（很多模型有免费额度） | `Qwen/Qwen2.5-72B-Instruct` |
| Ollama（本地，免费） | 本机装 Ollama 即可，**不用 Key** | `llama3.1:8b` |

还有「其他」选项，可填任意 OpenAI 兼容接口的地址和模型名。

---

## 二、安装（一句话）

**对你的豆包（或任意支持装技能的 agent）说一句话：**

> 安装 GitHub 上的 zyangpm/Job-Track 项目

它就会自动完成全部安装：下载项目 → 装成 Skill → 安装依赖 → 启动服务 → 打开看板。装好之后你再说"帮我扫邮箱""查投递状态"，它就直接用这个 Skill 干活。

**所有用户都是这一句话，不需要碰命令行。**

> 不用 agent 的话，手动安装只需要三步：
> 1. 仓库页点 **Code → Download ZIP** 解压
> 2. 装依赖：打开命令行进入文件夹，运行 `pip install -r requirements.txt` 和 `playwright install chromium`
> 3. 双击 `启动追踪表.bat`（首次使用先点右上角「设置」配邮箱授权码和模型 API Key，见下节）
> 依赖：Python 3.10+（见上文准备清单）。

---

## 三、配置（一次性）

### 第 1 步：生成邮箱的"应用专用密码 / 授权码"

邮箱不允许直接用登录密码连接第三方程序，需要单独生成一串 16 位专用密码。**这一步在哪生成，页面「设置」里点开教程就能看到**，这里先列个速查：

| 邮箱 | 生成位置 | 叫什么 |
|---|---|---|
| Gmail | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)（需先开两步验证） | 应用专用密码（16 位） |
| QQ 邮箱 | mail.qq.com → 设置 → 账户 → 开启 IMAP/SMTP | 授权码（16 位） |
| 163 / 126 | mail.163.com → 设置 → POP3/SMTP/IMAP → 开启 IMAP | 客户端授权密码 |
| Outlook | account.microsoft.com/security → 高级安全选项 → 应用密码 | 应用密码 |

> Gmail 注意：除了生成应用专用密码，还要在 Gmail 设置 → 转发和 POP/IMAP 里**开启 IMAP**。

### 第 2 步：在页面里配置（不用手动改文件）

1. 双击 `启动追踪表.bat`，浏览器打开看板
2. 点页面右上角 **「设置」**
3. 填两项，然后点「保存设置」，立刻生效：
   - **邮箱配置**：选你的邮箱提供商 → 填邮箱地址 + 第 1 步生成的专用密码（收信服务器自动填好）
   - **AI 模型配置**：选提供商 → 选模型名 → 填 API Key（接口地址自动填好；Ollama 不用填 Key）

> 配置只存在你这台电脑的 `.env` 文件里（页面改、程序自动存），**不会上传到 GitHub**（仓库的 .gitignore 已自动忽略它）。

---

## 四、启动

**双击 `启动追踪表.bat`**。

脚本会自动找到 Python、启动服务，并打开浏览器 `http://localhost:8788`（8788 被占用会自动往上换端口，窗口里会显示实际地址；8788 是分发版独立端口，与原版 job_track 用的 8768 彻底隔离）。首次使用前记得先在页面「设置」里配置邮箱和模型。

以后每次用都是双击这个 bat。

---

## 五、功能说明

| 按钮 | 作用 |
|---|---|
| **扫描邮箱** | 读取最近 30 天邮件，自动识别招聘邮件，把公司/岗位/状态加进表 |
| **AI 添加** | 粘贴一个投递网址（比如华为招聘页），自动识别公司和岗位并添加 |
| **官网巡检** | 自动打开浏览器，去你配好的招聘官网查投递状态（笔试/面试通知） |
| **登录信息** | 在这里添加要巡检的公司：公司名、登录网址、账号密码 |

### 关于官网巡检（可选）

这个功能不是必须的。如果你想让它自动查官网：

1. 点页面上的 **「登录信息」** 按钮
2. 添加公司：填公司名、招聘官网地址、你的账号密码
3. 它会在你点"官网巡检"时自动打开浏览器，帮你查投递进度

**注意**：每 4 小时最多巡检一次（保护账号不被风控）。

---

## 六、常见问题

**Q：扫描邮箱没反应？**
→ 检查页面「设置」里邮箱和专用密码/授权码有没有填对（16 位、无空格），保存后再点扫描。如果用的是 QQ/163 等，确认网页版设置里已开启 IMAP 服务。

**Q：AI 添加很慢？**
→ 第一次会稍慢（要打开浏览器看页面），之后会缓存，会快很多。

**Q：数据存在哪？**
→ 全在本文件夹里，没有上传到任何服务器。删掉文件夹就全没了。

**Q：换电脑怎么办？**
→ 整个文件夹拷走，`.env` 里的配置跟着走就行。
