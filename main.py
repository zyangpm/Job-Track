# -*- coding: utf-8 -*-
"""
job_track 主程序
================
两种自动模式（都只读拉取 Gmail，全程不需要手动录入投递信息）：

  模式A：AUTO_USE_DEEPSEEK = True
      拉取邮件后自动调用 DeepSeek API 完成结构化提取，全自动。

  模式B：AUTO_USE_DEEPSEEK = False （不消耗 DeepSeek 点数）
      脚本把全部求职邮件打包成一段现成 prompt，自动复制到剪贴板，
      并保存到 待解析_prompt.txt。你把它整段发给 Trae，把 Trae 返回的
      JSON 粘贴回控制台（或存成 trae_result.json），脚本自动写入台账。
"""
import imaplib
import email
import json
import os
import re
import sys
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# ================== 模式开关 ==================
AUTO_USE_DEEPSEEK = True   # True=模式A(DeepSeek自动解析) / False=模式B(Trae解析)

# ================== 常量 ==================
DAYS = 30                               # 抓最近30天（R6起邮件本地缓存+增量拉取，30天也很快）
IMAP_HOST = os.getenv("GMAIL_IMAP_HOST", "").strip() or "imap.gmail.com"
CSV_FILE = "job_tracker.csv"
STATUS_FILE = "status_list.json"
PROMPT_FILE = "待解析_prompt.txt"        # 模式B：自动生成的 prompt 文件
RESULT_FILE = "trae_result.json"        # 模式B：可把 Trae 结果存成此文件

KEYWORDS = [
    "网申", "投递", "笔试", "面试", "测评", "简历筛选", "初筛", "复筛", "终面",
    "oc", "offer", "人才库", "待沟通", "应聘", "校招", "校园招聘", "秋招", "春招",
    "录用", "感谢信", "候选人", "application", "interview", "reject",
    "简历", "简历评估", "简历初评", "邀约", "邀约面试", "线上面试", "线下面试",
    "群面", "单面", "二面", "三面", "hr面", "专业面", "技术面", "资格审核",
    "资格初审", "资格复审", "人才储备", "进入候选", "备选池", "等待通知",
    "结果通知", "录用审批", "发放offer", "签约", "三方", "意向书", "意向offer",
    "待审批", "简历驳回", "不予通过", "未通过", "淘汰", "感谢投递", "简历接收",
    "申请已收到", "职位申请", "岗位申请", "招聘", "应届生", "校园宣讲", "宣讲会",
    "open day", "assessment", "resume", "cv", "interview invitation",
    "shortlist", "screen", "hire", "recruit", "talent", "job apply", "rejection",
    "岗位已关闭", "职位关闭", "简历已读", "hr已查看", "hr查看", "沟通邀请",
    "面试邀请", "面试安排", "面试调整", "面试改期", "面试取消", "笔试通知",
    "测评邀请", "在线测评", "性格测评", "能力测评", "胜任力评估", "笔试结果",
    "面试结果", "流程终止", "流程结束", "进入下一环节", "进入下一轮",
    "进入终选", "终选", "补录", "补招", "调剂", "岗位调剂", "简历入库",
    "简历归档", "申请状态", "应聘状态", "招聘进度", "职位进度", "意向收集",
    "信息确认", "资料提交", "材料提交", "背景调查", "背调", "入职", "入职指引",
    "报到", "欢迎加入", "welcome", "short-listed", "screening",
    "assessment centre", "background check", "onboard", "offer letter",
    "job opening", "vacancy", "很遗憾", "未能入选", "不予录用", "暂不匹配",
    "暂不合适", "本次招聘结束", "双选会", "空中宣讲", "空宣", "实习生",
    "实习招聘", "jd", "job description", "apply", "application status",
    "interview schedule",
]
# 弱营销词：只在【标题】里判，且标题同时有招聘语境（校招/宣讲/简历…）时不拦——
# 正文里这些词太常见（空宣直播、福利群、"爆款产品岗"等），全文判会误杀真求职邮件。
AD_SUBJECT_WORDS = [
    "促销", "优惠券", "打折", "限时抢", "广告", "推广", "会员", "满减", "秒杀",
    "新品", "预售", "福利", "活动", "直播", "打卡", "礼包", "赠品", "体验课",
    "优惠码", "专场特惠", "团购", "限时购", "商城", "下单", "购物", "折扣",
    "抽奖", "限时活动", "限时福利", "专属福利", "限时折扣", "闪购", "爆款",
    "好物", "导购", "种草", "专栏", "付费专栏", "直播预告", "社群",
    "沙龙活动", "线下活动", "报名活动", "promotion", "sale", "discount",
    "coupon", "campaign",
]
# 强广告/培训课：标题正文任何位置出现都拦（正经招聘邮件不会有这些）
AD_BODY_WORDS = [
    "订阅确认", "newsletter", "webinar", "advertisement", "spam", "commercial",
    "marketing", "subscribe now",
    "简历修改", "简历优化", "面试辅导", "笔试辅导",
    "求职课", "求职训练营", "面试技巧", "求职内推课", "实训", "就业班",
]
# 标题里的招聘语境词：与弱营销词共现时豁免（如"宝宝巴士校招空宣直播"）
_RECRUIT_SUBJ_RE = re.compile(
    r"招聘|校招|秋招|春招|宣讲|空宣|职位|岗位|简历|投递|应聘|申请|"
    r"面试|笔试|测评|录用|offer|人才|recruit|campus|applic|interview", re.I)

