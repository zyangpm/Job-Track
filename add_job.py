# -*- coding: utf-8 -*-
"""
add_job.py — 粘贴招聘链接，AI 自动填进追踪表
=============================================
用法：
  python add_job.py https://某公司的校招岗位网址
  python add_job.py            # 不带网址，运行后粘贴也行

流程：抓取网页 → DeepSeek 提取 公司/岗位/地点 等 → 自动合并进 秋招投递追踪表.html
注意：需要 .env 里的 DEEPSEEK_API_KEY 且账户有余额
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import main as _main
from dashboard import load_embedded, merge, HTML_FILE

BASE_DIR = Path(__file__).resolve().parent

# 页面文本缓存：同 URL 60 秒内复用，避免 quick + 正式两次请求重复开浏览器
_PAGE_CACHE = {}
_PAGE_CACHE_TTL = 60

# 招聘系统 URL 里常见的公司标识 → 中文名（本地秒猜，不用开浏览器/AI）
_URL_COMPANY_MAP = {
    "bytedance": "字节跳动", "tencent": "腾讯", "alibaba": "阿里巴巴",
    "meituan": "美团", "jd": "京东", "huawei": "华为", "xiaomi": "小米",
    "baidu": "百度", "nio": "蔚来", "miniso": "名创优品", "hellobike": "哈啰",
    "talkweb": "拓维信息", "alibaba": "阿里巴巴", "pdd": "拼多多",
    "baidu": "百度", "kwai": "快手", "bilibili": "哔哩哔哩", "netease": "网易",
    "xiaohongshu": "小红书", "didi": "滴滴", "liangxin": "良信",
    "catherine": "凯淳股份", "tencent": "腾讯", "zs": "中泰证券",
    "zhaolian": "中联", "ocean": "奥克斯", "tcl": "TCL",
    "pingan": "中国平安", "jd-jds": "京东",
}


def _decode_html(raw, charset):
    """编码兜底：先按网页声明的编码解；中文乱码就试 utf-8 / gb18030（很多老站是 GBK）"""
    for cs in ([charset] if charset else []) + ["utf-8", "gb18030"]:
        try:
            return raw.decode(cs, errors="strict")
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


# ── 浏览器实例复用：只启动一次，后面的识别直接用它（省掉每次 3-5 秒的启动）──
_PW = None
_BROWSER = None


def _get_browser():
    """拿一个常驻的无头浏览器（第一次慢，之后都很快）"""
    global _PW, _BROWSER
    try:
        if _BROWSER is not None and _BROWSER.is_connected():
            return _BROWSER
    except Exception:
        _BROWSER = None
    from playwright.sync_api import sync_playwright
    _PW = sync_playwright().start()
    _BROWSER = _PW.chromium.launch(headless=True)
    return _BROWSER


def _url_key(u):

    """网址归一：只留域名+路径，忽略 ?query 和 #hash 和末尾斜杠"""

    u = (u or "").strip().lower()

    for cut in ("?", "#"):

        u = u.split(cut)[0]

    return u.rstrip("/")





def quick_company_from_url(url):

    """秒出通道：这个网址老板投过吗？表里有就直接返回公司名（不调 AI）。

    匹配规则：域名+路径完全相同（忽略 share_token 之类会变的部分）。"""

    key = _url_key(url)

    if not key:

        return ""

    try:

        embedded, _ = load_embedded(HTML_FILE.read_text(encoding="utf-8"))

        for r in embedded.get("records", []):

            u = (r.get("url") or "").strip()

            if u and _url_key(u) == key:

                return (r.get("company") or "").strip()

    except Exception:

        pass

    return ""


