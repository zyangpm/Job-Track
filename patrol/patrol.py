#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
秋招网申状态低频巡检器（仅本机运行，不上传任何数据）

用途：用带登录态的本地 Chromium 逐个访问你已投递公司的校招官网后台，
按"关键词窗口匹配"（无需手动找 XPath）提取投递状态，写入
dashboard/application_status.json 供本地看板展示。

铁律：
  - 两次巡检间隔强制 >= min_interval_hours（默认 4 小时），--force 才能临时越过
  - 顺序访问、公司之间随机等待 8~18 秒，只看自己的申请页，绝不批量爬取
  - 登录态（Cookie）只存在本机 patrol/.secrets/，请勿分享该目录

风险提示：本工具仅作个人秋招备忘，不保证 100% 通过所有网站的登录校验，
也不承担任何账号风控风险；若某平台明确禁止自动化访问，请勿对该平台启用。

用法：
  python patrol.py                 # 单次巡检（受 4 小时强制间隔约束）
  python patrol.py --force         # 无视间隔立即巡检（请节制使用）
  python patrol.py --loop          # 常驻模式：每 4h+随机抖动 自动巡检一轮
  python patrol.py --companies 腾讯,美团   # 只巡检指定公司
  python patrol.py --headed        # 有头模式（排查问题时用）
  python patrol.py --selftest      # 不开浏览器，自测状态提取逻辑
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
OUTPUT_PATH = BASE / "patrol_status.json"   # 结果文件（同时同步进追踪表html）
SCREENSHOT_DIR = BASE / "screenshots"
CAPTURE_DIR = BASE / ".secrets" / "captures"
PROFILE_ROOT = BASE / ".secrets" / "profiles"

# 巡检状态 → 追踪表状态映射（只升级不回退）
STATUS_TO_TRACKER = {
    "Offer": "offer", "进入人才库": "talent", "已挂": "rejected",
    "AI面试": "aiiv", "面试": "interview", "笔试": "test",
    "测评": "assess", "简历筛选中": "in_progress",
}

# 状态识别规则：状态名直接用看板已有的状态栏（老板定的：不许造"通过"这种没人懂的名字）。
# 顺序即优先级（先终态：Offer/人才库/已挂，再流程态）。
# 全部是文本关键词匹配，针对页面正文中公司名附近的 ±250 字窗口，
# 不依赖任何 XPath/选择器，网站改版也大概率不受影响。
STATUS_RULES = [
    ("Offer", ["offer", "Offer", "OFFER", "录用通知", "拟录用", "已接受"]),
    ("进入人才库", ["人才库", "已入库", "简历入库"]),
    ("已挂", ["流程终止", "不再推进", "未通过", "不匹配", "已淘汰", "已结束",
              "很遗憾", "已被拒绝", "已拒绝", "未通过筛选", "感谢您的关注"]),
    ("AI面试", ["AI面试", "AI 面试", "AI面"]),
    ("面试", ["一面", "二面", "三面", "复试", "终面", "面试"]),
    ("笔试", ["笔试", "在线考试", "行测"]),
    ("测评", ["测评", "性格测试", "在线测试"]),
    ("简历筛选中", ["简历初筛", "简历筛选", "筛选中", "简历评估", "评估中", "待筛选",
                    "待处理", "已投递", "审核中", "处理中", "录用评估"]),
]

# 结构化进度字段（腾讯 progress.html 这类页面）优先于关键词：
# 页面把全部流程环节列成图例（集体面试/初试/…/Offer/录用评估中），
# 纯关键词会命中图例误判；"是否发起面试：否"这种字段才是真实状态。
PROGRESS_FIELD_RULES = [
    (re.compile(r"是否发起面试[：: ]*是|是\s*发起面试"), "面试"),
    (re.compile(r"是否发起面试[：: ]*否|否\s*发起面试"), "简历筛选中"),
]
RULE_PRIO = {code: i for i, (code, _) in enumerate(STATUS_RULES)}
# 页面停留在登录页的典型文案（说明登录态失效，需要重新跑 login.py）
LOGIN_HINTS = ["请登录", "扫码登录", "立即登录", "登录/注册", "微信扫码登录", "登录后查看"]
# 页面明确显示"没有投递记录"的典型文案（已登录+翻遍入口后才算"证明没投"）
EMPTY_RECORD_RE = re.compile(
    r"(暂无投递|暂无申请|没有投递|还没有投递|暂未投递|暂无记录|暂无数据|"
    r"暂无职位|未投递过|暂无报名|暂无应聘|暂无求职|还没有申请|暂无投递记录)")

NORMAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

NOW = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{NOW()}] {msg}", flush=True)


def classify_window(text):
    """在一段文本窗口里按优先级找状态关键词，返回 (状态码, 命中词) 或 (None, None)"""
    for code, pats in STATUS_RULES:
        for p in pats:
            if p == "面试":
                # 排除"AI面试"，避免把 AI 面误判成普通面试
                if re.search(r"(?<!AI)面试", text) and not re.search(r"AI\s?面试", text):
                    return code, "面试"
                continue
            if p in text:
                return code, p
    return None, None