# 标题命中这些，百分百不是求职招聘邮件（账号/安全/产品营销类），直接丢弃。
# 典型：GitHub 验证码、Google 账号授权、Mistral 欢迎注册、设备设置引导。
NON_JOB_SUBJECT = re.compile(
    r"verification code|sudo (email|authentication)|验证码|校验码|"
    r"oauth application|two[- ]factor|2[- ]step verification|\b2fa\b|recovery codes?|"
    r"review this sign[ -]?in|new sign[ -]?in|signed in to|sign[ -]?in attempt|"
    r"has been added to your account|shared some google account data|"
    r"finish(?:ed)? setting up|almost done setting|set up google|"
    r"please download your|password (?:was|has been) (?:reset|changed)|"
    r"security alert|account data|api[ -]?key|newsletter|unsubscribe|"
    r"product update|月度资讯|weekly digest|"
    # 平台账号操作：换绑/绑定验证，不含投递状态
    r"绑定验证|更换邮箱|邮箱绑定|"
    # 纯产品营销/促销（Spotify/OpenAI/Together/Gemini 等）
    r"premium for|months? of|ad[- ]free|s\$\s?\d|playlist|fit you|accelerates inference|"
    r"access to daily help|隐私政策更新|申请快要完成|是不是忘了",
    re.I)
# 纯英文"欢迎使用 XX 产品"（无中文、无招聘语境）不是招聘欢迎信
_WELCOME_EN = re.compile(r"^welcome to\b", re.I)
_RECRUIT_EN = re.compile(
    r"applic|interview|campus|graduat|position|job offer|recruit|"
    r"assessment|笔试|面试|测评|招聘|校招|秋招|春招", re.I)


def _is_non_job(subject, body):
    subj = subject or ""
    if NON_JOB_SUBJECT.search(subj):
        return True
    s_head = subj.strip()
    if _WELCOME_EN.search(s_head) and not re.search(r"[一-龥]", s_head) \
            and not _RECRUIT_EN.search(subj + " " + (body or "")[:300]):
        return True
    return False

COLUMNS = ["投递日期", "公司全称", "岗位名称", "招聘阶段", "面试时间", "截止时间",
           "人工复核", "新状态待人工归类", "最后更新时间"]

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
GMAIL_EMAIL = os.getenv("GMAIL_EMAIL")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
# 模型配置：MODEL_API_KEY 优先，兼容旧的 DEEPSEEK_API_KEY；
# base_url / model 可在页面「设置」里换成任意 OpenAI 兼容接口（不填默认 DeepSeek）
DEEPSEEK_API_KEY = os.getenv("MODEL_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").strip() or "https://api.deepseek.com"
MODEL_NAME = os.getenv("MODEL_NAME", "").strip() or "deepseek-chat"


# ================== 邮件拉取（只读） ==================
def decode_str(s):
    """解码邮件头部字段（标题等）"""
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            for enc in (charset, "utf-8", "gbk", "latin-1"):
                try:
                    out.append(text.decode(enc or "utf-8", errors="replace"))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
        else:
            out.append(text)
    return "".join(out)


def extract_body(msg):
    """优先取 text/plain 正文，其次把 html 去标签"""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            plain = payload.decode(charset, errors="replace")
    # 先删 <script>/<style> 整块再去标签，否则 CSS/JS 文本会污染正文（阿里邮件就是）
    html_clean = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    body = plain or re.sub(r"<[^>]+>", " ", html_clean)
    return re.sub(r"\s+", " ", body).strip()


MAIL_CACHE_FILE = BASE_DIR / "mail_cache.json"   # R6：邮件正文增量缓存（第二次扫描秒级）