def guess_company_from_url(url):
    """纯本地从 URL 猜公司名（不开浏览器、不调 AI，毫秒级）。
    规则：招聘系统域名（mokahr/feishu/zhiye）里的子域名或 /campus-recruitment/ 后那段英文，
    查常见映射表；猜不到返回空串，让后面的浏览器+AI 兜底。"""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        path = (u.path or "").lower()
        # 1) app.mokahr.com/campus-recruitment/talkweb/...
        m = re.search(r"/campus-recruitment/([a-z0-9_-]+)", path)
        if m and m.group(1) in _URL_COMPANY_MAP:
            return _URL_COMPANY_MAP[m.group(1)]
        # 2) xxx.jobs.feishu.cn / xxx.zhiye.com / xxx.hotjob.cn
        for plat in ("jobs.feishu.cn", "zhiye.com", "hotjob.cn", "mokahr.com", "nowcoder.com"):
            if host.endswith(plat):
                sub = host.split(".")[0]
                if sub and sub not in ("app", "www", "campus", "m", "mokahr", "feishu", "zhiye", "hotjob", "nowcoder"):
                    if sub in _URL_COMPANY_MAP:
                        return _URL_COMPANY_MAP[sub]
        # 3) careers.bytedance.com / campus.tencent.com
        m2 = re.match(r"(?:careers?|campus|hr|jobs?)\.([a-z0-9-]+)\.", host)
        if m2 and m2.group(1) in _URL_COMPANY_MAP:
            return _URL_COMPANY_MAP[m2.group(1)]
    except Exception:
        pass
    return ""



def _fetch_with_browser(url, timeout_ms=20000):
    """用常驻无头浏览器打开页面，等 JS 渲染完再取内容。
    招聘网站多是动态渲染，静态抓是空壳；浏览器能拿到真正的岗位信息。
    失败就返回空串，外面会退回静态抓取。"""
    try:
        browser = _get_browser()
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120 Safari/537.36"))
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            title = ""
            body_txt = ""
            # 轮询：标题够具体或正文有内容就马上取（最多 8×250ms = 2 秒，一变就走）
            for _ in range(8):
                try:
                    title = page.title() or ""
                except Exception:
                    title = ""
                try:
                    body_txt = page.inner_text("body") or ""
                except Exception:
                    body_txt = ""
                if len(re.sub(r"\s+", "", body_txt)) > 200:
                    break
                if len(re.sub(r"\s+", "", title)) >= 6:     # 标题比"招聘官网"更具体了（含公司名）
                    break
                page.wait_for_timeout(250)
            # 内容可能在 iframe 里（不少招聘站是这么套的），一并读出来
            try:
                for _f in page.frames[1:]:
                    try:
                        _ft = _f.inner_text("body") or ""
                        if len(re.sub(r"\s+", "", _ft)) > len(re.sub(r"\s+", "", body_txt)):
                            body_txt = _ft
                    except Exception:
                        pass
            except Exception:
                pass
        finally:
            try:
                page.close()                      # 只关页面，浏览器留着下次用
            except Exception:
                pass
        parts = []
        if title.strip():
            parts.append("【页面标题】" + re.sub(r"\s+", " ", title).strip())
        body_txt = re.sub(r"\s+", " ", body_txt).strip()
        if body_txt:
            parts.append("【正文】" + body_txt[:2600])   # 只留关键部分，AI 处理更快
        return "\n".join(parts)
    except Exception:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120 Safari/537.36"))
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(2800)          # 给 JS 一点渲染时间
            try:
                title = page.title() or ""
            except Exception:
                title = ""
            try:
                body_txt = page.inner_text("body") or ""
            except Exception:
                body_txt = ""
            browser.close()
        parts = []
        if title.strip():
            parts.append("【页面标题】" + re.sub(r"\s+", " ", title).strip())
        body_txt = re.sub(r"\s+", " ", body_txt).strip()
        if body_txt:
            parts.append("【正文】" + body_txt[:5000])
        return "\n".join(parts)
    except Exception:
        return ""