def find_status(page_text, aliases, other_aliases=()):
    """结构化进度字段（如"是否发起面试：否"）优先；否则在公司名 ±250 字窗口内
    按关键词找状态。窗口会避开其他公司名，防止隔壁公司的状态算到本头上。
    返回 (status_code, keyword, evidence_snippet) 或 (None, None, None)。"""
    for rx, st in PROGRESS_FIELD_RULES:          # 结构化字段优先，防流程图例误判
        m = rx.search(page_text)
        if m:
            snippet = page_text[max(0, m.start() - 50):m.end() + 50].replace("\n", " ").strip()
            return st, m.group(0), snippet
    other_spans = []
    for oa in other_aliases:
        if oa:
            other_spans.extend(m.span() for m in
                               re.finditer(re.escape(oa), page_text, re.IGNORECASE))
    best = None  # (prio, code, kw, snippet)
    for alias in aliases:
        for m in re.finditer(re.escape(alias), page_text, re.IGNORECASE):
            s, e = max(0, m.start() - 250), min(len(page_text), m.end() + 250)
            for (os_, oe_) in other_spans:
                if oe_ <= m.start():
                    s = max(s, oe_)
                elif os_ >= m.end():
                    e = min(e, os_)
            if s >= e:
                continue
            window = page_text[s:e]
            code, kw = classify_window(window)
            if code and (best is None or RULE_PRIO[code] < best[0]):
                k = window.find(kw)
                snippet = window[max(0, k - 40):k + 60].replace("\n", " ").strip()
                best = (RULE_PRIO[code], code, kw, snippet)
    if best:
        return best[1], best[2], best[3]
    return None, None, None


def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def next_allowed_from_state(cfg):
    st = load_state()
    if not st.get("next_allowed"):
        return None
    try:
        return datetime.strptime(st["next_allowed"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def plan_next_run(cfg):
    jitter_min = float(cfg.get("jitter_minutes", 20))
    nxt = datetime.now() + timedelta(hours=float(cfg.get("min_interval_hours", 4)),
                                     minutes=random.uniform(0, jitter_min))
    return nxt.strftime("%Y-%m-%d %H:%M:%S")


def guard(cfg, force):
    """强制间隔检查：距上次巡检不足 min_interval_hours 时拒绝运行。
    返回 True=放行，False=被拦（已经打印原因）。"""
    if force:
        log("--force：跳过间隔保护（请节制使用，避免触发平台风控）")
        return True
    nxt = next_allowed_from_state(cfg)
    if nxt and datetime.now() < nxt:
        log(f"距上次巡检间隔不足 {cfg.get('min_interval_hours', 4)} 小时，"
            f"本次拒绝运行（下次可巡检：{nxt}）。这是为了保护你的账号不被风控。")
        log("确有紧急需要可用 --force；日常请用 --loop 常驻模式自动控制节奏。")
        return False
    return True


def wait_until_allowed(cfg):
    """--loop 模式用：睡到下次允许巡检的时间点。"""
    nxt = next_allowed_from_state(cfg)
    if not nxt:
        return
    remain = (nxt - datetime.now()).total_seconds()
    if remain > 0:
        log(f"常驻模式：休息到 {nxt} 再巡检（约 {remain/3600:.1f} 小时），窗口保持开着即可。")
        try:
            time.sleep(remain)
        except KeyboardInterrupt:
            log("已手动停止。")
            sys.exit(0)


def _maybe_new_page(ctx, page):
    """点击后可能弹了新标签页 → 返回最新那个"""
    try:
        pages = ctx.pages
        if pages and pages[-1] is not page:
            p = pages[-1]
            try:
                p.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            return p
    except Exception:
        pass
    return page


def _click_first_text(page, texts):
    """按文字精确匹配点第一个可见元素"""
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=True)
            cnt = loc.count()
        except Exception:
            continue
        for i in range(min(cnt, 3)):
            try:
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=2500)
                    return True
            except Exception:
                continue
    return False


def _wait_for_progress(page, before_url, timeout_ms=8000):
    """点击后不许死等新标签页：同页 SPA 跳转也要识别。
    轮询：page.url 变了，或 body 里出现多个"申请/投递/状态"类词 → 认为已到位。"""
    hint_words = ("投递", "申请", "进度", "状态", "简历", "笔试", "面试", "测评")
    waited = 0
    while waited < timeout_ms:
        try:
            page.wait_for_timeout(500)
        except Exception:
            return False
        waited += 500
        try:
            if page.url != before_url:
                return True
            body = page.inner_text("body", timeout=3000) or ""
            if sum(1 for w in hint_words if w in body) >= 3:
                return True
        except Exception:
            return False
    return False