def _load_mail_cache():
    try:
        d = json.loads(MAIL_CACHE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_mail_cache(cache):
    try:
        tmp = MAIL_CACHE_FILE.with_name(MAIL_CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, MAIL_CACHE_FILE)
    except Exception as e:
        print(f"      （邮件缓存写入失败，不影响本次：{e}）")


def _header_is_ad(subject):
    """标题一眼就是广告（命中广告词且无求职词）→ 正文都不用拉。
    英文 offer 单独出现不算求职语境（营销"优惠"常见），与 filter_job_emails 规则一致。"""
    t = (subject or "").lower()
    if not any(a.lower() in t for a in AD_SUBJECT_WORDS):
        return False
    protect = (k for k in KEYWORDS if k.lower() != "offer")
    return not any(k.lower() in t for k in protect)


def _uid_fetch_iter(mail, uids, parts, chunk=60):
    """分批 UID FETCH，yield (uid:int, meta:bytes, raw:bytes)。
    分批是为了避免单次请求行过长被服务器拒绝。"""
    ulist = sorted({int(u) for u in uids})
    for i in range(0, len(ulist), chunk):
        seg = b",".join(str(u).encode() for u in ulist[i:i + chunk])
        typ, data = mail.uid("FETCH", seg, f"({parts})")
        if typ != "OK" or not data:
            continue
        for item in data:
            if not isinstance(item, tuple):
                continue
            meta = item[0] or b""
            mu = re.search(rb"UID (\d+)", meta)
            if mu:
                yield int(mu.group(1)), meta, item[1]


def fetch_emails():
    """只读连接 Gmail，抓最近 DAYS 天邮件。
    R6 提速三件套：
    ① UID 增量缓存（mail_cache.json）——已抓过的邮件不再重拉正文，第二次扫描秒级；
    ② 新邮件先批量只拉标题，标题就能判定是广告的直接跳过，其余才拉全文；
    ③ 范围仍是 所有邮件(含已归档)+垃圾箱，Message-ID 跨文件夹去重。"""
    print(f"[1/4] 连接 Gmail（只读模式），同步最近 {DAYS} 天邮件…")
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)

    folder_all = _pick_folder(mail, "All Mail", "所有邮件")
    folder_trash = _pick_folder(mail, "Trash", "已删除邮件", "Bin")
    folders = [folder_all] if folder_all else ["INBOX"]
    if folder_trash:
        folders.append(folder_trash)
    print(f"      扫描文件夹：{'、'.join(folders)}")

    since = (datetime.now() - timedelta(days=DAYS)).strftime("%d-%b-%Y")
    cache = _load_mail_cache()
    n_full = n_adskip = 0

    for folder in folders:
        try:
            mail.select(f'"{folder}"', readonly=True)   # 只读：不删不改不标记
        except Exception as e:
            print(f"      跳过文件夹 {folder}：{e}")
            continue
        typ, sd = mail.uid("SEARCH", "SINCE", since)
        if typ != "OK":
            print(f"      {folder} 搜索失败，本轮保留旧缓存不动")
            continue
        valid = {int(x) for x in (sd[0].split() if sd and sd[0] else [])}
        old = cache.get(folder) or {}
        fc = {int(k): v for k, v in old.items() if int(k) in valid}
        fresh = [u for u in valid if u not in fc]

        if fresh:
            # ① 新邮件先只拉标题（一次批量往返，流量极小）
            ad_skip = set()
            for uid, _m, raw in _uid_fetch_iter(
                    mail, fresh, "BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)]"):
                try:
                    hm = email.message_from_bytes(raw)
                    subject = decode_str(hm.get("Subject"))
                except Exception:
                    subject = ""
                if _header_is_ad(subject):
                    ad_skip.add(uid)
            # ② 标题排除后剩下的，批量拉全文
            for uid, _m, raw in _uid_fetch_iter(
                    mail, [u for u in fresh if u not in ad_skip], "RFC822"):
                try:
                    msg = email.message_from_bytes(raw)
                    subject = decode_str(msg.get("Subject"))
                    body = extract_body(msg)
                    try:
                        date = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d")
                    except Exception:
                        date = datetime.now().strftime("%Y-%m-%d")
                    fc[uid] = {"subject": subject, "body": body[:3000],
                               "date": date, "mid": msg.get("Message-ID") or ""}
                except Exception:
                    continue
            n_adskip += len(ad_skip)
        n_full += sum(1 for u in fresh if u in fc)
        cache[folder] = {str(k): v for k, v in fc.items()}

    mail.logout()
    _save_mail_cache(cache)

    # 组装 + 去重：Message-ID 优先（跨文件夹重叠靠它），没有才退化为 文件夹+UID
    results, seen_mid, seen_weak = [], set(), set()
    for folder in folders:
        for k in sorted((cache.get(folder) or {}), key=lambda x: int(x)):
            m = cache[folder][k]
            mid = str(m.get("mid") or "").strip()
            if mid:
                if mid in seen_mid:
                    continue
                seen_mid.add(mid)
            else:
                weak = f"{folder}|{k}"
                if weak in seen_weak:
                    continue
                seen_weak.add(weak)
            results.append({"subject": m.get("subject", ""),
                            "body": m.get("body", ""),
                            "date": m.get("date", "")})
    total = len(results)
    print(f"      去重后邮件总数：{total}（新拉全文 {n_full} 封，标题预筛跳过广告 {n_adskip} 封，"
          f"其余直接来自本地缓存；第二次扫描会明显更快）")
    return results


def _pick_folder(mail, *candidates):
    """从 IMAP 文件夹列表里找出目标文件夹（兼容中英文 Gmail 界面）"""
    _, folders = mail.list()
    names = []
    for f in folders:
        s = f.decode(errors="replace")
        m = re.search(r'"([^"]+)"\s*$', s) or re.search(r' (\S+)$', s)
        if m:
            names.append(m.group(1))
    for want in candidates:
        for n in names:
            if want.lower() in n.lower():
                return n
    return None


def _kw_hit(keyword, text):
    """关键词命中：英文用词边界（避免 oc 命中 account、jd 命中 URL），中文仍包含匹配"""
    kw = keyword.lower()
    if re.fullmatch(r"[a-z0-9 \-]+", kw):
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None
    return kw in text


def filter_job_emails(emails):
    """关键词过滤。规则（按豆包建议的边界逻辑）：
    0. 标题命中账号/验证码/营销黑名单 → 直接丢弃，不进任何后续环节（含 AI 兜底）
    1. 全文转小写匹配（标题+正文）
    2. 命中广告排除词 → 直接丢弃（即使同时命中求职词，防培训广告误判）
    3. 单独命中英文 offer（营销"优惠"含义）不算求职，需另有其他求职词共现
    """
    kept = []
    dropped_nonjob = 0
    for m in emails:
        subj = m.get("subject", "")
        body = m.get("body", "")
        if _is_non_job(subj, body):
            dropped_nonjob += 1
            continue
        subj_l = subj.lower()
        text = (subj + " " + body).lower()
        # 强广告词全文拦
        if any(_kw_hit(ad, text) for ad in AD_BODY_WORDS):
            continue
        # 弱营销词只在标题判；标题本身有明确招聘语境则豁免
        if any(_kw_hit(ad, subj_l) for ad in AD_SUBJECT_WORDS) \
                and not _RECRUIT_SUBJ_RE.search(subj):
            continue
        hits = [k for k in KEYWORDS if _kw_hit(k, text)]
        # offer 歧义：唯一命中词是 offer 时不算
        if hits and hits != ["offer"]:
            kept.append(m)
    print(f"      筛选出求职邮件数量：{len(kept)}（账号/营销黑名单直接排除 {dropped_nonjob} 封）")
    return kept