def fetch_page_text(url):
    """抓网页 → 优先用无头浏览器（能读 JS 渲染出来的内容），不行再退回静态抓取。
    返回"页面标题 + meta 描述 + 正文"，交给 AI 提取。"""
    # ★ 投过的网址 → 不用抓（extract_job_info 会直接查表秒出），省掉几秒钟
    if quick_company_from_url(url):
        return ""

    # ★ 60 秒内同 URL 直接复用缓存（前端 quick + 正式两次请求不会重复开浏览器）
    now = time.time()
    if url in _PAGE_CACHE:
        ts, cached = _PAGE_CACHE[url]
        if now - ts < _PAGE_CACHE_TTL:
            return cached

    rendered = _fetch_with_browser(url)
    # 只要有内容（哪怕只有个页面标题）就采用 —— 很多招聘站正文是空的，标题里才有公司名
    if rendered and len(rendered.strip()) > 10:
        result = rendered[:6000]
        _PAGE_CACHE[url] = (now, result)
        return result

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            charset = r.headers.get_content_charset()
        html = _decode_html(raw, charset)
    except Exception:
        return ""          # 静态也抓不到（重定向/反爬）→ 返回空，让 AI 凭域名猜

    parts = []
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
    if m and m.group(1).strip():
        parts.append("【页面标题】" + re.sub(r"\s+", " ", m.group(1)).strip())
    for name in ("description", "keywords"):
        mm = re.search(r"<meta[^>]*name=[\"']" + name + r"[\"'][^>]*content=[\"']([^\"']*)[\"']", html, re.I)
        if mm and mm.group(1).strip():
            parts.append("【meta " + name + "】" + mm.group(1).strip())
    for prop in ("og:title", "og:description", "og:site_name"):
        mm = re.search(r"<meta[^>]*property=[\"']" + prop + r"[\"'][^>]*content=[\"']([^\"']*)[\"']", html, re.I)
        if mm and mm.group(1).strip():
            parts.append("【" + prop + "】" + mm.group(1).strip())
    body = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    if body:
        parts.append("【正文】" + body)
    result = "\n".join(parts)[:6000]
    _PAGE_CACHE[url] = (now, result)
    return result


def extract_job_info(url, page_text, job_hint=""):
    """让 DeepSeek 提取结构化字段。page_text 为空时仅凭网址+用户给的岗位名推断。"""
    # ★ 秒出通道：这个网址投过 → 直接查表返回公司名，不调 AI（毫秒级）
    known = quick_company_from_url(url)
    if known:
        return {"company": known, "job": job_hint or "", "location": "",
                "channel": "官网网申", "note": ""}

    # ★ URL 本地规则猜中常见公司（mokahr/feishu/zhiye 子域名）→ 也不调 AI
    guess = guess_company_from_url(url)
    if guess and not page_text:
        return {"company": guess, "job": job_hint or "", "location": "",
                "channel": "官网网申", "note": ""}

    if page_text:
        body = f"网页内容：\n{page_text}"
        rule = "【严格要求】信息不足就填空字符串\"\"，绝对禁止编造。"
    else:
        body = "（网页抓取失败，没有正文内容）"
        rule = ("【严格要求】网页没抓到内容，请根据网址域名判断公司名"
                "（如 careers.bytedance.com→字节跳动），岗位名优先用用户提供的；"
                "判断不了的字段填空字符串\"\"，绝对禁止编造。")
    prompt = f"""你是招聘信息提取助手。下面是一个校招岗位的网页信息。

【公司名怎么填】
  1) 页面标题里如果有公司名（例如"中泰证券招聘官网"→公司就是「中泰证券」），直接用，把"招聘官网/校园招聘/官网"这类尾巴去掉
  2) 网址里如果能看出公司（子域名或路径里的英文），结合标题判断
  3) 实在判断不出来才填空字符串，绝不编造

【岗位名怎么填】
  1) 是"岗位详情页"（就一个岗位）→ 填那个岗位名
  2) 是"职位列表页"（列出一堆岗位）→ 填列表里第一个岗位名
  3) 页面写"职位已停止招聘/不存在"，或者压根没有岗位 → 填空字符串，绝不编造

【最重要的规则：别把"招聘平台"当公司名】
下面这些是第三方招聘系统的域名，它们给很多家公司共用，出现这些域名时，
真公司名必须从 URL 路径 / 页面标题 / 正文里找，绝不能填平台名：
mokahr.com（Moka）、jobs.feishu.cn（飞书招聘）、zhiye.com（北森）、hotjob.cn、
nowcoder.com（牛客）、zhipin.com（BOSS直聘）、51job.com、lagou.com（拉勾）、
liepin.com（猎聘）、shixiseng.com（实习僧）、yingjiesheng.com、kanzhun.com
举例：
  app.mokahr.com/campus-recruitment/talkweb/71921   → 公司是「拓维信息」(talkweb)
  nio.jobs.feishu.cn/campus/...                      → 公司是「蔚来」(nio)
  miniso.zhiye.com/campus/jobs                       → 公司是「名创优品」(miniso)
  hellobike.zhiye.com/...                            → 公司是「哈啰」(hellobike)
URL 里那串英文（子域名或 /campus-recruitment/ 后面的那段）通常就是公司名的拼音/英文。
请提取字段并只返回 JSON（不要多余文字、不要 markdown 代码块）：
{{"company":"公司全称","job":"岗位名称","location":"工作地点","channel":"投递方式（官网网申/牛客网/内推/BOSS直聘/其他 之一）","note":"补充信息（如内推码、薪资、截止时间等，没有就空字符串）"}}

{rule}

网址：{url}
用户提供的岗位名：{job_hint or "（无）"}

{body}"""
    content = _main.call_deepseek(prompt)
    start, end = content.find("{"), content.rfind("}")
    return json.loads(content[start:end + 1])