def _click_account_area(page):
    """点「登录按钮原来的位置」——登录后那里变成 头像 / 用户名 / 「你好，xxx」。
    点一下（点不动就悬停）通常会弹出含「我的投递/个人中心」的菜单或进个人中心。
    返回动作描述（用于留证），没点到返回 None。"""
    # ① 顶栏问候语：你好，xxx / 您好，xxx / Hi,xxx（登录后才有）
    try:
        els = page.query_selector_all(
            "header *, nav *, .header *, .navbar *, .top *, .topbar *, .top-nav *")
    except Exception:
        els = []
    for el in els[:400]:
        try:
            if not el.is_visible():
                continue
            t = re.sub(r"\s+", "", el.inner_text() or "")
        except Exception:
            continue
        if re.match(r"^(你好|您好|hi|hello)[，,：:]?", t, re.I) and 2 < len(t) <= 16:
            try:
                el.click(timeout=2000)
                return f"点问候语「{t[:14]}」"
            except Exception:
                try:
                    el.hover(timeout=1500)
                    return f"悬停问候语「{t[:14]}」"
                except Exception:
                    pass
            break
    # ② 头像/账号类元素（class/id 含 avatar/user/account/profile；跳过文本长的大容器）
    for sel in ("header [class*=avatar i]", "nav [class*=avatar i]",
                "header [class*=user i]", "nav [class*=user i]",
                "header [id*=avatar i]", "header [class*=account i]",
                "header [class*=profile i]"):
        try:
            cands = page.query_selector_all(sel)
        except Exception:
            continue
        for el in cands[:6]:
            try:
                if not el.is_visible():
                    continue
                if len((el.inner_text() or "").strip()) > 20:
                    continue                    # 大容器不点，防止误触
                el.click(timeout=2000)
                return f"点头像/账号区（{sel}）"
            except Exception:
                try:
                    el.hover(timeout=1500)
                    return f"悬停头像/账号区（{sel}）"
                except Exception:
                    continue
    return None


def try_auto_navigate(ctx, page, nav_texts, max_rounds=3, is_logged_out_fn=None,
                      aliases=None):
    """登录后主动找「我的投递/申请记录」版块，再判状态（不许只在首页瞄一眼就交差）。
    每轮：先判当前页是否已是投递记录页；第 0 轮先点账号区/个人中心；之后模糊扫描
    整页 a/button（配置的 nav_texts 精确词优先，其次按入口打分，逐候选尝试）；最多 3 层跳转。
    登录判定哲学：主动点击取证——点完账号区/投递入口后若被拦到登录页，
    才算"未登录"铁证（info["login_wall"]=True）；没有证据就当已登录继续找。
    已登录+已投递 ⇒ 状态一定在站内某处：正文同时出现公司名+状态词也算到达。
    返回 (page, info)，info 记录 landed_url / nav_ok / login_wall / 每轮轨迹。"""
    info = {"nav_ok": False, "login_wall": False, "landed_url": page.url, "steps": []}
    aliases = aliases or []

    def _hit_login_wall(p):
        """点击后被要求登录 → 未登录铁证"""
        try:
            return bool(is_logged_out_fn and is_logged_out_fn(p))
        except Exception:
            return False

    def _judge(p, act):
        try:
            u = p.url
            try:
                b = p.inner_text("body", timeout=3000)
            except Exception:
                b = ""
            ok, why = score_apply_page(u, b)
        except Exception:
            ok, why, u = False, "页面读取失败", p.url
        info["steps"].append({"act": act, "url": u, "ok": ok, "why": why})
        if ok:
            info["nav_ok"] = True
        return ok

    def _status_visible(p):
        """正文里同时出现状态词+公司名 → 这就是状态页，直接收工"""
        try:
            b = p.inner_text("body", timeout=3000) or ""
        except Exception:
            return False
        has_company = any(a.lower() in b.lower() for a in aliases if a)
        return has_company and any(w in b for w in _APPLY_STATE_WORDS)

    for rnd in range(max_rounds):
        if _judge(page, "start"):
            break
        if _status_visible(page):           # 当前页已能看到状态，不再乱点
            info["nav_ok"] = True
            info["steps"].append({"act": "状态可见", "url": page.url,
                                  "why": "正文含公司名+状态词"})
            break
        before = page.url
        moved = False
        # 第 0 轮：先试「个人中心」文字，再点"登录按钮原来的位置"（头像/你好，xxx），
        # 很多官网登录后点这里才弹出「我的投递/应聘记录」菜单
        if rnd == 0:
            _click_first_text(page, ["个人中心", "我的主页", "用户中心"])
            act = _click_account_area(page)
            if act:
                try:
                    page.wait_for_timeout(900)   # 等下拉菜单/新页面展开
                except Exception:
                    pass
                info["steps"].append({"act": act, "url": page.url})
            if _wait_for_progress(page, before, 4000):
                moved = True
            page = _maybe_new_page(ctx, page)
            if _hit_login_wall(page):               # 点账号区后被要求登录 → 铁证
                info["login_wall"] = True
                info["steps"].append({"act": "点账号区后被要求登录", "url": page.url})
                break
            if moved and _judge(page, "点账号区/个人中心"):
                break
        # ① 配置的精确词优先（保留旧行为，config.json nav_texts）
        clicked = False
        for t in (nav_texts or []):
            if _click_first_text(page, [t]):
                clicked = True
                info["steps"].append({"act": f"配置入口「{t}」", "url": page.url})
                break
        # ② 模糊扫描整页链接/按钮，按"像投递记录入口"打分，多候选逐个点过去
        # （已登录+已投递 ⇒ 状态一定在站内某处，多试几个入口比一次押注更稳）
        if not clicked:
            cands = _collect_nav_candidates(page)
            for s, txt, href, el in cands[:3]:
                try:
                    el.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    el.click(timeout=3000)
                except Exception:
                    continue
                clicked = True
                info["steps"].append({"act": f"模糊点击「{txt}」",
                                      "href": href[:120], "score": s})
                if _wait_for_progress(page, before, 6000):
                    moved = True
                    break
                # 点了没跳转：可能点错了/是个无效入口，回到原页继续试下一个
                info["steps"].append({"act": f"「{txt}」无跳转，换下一个候选"})
                page = _maybe_new_page(ctx, page)
        if clicked and _wait_for_progress(page, before, 2000):
            moved = True
        page = _maybe_new_page(ctx, page)
        if _status_visible(page):           # 点完就看到状态，直接收工
            info["nav_ok"] = True
            info["steps"].append({"act": "状态可见", "url": page.url,
                                  "why": "正文含公司名+状态词"})
            break
        if moved and _hit_login_wall(page):         # 点投递入口后被要求登录 → 铁证
            info["login_wall"] = True
            info["steps"].append({"act": "点投递入口后被要求登录", "url": page.url})
            break
        if _judge(page, "找投递入口"):
            break
        if not moved and page.url == before:
            info["steps"].append({"act": "stop", "why": "点击后无跳转，停止深挖"})
            break
    info["landed_url"] = page.url
    return page, info