# ================== AI 解析 ==================
PROMPT_TEMPLATE = """你是秋招投递信息提取助手。下面是{count}封求职相关邮件（标题+正文片段）。
请对【每一封】邮件各返回一条记录，共{count}条，一条都不能少；【严禁】把多封邮件合并成一条。

- 邮件序号：这封邮件在上面的编号，形如"邮件3"
- 投递日期：YYYY-MM-DD；【判断不出时，直接填这封邮件抬头标注的"邮件日期"（也就是收件日期）】
- 公司全称：公司完整名称，判断不出填"未知"
- 岗位名称：见下方要求
- 招聘阶段：按邮件原文【如实】填写，原文怎么说就怎么填
- 面试时间：具体场次的赴约时间，有则填 YYYY-MM-DD HH:mm，没有填"无"
- 截止时间：笔试/测评/AI面试等"窗口期任务"要求【完成的最后期限】（如"请于9月19日23:59前完成"），
  有则填 YYYY-MM-DD HH:mm，只有日期没钟点填 YYYY-MM-DD，没有填"无"。
  注意：具体场次的开考/赴约时间填"面试时间"，"X日前完成"这类期限才填"截止时间"，二者不要混淆。

【阶段措辞——必须原样区分，严禁吞并】
- 原文是"AI面试/AI视频面试/智能面试"→ 必须填"AI面试"，【不得】写成"面试"
- 原文是"在线初评/线上初评"→ 必须填"在线初评"，【不得】写成"面试"或"测评/笔试"
- 其他阶段照原文归纳：已投递/简历筛选中/待笔试/测评/AI面试/AI面试完成/在线初评/待面试/终面/OC/Offer/已拒绝/进入人才库 等

【非求职邮件——必须如实标记，严禁脑补】
若邮件是账号注册/登录验证码/OAuth授权/安全提醒/密码重置/产品欢迎信/产品营销/订阅推送等
（如 GitHub、Google 账号、Mistral、together.ai 等开发者平台的通知），与求职招聘无关：
公司全称、岗位名称、招聘阶段、面试时间、截止时间【全部填"未知"】，并在招聘阶段填"未知"。
【严禁】仅因出现"申请/apply""欢迎加入/welcome""录用/hire"等字样就编造"已投递"等招聘状态。

【严格要求】
1. 岗位名称：必须结合标题+正文尽力提取；像"产品运营-校招/实习转正""游戏策划-增长方向"这种要完整保留，不要截断；
   只有全文确实【没有任何】岗位/职位信息时，才允许填"未知"。不要把"校招/校园招聘"当成岗位名。
2. 投递日期：凡是邮件抬头有日期的，一律不允许返回"未知"，直接填该收件日期。
3. 公司全称：信息不足就填"未知"，绝对禁止编造、猜测公司名/岗位/时间。
4. 每封邮件下方若附有"本地已识别"，那是规则引擎的半成品：正确的字段直接沿用，错误或标着"未知"的字段你来补正。
5. 只返回 JSON 数组，不要任何多余文字、不要用 markdown 代码块。
6. 每个元素格式：{{"邮件序号":"邮件i","投递日期":"...","公司全称":"...","岗位名称":"...","招聘阶段":"...","面试时间":"...","截止时间":"..."}}

【邮件列表】
{mails}
"""


def _unwrap_pair(pair):
    """build_prompt 的元素可以是 mail dict，也可以是 (mail, local_rec) 元组。"""
    if isinstance(pair, tuple):
        return pair[0], pair[1] if len(pair) > 1 else None
    return pair, None


def build_prompt(pairs):
    blocks = []
    for i, pair in enumerate(pairs, 1):
        m, local = _unwrap_pair(pair)
        block = f"--- 邮件{i}（日期:{m['date']}）---\n标题：{m['subject']}\n正文：{m['body']}"
        if local:
            hints = []
            for k in ("公司全称", "岗位名称", "招聘阶段", "截止时间"):
                v = str(local.get(k) or "未知")
                hints.append(f"{k}={v}")
            block += "\n本地已识别（请核对/补正）：" + "，".join(hints)
        blocks.append(block)
    return PROMPT_TEMPLATE.format(count=len(pairs), mails="\n\n".join(blocks))