def main():
    if not _main.DEEPSEEK_API_KEY:
        sys.exit("还没配置 AI 模型：请在页面右上角「设置」里填写模型 API Key（默认 DeepSeek，需账户有余额）")

    url = " ".join(sys.argv[1:]).strip() or input("粘贴招聘链接后回车：\n").strip()
    if not url.startswith("http"):
        sys.exit("链接要以 http 开头")

    print(f"[1/3] 抓取网页…\n      {url}")
    try:
        page_text = fetch_page_text(url)
    except Exception as e:
        sys.exit(f"网页抓取失败：{e}\n（有些招聘页需要登录或是JS动态渲染，抓不到内容）")
    if len(page_text) < 12:   # 有标题就算有内容（HTML 页面可能只有标题）
        sys.exit("抓到的内容太少，这个页面可能是JS动态渲染的，静态抓不到")

    print("[2/3] DeepSeek 提取岗位信息…")
    info = extract_job_info(url, page_text)
    if not info.get("company"):
        sys.exit("AI 没识别出公司名，这个链接可能不是岗位详情页")

    print(f"      识别结果：{info.get('company')} | {info.get('job') or '岗位未知'}"
          f" | {info.get('location') or '地点未知'}")

    print("[3/3] 写入追踪表…")
    html_text = HTML_FILE.read_text(encoding="utf-8")
    embedded, match = load_embedded(html_text)

    record = {
        "id": 0,
        "company": info["company"],
        "job": info.get("job", ""),
        "location": info.get("location", ""),
        "date": __import__("datetime").date.today().isoformat(),
        "status": "applied",
        "channel": info.get("channel", "官网网申"),
        "url": url,
        "note": info.get("note", ""),
        "receiveDate": "",
        "dueDate": "",
        "remindDays": 1,
    }
    merged, added, updated, _conflicts = merge(embedded["records"], [record])
    embedded["records"] = merged

    if added == 0 and updated == 0:
        print("      这家公司+岗位已在追踪表里了，无需添加")
        return

    embedded["version"] = int(__import__("time").time())
    new_html = html_text[:match.start(1)] + json.dumps(embedded, ensure_ascii=False) \
        + html_text[match.end(1):]
    HTML_FILE.write_text(new_html, encoding="utf-8")
    print(f"\n完成！已{'新增' if added else '更新'}到追踪表，刷新浏览器（F5）即可看到")


if __name__ == "__main__":
    main()