# ---- 投递记录页识别（纯函数 + DOM 扫描，selftest 可直接测）----
# 各公司叫法千差万别，关键词尽量扩：凡是"我的/个人/账号"系 + "投递/申请/应聘/职位/
# 进度/记录/流程/状态/消息/通知"系都可能是入口。已登录+已投递 ⇒ 状态一定在站内某处。
NAV_TEXT_KEYWORDS = (
    "我的投递", "投递记录", "投递进度", "投递管理", "我的申请", "申请记录",
    "申请进度", "应聘记录", "我的应聘", "应聘进度", "应聘管理", "投递箱",
    "招聘进度", "我的流程", "我的职位", "职位管理", "我的岗位", "已投职位",
    "已投岗位", "我的报名", "报名记录", "我的校招", "校招进度", "流程查询",
    "进度查询", "我的求职", "求职中心", "我的消息", "消息中心", "通知中心",
    "历史投递", "全部投递", "当前投递", "进行中的流程", "我的offer", "offer管理",
    "个人中心", "我的主页", "用户中心", "账号中心", "我的账户", "个人资料",
    "我的简历", "简历管理", "简历中心",
)
NAV_URL_HINTS = ("apply", "applied", "application", "deliver", "delivery",
                 "record", "candidate", "resume", "portal", "my-position",
                 "myposition", "usercenter", "personal", "mine", "my", "user",
                 "center", "ucenter", "space", "console", "dashboard", "home",
                 "history", "progress", "status", "member", "account", "profile",
                 "message", "notice", "notification")
NAV_NEG_WORDS = ("退出", "注销", "登出", "切换账号", "设置", "意见反馈",
                 "帮助中心", "客服", "关于我们", "首页", "登录", "注册",
                 "隐私", "协议", "条款", "英文", "EN", "公众号", "小程序")
# 注意：不能把 "home" 算投递路径——美团投递后台首页就是 /web/home（纯营销页），
# 腾讯/京东等也常用 home 做落地页，会把首页误判成投递记录页
_APPLY_URL_RE = re.compile(
    r"(apply|applied|applications?|deliver(?:y)?|records?|candidate|my-?apply|my-?position|"
    r"delivery|progress|mydeliver|myresume|resume)", re.I)
# 明确是"首页/落地页"的 URL（命中直接判负，防止状态词凑分误判）
_HOME_URL_RE = re.compile(r"(?:^|/)(?:web/)?(?:home|index(?:\.html?)?)(?:[?#].*)?$", re.I)
_APPLY_HEAD_RE = re.compile(
    r"(我的投递|投递记录|投递进度|我的申请|申请记录|申请进度|应聘记录|我的应聘|投递管理|投递箱|招聘进度|我的流程)")
_APPLY_STATE_WORDS = ("筛选", "待处理", "笔试", "面试", "测评", "评估", "offer",
                      "录用", "已读", "待查看", "环节", "流程", "已完成", "已结束",
                      "已拒绝", "未通过", "简历", "邀约", "初筛", "复筛")


def score_apply_page(url, body):
    """最终落地 URL + 正文 → 是不是「我的投递/申请记录」页。
    必须同时有投递/申请语境，防止官网首页"投递简历"按钮误判。返回 (bool, 原因)。"""
    body = body or ""
    # 首页/落地页直接判负（美团 /web/home 就是营销首页，状态词也可能凑够分）
    path_only = re.sub(r"^https?://[^/]+", "", url or "")
    if _HOME_URL_RE.search(path_only or "/"):
        return False, f"网址是首页/落地页（{url}），不是投递记录页"
    score, why = 0, []
    if _APPLY_URL_RE.search(url or ""):
        score += 3
        why.append("网址含投递/申请路径")
    m = _APPLY_HEAD_RE.search(body)
    if m:
        score += 3
        why.append(f"标题「{m.group(0)}」")
    hits = [w for w in _APPLY_STATE_WORDS if w in body]
    if len(hits) >= 4:
        score += 2
        why.append(f"状态词×{len(hits)}")
    elif len(hits) >= 2 and ("投递" in body[:800] or "申请" in body[:800]):
        score += 1
        why.append("页首有投递语境")
    has_ctx = (("投递" in body or "申请" in body or "应聘" in body)
               or bool(_APPLY_URL_RE.search(url or "")))
    if score >= 3 and has_ctx:
        return True, "、".join(why)
    return False, ("、".join(why) + "（但不够像投递页）" if why else "未发现投递页特征")