def call_deepseek(prompt):
    """调用 AI 模型（OpenAI 兼容接口；默认 DeepSeek，可在页面「设置」里换其他模型/接口）"""
    _base = MODEL_BASE_URL.rstrip("/")
    req = urllib.request.Request(
        _base + "/chat/completions",
        data=json.dumps({
            "model": MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {DEEPSEEK_API_KEY}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def extract_json_array(text):
    """从模型返回文本里稳健提取 JSON 数组"""
    text = text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    # 兼容 {"records":[...]} 或纯 [...]
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("返回内容里没有 JSON 数组")
    return json.loads(text[start:end + 1])


def mode_a_parse(mails):
    print("[2/4] 模式A：调用 DeepSeek API 解析…")
    content = call_deepseek(build_prompt(mails))
    return extract_json_array(content)


def mode_b_parse(mails):
    print("[2/4] 模式B：不调用 API，生成 Trae 待解析文本…")
    prompt = build_prompt(mails)
    (BASE_DIR / PROMPT_FILE).write_text(prompt, encoding="utf-8")
    # 尽力自动复制到剪贴板（Windows），失败也不影响
    try:
        subprocess.run(["clip"], input=prompt.encode("gbk", errors="replace"),
                       check=False, capture_output=True)
        clip_ok = True
    except Exception:
        clip_ok = False

    result_path = BASE_DIR / RESULT_FILE
    if result_path.exists():
        print(f"检测到 {RESULT_FILE}，直接读取结果…")
        content = result_path.read_text(encoding="utf-8")
        records = extract_json_array(content)
        # 处理完改名，防止下次误用旧结果
        result_path.rename(BASE_DIR / f"trae_result_已处理_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        return records

    print("=" * 60)
    print(f"已把全部 {len(mails)} 封邮件打包成 prompt：")
    print(f"  · 已保存到文件：{PROMPT_FILE}")
    if clip_ok:
        print("  · 已自动复制到剪贴板，直接 Ctrl+V 发给 Trae 即可")
    print("\n下一步（二选一）：")
    print(f"  方式1：把 Trae 返回的 JSON 存成 {RESULT_FILE}，然后重新运行 python main.py")
    print("  方式2：把 Trae 返回的 JSON 粘贴到下面，最后一行单独输入 END 回车")
    print("=" * 60)

    try:
        first = input("粘贴 JSON 后回车（或 Ctrl+C 退出）：\n")
    except EOFError:
        sys.exit(f"当前是非交互终端：请把 Trae 返回的 JSON 保存为 {RESULT_FILE} 后重新运行 python main.py")
    if not first.strip():
        sys.exit(f"未收到内容：请把 JSON 粘贴进来，或存成 {RESULT_FILE} 后重新运行")
    lines = [first]
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    return extract_json_array("\n".join(lines))


# ================== 状态库 ==================
def load_status_list():
    path = BASE_DIR / STATUS_FILE
    if not path.exists():
        initial = ["已投递", "简历筛选中", "待笔试/测评", "待面试", "终面",
                   "OC", "Offer", "已拒绝", "进入人才库"]
        path.write_text(json.dumps(initial, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def save_status_list(statuses):
    (BASE_DIR / STATUS_FILE).write_text(
        json.dumps(statuses, ensure_ascii=False, indent=2), encoding="utf-8")


# ================== 台账更新 ==================
def normalize_records(raw_records, status_list):
    """补默认字段 + 人工复核标记 + 新状态处理"""
    records, new_status_count = [], 0
    for r in raw_records:
        rec = {c: str(r.get(c, "") or "").strip() for c in
               ["投递日期", "公司全称", "岗位名称", "招聘阶段", "面试时间", "截止时间"]}
        if not rec["面试时间"]:
            rec["面试时间"] = "无"
        # 截止时间合法性：形如 2026-09-19[ 23:59]，否则归一为"无"
        dl = rec["截止时间"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?", dl or ""):
            rec["截止时间"] = "无"
        # 公司名都认不出 → 无法归档，丢弃
        if rec["公司全称"] in ("", "未知", "不确定", "N/A"):
            continue
        # 公司明确但阶段认不出 → 默认"已投递"（能进来说明至少投过了）
        if rec["招聘阶段"] in ("", "未知", "不确定", "N/A"):
            rec["招聘阶段"] = "已投递"
        rec["人工复核"] = "否"
        # 状态识别：优先匹配状态库；全新状态自动扩充并标记
        rec["新状态待人工归类"] = "false"
        if rec["招聘阶段"] and rec["招聘阶段"] not in ("未知", "不确定"):
            if rec["招聘阶段"] not in status_list:
                status_list.append(rec["招聘阶段"])
                rec["新状态待人工归类"] = "true"
                new_status_count += 1
                print(f"      发现新状态：「{rec['招聘阶段']}」已追加进 {STATUS_FILE}")
        rec["最后更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        records.append(rec)
    return records, new_status_count


def update_csv(records):
    """以「公司+岗位」为唯一键：不存在则新增，状态变化则更新"""
    path = BASE_DIR / CSV_FILE
    if path.exists():
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    else:
        df = pd.DataFrame(columns=COLUMNS)
    # 旧台账缺新列（如"截止时间"）→ 补空列，顺序对齐 COLUMNS
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df = df.reindex(columns=COLUMNS)

    added = updated = 0
    for rec in records:
        key = (rec["公司全称"], rec["岗位名称"])
        mask = (df["公司全称"] == key[0]) & (df["岗位名称"] == key[1])
        if mask.any():
            idx = df[mask].index[0]
            # 状态变了更新整行；状态没变但新识别出面试/截止时间，也要补进去
            time_gained = (
                (df.at[idx, "面试时间"] in ("", "无") and rec["面试时间"] not in ("", "无"))
                or (df.at[idx, "截止时间"] in ("", "无") and rec["截止时间"] not in ("", "无"))
            )
            if df.at[idx, "招聘阶段"] != rec["招聘阶段"] or time_gained:
                for c in COLUMNS:
                    df.at[idx, c] = rec[c]
                updated += 1
        else:
            df = pd.concat([df, pd.DataFrame([rec])], ignore_index=True)
            added += 1

    df.to_csv(path, index=False, encoding="utf-8-sig")  # utf-8-sig：Excel打开不乱码
    return added, updated


# ================== 供网页调用 ==================


# ================== 公司名多层清洗 + AI 复核 ==================
# 老板要的分层思路：本地规则先跑（免费、秒出）→ 拿不准的再交给 DeepSeek 复核（一批一次调用，省钱）
#   第1层 去前后缀： "感谢您申请TCL"→"TCL"、"并应聘基恩士"→"基恩士"
#   第2层 丢纯标题： "关于我们" → 丢掉
#   第3层 去后缀噪音： "网易招聘"→"网易"、"京东校招"→"京东"、"X在线"→"X"
#   第4层 DeepSeek 复核：只处理本地拿不准的

CLEAN_PREFIX1 = re.compile(r"^感谢您(申请|投递|应聘|投递了|已投递|报名|关注)\s*")
CLEAN_PREFIX2 = re.compile(r"^(并|已|我|您|请|此|本)?(可以|能|可|要)?(应聘|申请|投递|登录|登陆|报名|关注|查看|收到)\s*")
CLEAN_PREFIX3 = re.compile(r"^(恭喜您|您好|尊敬的|亲爱的)[^,，。:：]{0,8}(申请|投递|应聘)?\s*")
CLEAN_JUNK = re.compile(r"^(关于我们|我们是谁|公司简介|企业简介|集团简介|笔试邀请|参加测评方式|宣讲信息|宣讲会|"
                        r"简历投递回复|笔试注意事项|招聘公告|招聘信息|岗位介绍|投递须知|温馨提示|系统通知|"
                        r"此邮件由系统发出|尊敬的候选人)$")
CLEAN_SUFFIX = re.compile(r"(在线|官方招聘|招聘官网|官方网站|官方|招聘系统|校园招聘|校招|招聘|官网|系统)$")
CLEAN_WORD_JUNK = re.compile(r"^(您好|谢谢|通知|提醒|邀请|测试|免费|系统|邮件|申请|投递)$")


def clean_company_name(name):
    """本地多层清洗公司名。返回清洗后的名字；返回空串 = 这条是垃圾，该丢掉。"""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    if not s:
        return ""
    s = CLEAN_PREFIX1.sub("", s)
    s = CLEAN_PREFIX2.sub("", s)
    s = CLEAN_PREFIX3.sub("", s)
    if CLEAN_JUNK.match(s):
        return ""
    s = CLEAN_SUFFIX.sub("", s).strip()
    if len(s) < 2 or CLEAN_WORD_JUNK.match(s):
        return ""
    return s


def looks_suspicious(co):
    """本地规则没把握的名字 → 值得让 AI 复核一眼"""
    if not co:
        return False
    if len(co) > 14:
        return True
    return bool(re.search(r"感谢|投递|申请|应聘|登录|关于我们|在线|招聘|校招|通知|邀请|回复|系统|免费", co))


def review_companies(records):
    """最后一道：本地多层清洗 + DeepSeek 复核公司名。
    本地先跑（免费）；只有拿不准的才攒成一批问 AI —— 一批一次调用，不逐条烧钱。"""
    if not records:
        return records
    # ── 第1~3层：本地规则 ──
    after_local = []
    for r in records:
        old = str(r.get("公司全称") or "").strip()
        new = clean_company_name(old)
        if not new:
            print("      [清洗] 丢掉非公司名：%s" % old)
            continue
        if new != old:
            print("      [清洗] %s → %s" % (old, new))
            r["公司全称"] = new
        after_local.append(r)

    # ── 第4层：DeepSeek 复核 ──
    if not DEEPSEEK_API_KEY:
        print("      [复核] 没配 DeepSeek key，跳过 AI 复核（本地清洗已生效）")
        return after_local
    suspects = sorted({str(r.get("公司全称") or "").strip() for r in after_local
                       if looks_suspicious(r.get("公司全称"))})
    if not suspects:
        return after_local
    print("      [复核] %d 个名字本地拿不准，交给 DeepSeek 复核…" % len(suspects))
    prompt = ("下面是求职者邮箱里抓出来的公司名，其中混了一些不是公司名的垃圾（邮件标题、问候语、通知用语）。\n"
              "请逐条判断：\n"
              "1) 是真公司名 → 给出规范写法。像\"万葭灯火（抖音生活服务旗下品牌）\"这种，"
              "把括号里的\"说明性文字\"去掉留公司名即可；但\"点点互动（北京）科技有限公司\"这种，"
              "括号是公司全称的一部分（地名），要保留。\n"
              "2) 不是公司（邮件标题 / 问候语 / 通知用语 / 纯感叹词）→ \"规范公司名\" 写 null。\n"
              "3) 只要拿不准，就当它是真公司原样保留，宁可留着让用户自己删，也别乱删。\n"
              "只输出 JSON 数组，格式： [{\"原公司名\":\"\",\"规范公司名\":\"\",\"理由\":\"\"}]\n\n"
              + "\n".join("- " + x for x in suspects))
    try:
        fixed = extract_json_array(call_deepseek(prompt))
    except Exception as e:
        print("      [复核] DeepSeek 复核失败（%s），保留本地清洗结果" % e)
        return after_local
    fixmap = {}
    for it in fixed:
        if isinstance(it, dict):
            fixmap[str(it.get("原公司名", "")).strip()] = it.get("规范公司名")
    out = []
    for r in after_local:
        co = str(r.get("公司全称") or "").strip()
        if co in fixmap:
            nc = fixmap[co]
            if nc is None or str(nc).strip() == "":
                print("      [复核] 丢掉：%s" % co)
                continue
            nc = str(nc).strip()
            if nc != co:
                print("      [复核] %s → %s" % (co, nc))
                r["公司全称"] = nc
        out.append(r)
    return out

SCAN_LOG = BASE_DIR / "scan_log.json"

LEARNED_FILE = BASE_DIR / "learned_rules.json"


def summarize_patterns(records, mails):
    """扫描后：让 AI 从"邮件标题 → 招聘阶段"里总结规律，存进 learned_rules.json。
    这样下次本地规则就能直接认出来（越用越准）。找不到规律就返回空。"""
    if not DEEPSEEK_API_KEY:
        return []
    pairs = []
    for m in mails:
        subj = (m.get("subject") or "").strip()
        if not subj:
            continue
        for r in records:
            co = (r.get("公司全称") or "").strip()
            if co and co != "未知" and co in subj:
                stage = (r.get("招聘阶段") or "").strip()
                if stage and stage != "未知":
                    pairs.append((subj[:100], co, stage))
                break
    if len(pairs) < 3:
        return []
    prompt = ("下面是求职者邮箱里一些邮件标题，以及它们对应的招聘阶段。\n"
              "请总结出【标题里出现哪些关键词 → 说明是哪个阶段】的规律，供以后自动识别用。\n"
              "【严格要求】\n"
              "1) 只总结【稳定可靠】的规律：同一个关键词在绝大多数邮件里都指向同一个阶段（比如「笔试邀请」「测评通知」「面试邀请」这类）。\n"
              "2) 【不要】总结含义模糊的（如「反馈」「回复」「通知」「进度」单独出现，可能是拒信也可能是进展），宁可漏掉也别写错。\n"
              "3) 【不要】总结跟求职无关的邮件（隐私政策、GitHub 通知、账号验证等）。\n"
              "4) 关键词要短（2-8 个字），要能明确指向阶段。\n"
              "5) 拿不准的一条都别写，返回空数组也可以。\n"
              "只输出 JSON 数组：[{\"keyword\":\"关键词\",\"stage\":\"招聘阶段\",\"why\":\"一句话\"}]\n\n"
              + "\n".join("- 标题：" + a + "　公司：" + b + "　阶段：" + c for a, b, c in pairs[:30]))
    try:
        out = extract_json_array(call_deepseek(prompt))
    except Exception:
        return []
    old = []
    try:
        if LEARNED_FILE.exists():
            old = json.loads(LEARNED_FILE.read_text(encoding="utf-8"))
    except Exception:
        old = []
    if not isinstance(old, list):
        old = []
    seen = {str(x.get("keyword")) for x in old if isinstance(x, dict)}
    for x in out:
        if isinstance(x, dict) and x.get("keyword") and str(x["keyword"]) not in seen:
            old.append({"keyword": str(x["keyword"]), "stage": str(x.get("stage", "")), "why": str(x.get("why", ""))})
            seen.add(str(x["keyword"]))
    LEARNED_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    return old

def _fill_missing_dates(raw, mails):
    """日期双保险：投递日期是"未知"/空时，用邮件本身日期兜底。
    按公司名（能对上岗名更好）在原始邮件里找，取该公司最新一封的日期。
    返回本次补上的条数。"""
    def _norm(s):
        return re.sub(r"\s", "", str(s or "")).lower()
    fixed = 0
    for r in raw:
        d = str(r.get("投递日期") or "").strip()
        if d and d != "未知":
            continue
        cn = _norm(r.get("公司全称"))
        if not cn or cn == "未知":
            continue
        jb = _norm(r.get("岗位名称"))
        md = ""
        for m in mails:
            mt = _norm((m.get("subject") or "") + (m.get("body") or ""))
            if cn in mt and (not jb or jb == "未知" or jb in mt):
                md = m.get("date", "") or md
        if md:
            r["投递日期"] = md
            fixed += 1
    return fixed


_UNKNOWN = {"", "未知", "无", "不确定", "none", "null", "n/a", "na", "未识别"}


def _is_unknown(v):
    return str(v or "").strip().lower() in _UNKNOWN


def _fill_job_from_sibling(raw):
    """同公司在本批里只有唯一一个已知岗位时，给该公司"岗位未知"的记录补上。
    典型场景：瑞幸 AI 面试提醒邮件正文只在条件句里提岗位，主邀请信里有"储备店长"，
    DeepSeek 不可用时也不至于多出一张空岗卡。"""
    jobs_by_company = {}
    for r in raw:
        c = str(r.get("公司全称") or "").strip()
        j = str(r.get("岗位名称") or "").strip()
        if c and not _is_unknown(j):
            jobs_by_company.setdefault(c, set()).add(j)
    fixed = 0
    for r in raw:
        if _is_unknown(r.get("岗位名称")):
            js = jobs_by_company.get(str(r.get("公司全称") or "").strip())
            if js and len(js) == 1:
                r["岗位名称"] = next(iter(js))
                fixed += 1
    return fixed


def _merge_ai_with_local(ai_records, pairs):
    """AI 结果与本地半成品做字段级合并：
    - AI 返回为空/未知的字段，用本地半成品补（本地岗位/公司规则现在已很准）；
    - AI 漏返回的邮件序号，用本地半成品整条兜底（公司+阶段齐全才入库）；
    - 返回合并后的 records（已含邮件序号，便于后续日期兜底）。"""
    def _norm_num(s):
        mnum = re.search(r"(\d+)", str(s or ""))
        return int(mnum.group(1)) if mnum else None

    used = set()
    out = []
    fields = ("投递日期", "公司全称", "岗位名称", "招聘阶段", "面试时间", "截止时间")
    for ar in ai_records:
        if not isinstance(ar, dict):
            continue
        i = _norm_num(ar.get("邮件序号"))
        mail, local = None, None
        if i is not None and 1 <= i <= len(pairs):
            used.add(i)
            mail, local = pairs[i - 1][0], pairs[i - 1][1]
        # 反幻觉：标题就是账号/验证码/营销通知（本地三字段全未知）→ AI 再怎么编也丢弃
        if mail and local and _is_unknown(local.get("公司全称")) \
                and _is_unknown(local.get("岗位名称")) \
                and _is_unknown(local.get("招聘阶段")) \
                and _is_non_job(mail.get("subject", ""), mail.get("body", "")):
            continue
        rec = dict(ar)
        rec["邮件序号"] = f"邮件{i}" if i else ar.get("邮件序号", "")
        if local:
            for k in fields:
                if _is_unknown(rec.get(k)) and not _is_unknown(local.get(k)):
                    rec[k] = local[k]
        out.append(rec)
    # AI 整封漏掉的：用本地半成品兜底
    for idx, (mail, local) in enumerate(pairs, 1):
        if idx in used:
            continue
        if not _is_unknown(local.get("公司全称")) and not _is_unknown(local.get("招聘阶段")):
            rec = dict(local)
            rec["邮件序号"] = f"邮件{idx}"
            out.append(rec)
    return out


def _local_fallback(pairs):
    """AI 整条路不可用时的兜底：公司+阶段齐全的半成品直接入库。"""
    return [dict(loc) for _m, loc in pairs
            if not _is_unknown(loc.get("公司全称"))
            and not _is_unknown(loc.get("招聘阶段"))]


def _ai_fallback_concurrent(need_ai, batch_size=12, max_workers=4):
    """把本地没认全的邮件分批，并发调 DeepSeek（每批独立 prompt/序号，互不影响）。
    某一批超时或报错 → 该批退本地半成品，不牵连其他批。返回合并后的 records。"""
    batches = [need_ai[i:i + batch_size]
               for i in range(0, len(need_ai), batch_size)]

    def _one(batch):
        content = call_deepseek(build_prompt(batch))
        return _merge_ai_with_local(extract_json_array(content), batch)

    out, failed = [], []
    if len(batches) == 1:                     # 只有一批就不必开线程池
        try:
            return _one(batches[0])
        except Exception as e:
            print(f"      DeepSeek 调用失败（{e}），该批用本地半成品兜底")
            return _local_fallback(batches[0])
    with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as ex:
        futs = {ex.submit(_one, b): b for b in batches}
        for fut in as_completed(futs):
            b = futs[fut]
            try:
                out.extend(fut.result())
            except Exception as e:
                print(f"      一批 DeepSeek 调用失败（{e}），{len(b)} 封改用本地半成品兜底")
                failed.append(b)
    for b in failed:
        out.extend(_local_fallback(b))
    return out


def run_scan():
    """执行一轮邮箱扫描，返回简报 dict 并写 scan_log.json。
    全程自动：先跑本地规则引擎（免费），识别不出的邮件再调 DeepSeek 兜底；
    DeepSeek 没 key 或没余额也不影响——本地结果照常入库。"""
    from local_parser import parse_emails
    emails = fetch_emails()
    mails = filter_job_emails(emails)
    report = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": len(emails), "job_emails": len(mails),
        "added": 0, "updated": 0, "new_status": 0, "review": 0,
        "engine": "本地规则引擎",
    }
    if mails:
        raw, need_ai = parse_emails(mails)
        # DeepSeek 兜底：本地公司/阶段/岗位任一没认出的邮件，带半成品并发分批送
        if need_ai and DEEPSEEK_API_KEY:
            print(f"      {len(need_ai)} 封本地识别不全，分"
                  f"{(len(need_ai) + 11) // 12} 批并发调 DeepSeek 兜底…")
            try:
                raw += _ai_fallback_concurrent(need_ai)
                report["engine"] = "本地+DeepSeek"
            except Exception as e:
                print(f"      DeepSeek 兜底整体失败（{e}），用本地半成品兜底入库")
                raw += _local_fallback(need_ai)
                report["engine"] = "本地规则引擎（DeepSeek不可用）"
        elif need_ai:
            # 没配 key：半成品里公司+阶段齐全的也照常入库，只有岗位未知可后续补
            raw += _local_fallback(need_ai)
        # 同公司本批只有唯一已知岗位 → 补给岗位未知的同公司邮件（避免空岗卡）
        try:
            report["job_filled"] = _fill_job_from_sibling(raw)
        except Exception:
            report["job_filled"] = 0
        # ★ 日期双保险：日期仍未知的，用邮件本身日期兜底（先按邮件序号，再按公司名）
        try:
            report["date_filled"] = _fill_missing_dates(raw, mails)
        except Exception:
            report["date_filled"] = 0
        for _r in raw:
            _r.pop("邮件序号", None)     # 内部字段，别写进 CSV
        raw = review_companies(raw)          # 最后一道：本地清洗 + DeepSeek 复核公司名
        status_list = load_status_list()
        records, n = normalize_records(raw, status_list)
        save_status_list(status_list)
        added, updated = update_csv(records)
        report.update(added=added, updated=updated, new_status=n,
                      review=sum(1 for r in records if r["人工复核"] == "是"))
        # ★ 详细清单：每条识别到什么（方便用户核对，知道有没有出错）
        report["items"] = [
            {
                "company": (r.get("公司全称") or "").strip(),
                "job": (r.get("岗位名称") or "").strip(),
                "status": (r.get("招聘阶段") or "").strip(),
                "interview": (r.get("面试时间") or "").strip(),
                "deadline": (r.get("截止时间") or "").strip(),
                "date": (r.get("投递日期") or "").strip(),
                "review": (r.get("人工复核") == "是"),
            }
            for r in records
        ]
        # ★ 交给 AI 兜底的数量（这些邮件本地规则没认出公司/状态）
        report["sent_to_ai"] = len(need_ai or [])
        # ★ 真正需要人看的：本地+AI 都没搞定的（人工复核=是）
        report["failed"] = []
        for r in records:
            if r.get("人工复核") == "是":
                report["failed"].append((r.get("公司全称") or "?") + "：这封邮件没认出状态，建议自己看一眼")
        # ★ 扫描后总结"标题关键词 → 阶段"的规律（下次本地就能认）
        try:
            _rules = summarize_patterns(records, mails)
            report["learned"] = len(_rules or [])
        except Exception as _e:
            report["learned"] = 0
    SCAN_LOG.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


# ================== 主流程 ==================
def main():
    if not GMAIL_EMAIL or not GMAIL_APP_PASSWORD:
        sys.exit("错误：还没配置邮箱，请在页面右上角「设置」里填写 Gmail 邮箱和应用专用密码")
    if AUTO_USE_DEEPSEEK and not DEEPSEEK_API_KEY:
        sys.exit("错误：还没配置 AI 模型，请在页面右上角「设置」里填写模型 API Key（默认 DeepSeek）")

    emails = fetch_emails()
    total = len(emails)
    mails = filter_job_emails(emails)

    if not mails:
        print("\n===== 本次简报 =====")
        print(f"本次扫描邮件总数：{total}\n筛选出求职邮件数量：0\n新增记录数：0\n"
              f"更新状态记录数：0\n新发现状态数量：0\n需要人工复核条数：0")
        return

    raw = mode_a_parse(mails) if AUTO_USE_DEEPSEEK else mode_b_parse(mails)

    raw = review_companies(raw)              # 最后一道：本地清洗 + DeepSeek 复核公司名
    print("[3/4] 状态识别 + 写入台账…")
    status_list = load_status_list()
    records, new_status_count = normalize_records(raw, status_list)
    save_status_list(status_list)
    added, updated = update_csv(records)

    review_count = sum(1 for r in records if r["人工复核"] == "是")

    print("[4/4] 完成！")
    print("\n===== 本次简报 =====")
    print(f"本次扫描邮件总数：{total}")
    print(f"筛选出求职邮件数量：{len(mails)}")
    print(f"新增记录数：{added}")
    print(f"更新状态记录数：{updated}")
    print(f"新发现状态数量：{new_status_count}")
    print(f"需要人工复核条数：{review_count}")
    print(f"\n台账文件：{CSV_FILE}（可运行 python dashboard.py 生成可视化看板）")


if __name__ == "__main__":
    main()