def _collect_nav_candidates(page):
    """扫描当前页所有可见 a/button/菜单项，按"像投递记录入口"打分，降序返回。"""
    try:
        els = page.query_selector_all(
            "a[href], button, [role='menuitem'], [role='link'],"
            " li[class*='menu'], div[class*='menu-item'], span[class*='menu']")
    except Exception:
        return []
    cands, seen = [], set()
    for el in els[:500]:
        try:
            if not el.is_visible():
                continue
            txt = re.sub(r"\s+", " ", el.inner_text() or "").strip()[:40]
            href = (el.get_attribute("href") or "").strip()
        except Exception:
            continue
        if not txt and not href:
            continue
        key = (txt[:24], href[:80])
        if key in seen:
            continue
        seen.add(key)
        if any(n in txt for n in NAV_NEG_WORDS):
            continue
        s = 0
        for kw in NAV_TEXT_KEYWORDS:
            if kw in txt:
                s += 10
                break
        tl = txt.lower()
        if re.match(r"^(我的|my\b|personal|account)", tl):   # "我的xx" 多半是入口
            s += 5
        if any(k in txt for k in ("记录", "进度", "状态", "历史", "中心", "流程")):
            s += 3
        hl = href.lower()
        if hl and not hl.startswith(("javascript", "#", "mailto:", "tel:")) \
                and any(k in hl for k in NAV_URL_HINTS):
            s += 7
        if 0 < len(txt) <= 10 and any(k in txt for k in ("投递", "申请", "应聘", "进度")):
            s += 4
        if s:
            cands.append((s, txt, href, el))
    cands.sort(key=lambda x: -x[0])
    return cands


def patrol_company(pw, target, cfg, headed):
    """巡检一家公司，返回该公司的结果 dict。任何异常都不会中断整体流程。"""
    from playwright.sync_api import sync_playwright  # noqa: F401 (pw 已传入)
    company = target["company"]
    aliases = target.get("aliases") or [company]
    result = {
        "company": company,
        "note": target.get("note", ""),
        "status_code": "未识别",
        "keyword": "",
        "evidence": "",
        "checked_at": NOW(),
        "needs_review": False,
        "nav_ok": False,        # 是否真的走到了「我的投递/申请记录」版块
        "landed_url": "",       # 导航后最终到达的网址（留证，报告里展示）
    }
    url = (target.get("login_url") or "").strip()
    if not url:
        result.update(status_code="非官网渠道",
                      status_raw="这家没有官网后台（如 BOSS直聘/牛客/内推），自动跳过",
                      needs_review=False)
        return result

    profile_dir = PROFILE_ROOT / re.sub(r'[\\/:*?"<>|]', "_", company)
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not headed,
            user_agent=NORMAL_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 768},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
    except Exception as e:
        msg = str(e)
        if "Target page, context or browser has been closed" in msg or "user data directory" in msg.lower():
            # 登录窗口还开着，profile 被独占，巡检拿不到登录态
            result.update(status_code="浏览器占用",
                          status_raw=(f"{company} 的登录窗口还开着，登录态目录被占用，巡检起不了浏览器。"
                                      f"请关掉那个窗口再巡检（以后登录成功会自动关窗）。"),
                          needs_review=True)
        else:
            result.update(status_code="访问失败", status_raw=msg[:200], needs_review=True)
        return result
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    lg = _login_mod()
    apply_url = (target.get("apply_url") or "").strip()
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        # 有"投递记录直达页"：先唤醒会话，再直达
        if apply_url:
            try:
                page.goto(apply_url, timeout=45000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
            except Exception:
                pass
        page, nav_info = try_auto_navigate(
            ctx, page, cfg.get("nav_texts") or [],
            is_logged_out_fn=(lg.is_logged_out if lg else None),
            aliases=aliases)
        result["nav_ok"] = nav_info["nav_ok"]
        result["landed_url"] = nav_info["landed_url"]
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
        page_url = page.url
        try:
            body = page.inner_text("body", timeout=10000)
        except Exception:
            body = re.sub(r"<[^>]+>", " ", page.content())

        # ★ 未登录只认铁证（老板定的哲学：没有证据就是已登录）：
        #   ① 导航点击后被拦到登录页（login_wall）
        #   ② 最终页面上存在明确的"登录/扫码登录"元素
        # 不许再凭"页面上有'请登录'三个字"这种文本包含误判。
        logged_out = bool(nav_info.get("login_wall"))
        if not logged_out:
            try:
                if lg is not None:
                    logged_out = lg.is_logged_out(page)
            except Exception:
                logged_out = False

        nav_tip = ("已进入「我的投递/申请记录」版块，"
                   if result["nav_ok"] else
                   "没找到「我的投递/申请记录」版块（可能藏在深层菜单，或这个网址不是招聘后台），")
        if logged_out:
            result.update(
                status_code="未登录",
                status_raw=(f"点击导航时被要求登录（铁证），点按钮重新登录"
                            f"（上次登录时间：{last_login_at(company) or '无'}）。地址：{page_url}"),
                needs_review=True)
        elif EMPTY_RECORD_RE.search(body):
            # 老板定的哲学：已登录+翻遍入口，页面明确说"暂无投递记录"才算"证明没投"
            result.update(
                status_code="未找到投递记录",
                status_raw=(f"已登录并点遍可能的入口，页面明确显示没有投递记录"
                            f"（证明这家公司没投过/记录已被清空）。最终地址：{page_url}"),
                needs_review=True)
        elif not any(a.lower() in body.lower() for a in aliases):
            result.update(status_code="未识别",
                          status_raw=f"{nav_tip}页面上也没找到「{company}」的状态信息。"
                                     f"怎么办：点这家后面的「官网直达」自己看一眼投递状态，"
                                     f"再在台账里手动改。最终地址：{page_url}",
                          needs_review=True)
        else:
            other_aliases = []
            for t2 in cfg.get("targets", []):
                if t2.get("company") != company:
                    other_aliases.extend(t2.get("aliases") or [t2.get("company") or ""])
            code, kw, snippet = find_status(body, aliases, other_aliases)
            if code:
                result.update(status_code=code, keyword=kw, evidence=snippet)
            else:
                result.update(status_code="未识别",
                              status_raw=f"{nav_tip}找到公司名但没识别出状态词。最终地址：{page_url}",
                              needs_review=True)
        if result["needs_review"]:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%m%d_%H%M")
            safe = re.sub(r'[\\/:*?"<>|]', "_", company)
            try:
                page.screenshot(path=str(SCREENSHOT_DIR / f"{safe}_{stamp}.png"),
                                full_page=False)
            except Exception:
                pass
            (CAPTURE_DIR / f"{safe}_{stamp}.txt").write_text(
                f"URL: {page_url}\n\n{body[:6000]}", encoding="utf-8")
    except Exception as e:
        result.update(status_code="访问失败", status_raw=str(e)[:200], needs_review=True)
    finally:
        ctx.close()
    return result


def _login_mod():
    """复用 login.py 的元素级登录检测（避免两套判定不一致）"""
    try:
        if str(BASE) not in sys.path:
            sys.path.insert(0, str(BASE))
        import login as _lg
        return _lg
    except Exception:
        return None


LOGIN_STATE_PATH = BASE / "login_state.json"
LOGIN_VALID_DAYS = 7


def read_login_state():
    try:
        if LOGIN_STATE_PATH.exists():
            d = json.loads(LOGIN_STATE_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def saved_login_companies():
    """最近 7 天内检测到登录成功（login_state.json 里 ok=true）的公司名集合。"""
    saved = set()
    now = datetime.now()
    for co, v in read_login_state().items():
        if not isinstance(v, dict) or not v.get("ok"):
            continue
        try:
            at = datetime.strptime(str(v.get("at", ""))[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if (now - at).days <= LOGIN_VALID_DAYS:
            saved.add(co)
    return saved


def last_login_at(company):
    v = read_login_state().get(company) or {}
    return str(v.get("at") or "")


def sync_to_tracker(results):
    """把巡检结果同步进追踪表 html 内嵌数据。

    与邮箱扫描共用同一套"按状态变化"同步逻辑（dashboard.merge）：
    读 patrol/patrol_last_seen.json 做差分，"未识别/访问失败"不算有效观察。
    返回 {"synced", "changed", "added", "changes", "conflicts", "error"}
    """
    tracker_html = ROOT / "秋招投递追踪表.html"
    if not tracker_html.exists():
        return {"synced": 0, "changed": 0, "added": 0, "changes": [], "conflicts": [],
                "error": "找不到追踪表文件"}
    text = tracker_html.read_text(encoding="utf-8")
    m = re.search(
        r'<script type="application/json" id="embeddedData">\s*(\{.*?\})\s*</script>',
        text, re.S)
    if not m:
        return {"synced": 0, "changed": 0, "added": 0, "changes": [], "conflicts": [],
                "error": "追踪表里找不到内嵌数据块"}
    data = json.loads(m.group(1))

    # 复用 dashboard 的同步逻辑（保证邮箱 / 巡检两套完全一致）
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        import dashboard as dash
    except Exception as e:
        return {"synced": 0, "changed": 0, "added": 0, "changes": [], "conflicts": [],
                "error": f"无法加载同步模块：{e}"}

    snap = dash.read_snapshot(dash.SNAPSHOT_PATROL)
    records = data.get("records", [])

    # 巡检只知道公司名（不知道岗位）：该公司名下所有记录都算被观察到，逐条走同步规则
    incoming = []
    for pr in results:
        code = pr.get("status_code")
        if code in ("未识别", "访问失败", "未登录", "非官网渠道", "", None):
            continue                       # 不算有效观察
        tgt = STATUS_TO_TRACKER.get(code)
        co = str(pr.get("company") or "").strip()
        if not co:
            continue
        ev = (pr.get("evidence") or "")[:50]
        note = ("官网巡检: " + ev) if ev else ""
        hits = [r for r in records
                if r.get("company") and dash.same_company(r.get("company"), co)]
        if hits:
            for r in hits:
                item = {"company": r.get("company"), "job": r.get("job") or "",
                        "status": tgt or code, "date": pr.get("checked_at", "")[:10],
                        "note": note}
                if not tgt:
                    item["_unmapped_stage"] = code     # 官网新词 → 走三层归类
                incoming.append(item)
        else:
            item = {"company": co, "job": "", "status": tgt or code,
                    "date": pr.get("checked_at", "")[:10], "note": note}
            if not tgt:
                item["_unmapped_stage"] = code
            incoming.append(item)

    # 官网出现但映射表没有的新词 → 三层归类（auto_status_map → 一批一次AI → 自动建状态）
    sts = data.get("statuses") or []
    new_statuses = []
    try:
        new_statuses = dash.resolve_unmapped_stages(incoming, sts)
    except Exception:
        new_statuses = []
    data["statuses"] = sts

    stats = {}
    merged, added, updated, conflicts = dash.merge(
        records, incoming, channel="patrol", snapshot=snap,
        statuses=sts, stats=stats)
    data["records"] = merged
    try:
        data["records"], _mc = dash.dedupe_records(data["records"], sts)
    except Exception:
        pass
    data["version"] = int(time.time() * 1000)
    new_text = text[:m.start(1)] + json.dumps(data, ensure_ascii=False) + text[m.end(1):]
    tracker_html.write_text(new_text, encoding="utf-8")
    dash.write_snapshot(dash.SNAPSHOT_PATROL, snap)

    changes = [f"{u['company']}: {u['from']} → {u['to']}" for u in stats.get("updates", [])]
    return {"synced": updated, "changed": stats.get("changed", 0), "added": added,
            "changes": changes, "conflicts": conflicts,
            "new_statuses": new_statuses, "error": None}


def run_cycle(cfg, force, only, headed):
    from playwright.sync_api import sync_playwright
    targets = [t for t in cfg["targets"] if t.get("enabled", True)]
    if only:
        keys = [k.strip() for k in only.split(",") if k.strip()]
        targets = [t for t in targets if any(k in t["company"] for k in keys)]
    if not targets:
        log("没有匹配到任何启用中的巡检目标，请检查 config.json / --companies 参数。")
        return False

    # 登录态预检：一家都没登录过时，不允许开跑——无头访问 31 个登录页
    # 没有任何意义，还会产生无意义请求。先跑 login.bat 保存登录态再来。
    saved = saved_login_companies()
    ready, skipped_no_login, skipped_non_official = [], [], []
    for t in targets:
        if not (t.get("login_url") or "").strip():
            skipped_non_official.append(t)          # 非官网渠道（BOSS直聘/牛客/内推等）
        elif t["company"] in saved:
            ready.append(t)
        else:
            skipped_no_login.append(t)              # 有官网但没登录 / 登录过期
    if skipped_non_official:
        names = "、".join(t["company"] for t in skipped_non_official)
        log(f"以下 {len(skipped_non_official)} 家是非官网渠道（没有官网后台），自动跳过：{names}")
    if skipped_no_login:
        names = "、".join(t["company"] for t in skipped_no_login)
        log(f"以下 {len(skipped_no_login)} 家需要重新登录，本轮跳过：{names}")
        log("（点网页上的「打开浏览器登录」，或在 patrol 目录跑：python login.py 公司名）")
    if not ready:
        log("本轮没有可以巡检的公司（都要先登录，或都是非官网渠道）。")
        return False
    targets = ready

    log(f"开始巡检 {len(targets)} 家公司（间隔保护：≥{cfg.get('min_interval_hours', 4)}h，"
        f"公司间随机等待 {cfg.get('page_delay_seconds', [8, 18])} 秒）")
    delay = cfg.get("page_delay_seconds", [8, 18])
    results = []
    with sync_playwright() as pw:
        for i, t in enumerate(targets, 1):
            log(f"({i}/{len(targets)}) 正在检查：{t['company']}")
            try:
                r = patrol_company(pw, t, cfg, headed)
            except Exception as e:
                r = {"company": t["company"], "note": t.get("note", ""),
                     "status_code": "访问失败", "keyword": "",
                     "evidence": "", "status_raw": str(e)[:200],
                     "checked_at": NOW(), "needs_review": True,
                     "nav_ok": False, "landed_url": ""}
            tag = r["status_code"] + ("（需人工核对）" if r["needs_review"] else "")
            log(f"    → {tag}" + (f" ｜ {r.get('evidence', '')[:60]}" if r.get("evidence") else ""))
            results.append(r)
            if i < len(targets):
                time.sleep(random.uniform(*delay))

    payload = {
        "generated_at": NOW(),
        "min_interval_hours": cfg.get("min_interval_hours", 4),
        "next_allowed_at": plan_next_run(cfg),
        "results": results,
    }
    write_json_atomic(OUTPUT_PATH, payload)
    write_json_atomic(STATE_PATH, {
        "last_run": payload["generated_at"],
        "next_allowed": payload["next_allowed_at"],
    })
    review = sum(1 for r in results if r["needs_review"])
    # 同步进追踪表 + 生成巡检报告（页面状态栏用）
    sync = sync_to_tracker(results)
    report = {
        "time": payload["generated_at"],
        "total": len(results),
        "identified": sum(1 for r in results if not r["needs_review"]),
        "review": review,
        "changed": sync.get("changed", 0),
        "added": sync.get("added", 0),
        "synced": sync["synced"],
        "changes": sync["changes"],
        "conflicts": sync.get("conflicts", []),
        "needs_login": [t["company"] for t in skipped_no_login],
        "skipped_non_official": [t["company"] for t in skipped_non_official],
        "items": [{"company": r["company"], "status": r["status_code"],
                   "review": r["needs_review"],
                   "nav_ok": r.get("nav_ok", False),
                   "landed_url": r.get("landed_url", "")} for r in results],
    }
    write_json_atomic(BASE / "patrol_report.json", report)
    log(f"巡检完成：{len(results)} 家，其中 {review} 家需要人工核对（截图存 patrol/screenshots/）。")
    log(f"同步进追踪表：{sync['synced']} 条状态更新 {sync['changes']}")
    log(f"结果已写入 {OUTPUT_PATH}")
    log(f"下次可巡检：{payload['next_allowed_at']}（看板会同步显示）")
    return True


def selftest():
    # 状态名必须与看板状态栏一致（老板定的：不许造"通过"这种没人懂的名字）
    cases = [
        ("尊敬的候选人：恭喜您通过筛选，我们向您发放offer", "Offer"),
        ("很遗憾，您未能通过本环节的筛选", "已挂"),
        ("您的流程已终止，感谢您的关注", "已挂"),
        ("请参加AI面试，链接如下", "AI面试"),
        ("恭喜进入面试环节，请准备一面", "面试"),
        ("笔试通知：请按时参加在线考试", "笔试"),
        ("请尽快完成在线测评", "测评"),
        ("简历已投递，进入简历筛选阶段", "简历筛选中"),
        ("本公司简介：我们是一家好公司", None),
    ]
    ok = True
    for text, expect in cases:
        code, kw = classify_window(text)
        flag = "PASS" if code == expect else "FAIL"
        if code != expect:
            ok = False
        print(f"[{flag}] {text[:26]}… → {code}（命中：{kw}）")
    page_text = ("腾讯 校招个人中心\n  产品运营  已投递\n\n"
                 "美团 校招个人中心\n  运营岗  请完成在线测评\n\n"
                 "京东 校招个人中心\n  京东已完成笔试，请等待面试通知")
    all_cos = ["腾讯", "美团", "京东"]
    for co, expect in [("腾讯", "简历筛选中"), ("美团", "测评"), ("京东", "面试")]:
        others = [c for c in all_cos if c != co]
        code, kw, snip = find_status(page_text, [co], others)
        flag = "PASS" if code == expect else "FAIL"
        if code != expect:
            ok = False
        print(f"[{flag}] 多公司混排提取 {co} → {code}（命中：{kw}）")
    # 腾讯 progress.html 结构化字段优先（防流程图例误命中）
    tx_text = "腾讯\n是否发起面试：否\n集体面试 初试 复试 HR面试 Offer 录用评估中\n"
    code, kw, snip = find_status(tx_text, ["腾讯"])
    flag = "PASS" if code == "简历筛选中" else "FAIL"
    if code != "简历筛选中": ok = False
    print(f"[{flag}] 腾讯结构化字段 → {code}（命中：{kw}）")
    # R6：登录后是否真的走到了「我的投递/申请记录」版块
    nav_cases = [
        ("https://join.qq.com/apply/record",
         "我的投递记录\n产品运营-校招｜简历筛选中\n笔试｜面试｜测评流程", True),
        ("https://campus.meituan.com/usercenter",
         "美团校招 个人中心\n我的投递\n商家BD（广州）当前流程：面试 已完成笔试", True),
        ("https://www.lkcoffee.com/about",
         "瑞幸咖啡官网｜关于我们｜加入我们，立即投递简历，了解企业新闻", False),
        ("https://example-corp.com/",
         "欢迎登录｜首页 产品介绍\n投递简历请先注册账号", False),
        ("https://x.com/settings", "退出登录 注销账号 设置 帮助中心", False),
        # 美团真实回归：/web/home 是营销首页（曾误判为投递页），直达记录页必须判正
        ("https://zhaopin.meituan.com/web/home",
         "首页 LongCat人才招聘 社会招聘 校园招聘 美团赛事 了解美团 "
         "面向所有校园人才，提供多种招聘项目，都能找到合适岗位 "
         "美团的社招招聘流程是什么 一起成长，一起更好", False),
        ("https://zhaopin.meituan.com/web/personal-center/delivery-record",
         "我的简历 投递记录 账号设置 2026年（2027届）秋季校招 进行中 "
         "志愿一：AI产品经理岗 应届 投递时间：2026/09/09 笔试 志愿状态说明", True),
    ]
    for u, b, expect in nav_cases:
        got, why = score_apply_page(u, b)
        flag = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"[{flag}] 投递页判定 {u.split('//')[1][:28]} → {got}（{why}）")
    print("自测" + ("全部通过 ✅" if ok else "存在失败项 ❌"))
    sys.exit(0 if ok else 1)


def main():
    args = sys.argv[1:]
    loop = "--loop" in args
    force = "--force" in args
    headed = "--headed" in args
    only = None
    if "--companies" in args:
        only = args[args.index("--companies") + 1]
    if "--selftest" in args:
        selftest()
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    if loop:
        log(f"常驻巡检模式启动：每 ≥{cfg.get('min_interval_hours', 4)} 小时自动巡检一轮"
            f"（+0~{cfg.get('jitter_minutes', 20)} 分钟随机抖动）。关闭本窗口即停止。")
        while True:
            wait_until_allowed(cfg)
            if not run_cycle(cfg, force=False, only=only, headed=headed):
                # 没有登录态等不可自动恢复的情况：退出，等用户处理后再启动
                break
    else:
        if not guard(cfg, force):
            sys.exit(2)
        run_cycle(cfg, force, only, headed)


if __name__ == "__main__":
    main()
