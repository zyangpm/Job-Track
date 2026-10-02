#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性登录态保存工具：弹出真浏览器窗口，你逐个扫码/登录各公司校招官网，
登录成功后会写入 patrol/login_state.json（记录登录时间），登录态（Cookie）保存在
本机 patrol/.secrets/profiles/<公司名>/ 里，之后 patrol.py 就能带着登录态静默巡检。

用法：
  python login.py              # 依次登录 config.json 里所有 enabled 且配置了 login_url 的公司
  python login.py 腾讯 美团    # 只登录指定公司（支持名称片段）
  python login.py 腾讯 --auto  # 弹窗后自动检测登录成功（由网页「打开浏览器登录」按钮调用）
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
PROFILE_ROOT = BASE / ".secrets" / "profiles"
LOGIN_STATE = BASE / "login_state.json"      # 登录态标记：{公司: {ok, at, url}}

# 正向登录信号（出现任一可见元素 → 很可能已登录）
# "你好，xxx" 是腾讯等站点登录后顶栏的问候语（未登录时该位置是"登录"按钮，
# 而"你好，请登录"这类会被 is_logged_out 的"请登录"精确匹配先拦住，不冲突）
POSITIVE_TEXTS = ("退出", "注销", "登出", "个人中心", "我的投递", "我的申请",
                  "我的投递记录", "简历中心", "已认证", "我的简历", "投递记录",
                  "你好，", "你好,")
# 扫码登录弹层关键词
SCAN_LOGIN_TEXTS = ("扫码登录", "微信扫码登录", "请使用微信扫码", "手机扫码登录")


def _load_logins():
    """读登录信息库（各公司账号/密码/手机号）"""
    try:
        f = BASE / "logins.json"
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


def _autofill_login(page, company, quiet=False):
    """按公司名找到登录信息，自动填进页面输入框。可反复调用（等弹窗出现）。
    原则：只填「空框」，绝不覆盖老板手输的内容；
    有账号+密码 → 自动切「密码登录」选项卡填好（免短信验证码）；
    只有手机号 → 切「验证码/短信登录」填手机号并自动点「获取验证码」（只点一次）。"""
    info = None
    for x in _load_logins():
        c = str(x.get("company") or "").strip()
        if c and (c in company or company in c):
            info = x
            break
    if not info:
        return False
    u = str(info.get("user") or "").strip()
    p = str(info.get("password") or "").strip()
    ph = str(info.get("phone") or "").strip()
    if not (u or p or ph):
        return False
    # 每页面一次的旗标（短信只发一次，避免反复轰炸老板手机；选项卡每种只点一次）
    st = getattr(page, "_autofill_state", None)
    if st is None:
        st = {"sms_sent": False, "modal_clicks": 0, "tabs": set()}
        setattr(page, "_autofill_state", st)
    filled = []

    def _scopes():
        # 页面 + 所有 iframe（跨域的也要试，Playwright 能绕过同源限制）
        out = [page]
        try:
            for fr in page.frames:
                if fr is not page.main_frame:
                    out.append(fr)
        except Exception:
            pass
        return out

    def _vis(sels):
        for sc in _scopes():
            for sel in sels:
                try:
                    for el in sc.query_selector_all(sel):
                        try:
                            if el.is_visible():
                                return el
                        except Exception:
                            continue
                except Exception:
                    continue
        return None

    def _fill_empty(el, val, tag):
        try:
            cur = (el.input_value() or "").strip()     # 只填空框
        except Exception:
            cur = ""
        if cur:
            return False
        el.fill(val)
        filled.append(tag)
        return True

    def _click_text(texts, exact=False, once=False):
        for t in texts:
            if once and t in st["tabs"]:        # 同一种选项卡只点一次，防止每3秒反复点
                continue
            for sc in _scopes():                # 登录弹窗可能在 iframe 里（京东就是）
                try:
                    loc = sc.get_by_text(t, exact=exact)
                    for i in range(min(loc.count(), 4)):
                        el = loc.nth(i)
                        if el.is_visible():
                            el.click(timeout=2000)
                            st["tabs"].add(t)
                            return t
                except Exception:
                    continue
        return None

    phone_sels = ["input[type=tel]", "input[name*=phone i]", "input[name*=mobile i]",
                  "input[placeholder*=手机]", "input[id*=phone i]", "input[id*=mobile i]"]
    user_sels = ["input[name*=user i]", "input[name*=account i]", "input[id*=user i]",
                 "input[placeholder*=账号]", "input[placeholder*=邮箱]"]
    pass_sels = ["input[type=password]", "input[name*=pass i]", "input[id*=pass i]"]

    pw_el = _vis(pass_sels)
    ph_el = _vis(phone_sels)
    us_el = _vis(user_sels)

    # ① 有账号+密码 → 优先密码登录（不用等短信）。当前停在短信选项卡也要切过去
    if u and p:
        if not pw_el:
            tab = _click_text(["密码登录", "账号密码登录", "账号登录"], once=True)
            if tab:
                print(f"    ↪ 已切到「{tab}」选项卡")
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                pw_el = _vis(pass_sels)
                us_el = _vis(user_sels)
        if pw_el:
            _fill_empty(pw_el, p, "密码")
            if us_el:
                _fill_empty(us_el, u, "账号")
            elif not ph_el and _vis(["input[type=text]", "input:not([type])"]):
                _fill_empty(_vis(["input[type=text]", "input:not([type])"]), u, "账号")
    # ② 手机号路线：填手机号 + 自动点一次「获取验证码」
    if ph and "密码" not in filled:
        if not ph_el:
            tab = _click_text(["验证码登录", "短信登录", "手机验证码登录",
                               "手机号登录", "手机登录", "免密登录"], once=True)
            if tab:
                print(f"    ↪ 已切到「{tab}」选项卡")
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                ph_el = _vis(phone_sels)
        if ph_el and _fill_empty(ph_el, ph, "手机号") and not st["sms_sent"]:
            btn = _click_text(["获取验证码", "发送验证码", "获取短信验证码"])
            if btn:
                st["sms_sent"] = True
                print(f"    📨 已自动点「{btn}」，短信验证码马上发到你手机，你只输码")
    # ③ 什么框都没见到 → 登录弹窗可能没打开，试点页面上的「登录」按钮打开它（最多2次）
    if not (pw_el or ph_el or us_el) and st["modal_clicks"] < 2:
        if _click_text(["登录", "登录/注册", "立即登录"], exact=False):
            st["modal_clicks"] += 1
    if filled and not quiet:
        print("    🤖 已自动填入：" + "、".join(filled) + "（验证码需要你自己输）")
    return bool(filled)


# ═══ 元素级登录检测 ═══════════════════════════════════════
def _iter_visible_short_texts(page, max_nodes=1500, max_len=14):
    """遍历页面上可见元素，产出 (元素, 去空白文本)（只取短文本，避免整页大段文字误判）"""
    try:
        els = page.query_selector_all("a,button,span,li,div")
    except Exception:
        return
    n = 0
    for el in els:
        n += 1
        if n > max_nodes:
            return
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
        except Exception:
            continue
        if not t or len(t) > max_len:
            continue
        yield el, t


def is_logged_out(page):
    """元素级"未登录"检测：存在可见元素文本精确等于「登录/登 录/Login」这类
    （或长度 ≤3 且含"登录"），或存在扫码登录弹层 → 判为未登录。"""
    for _el, t in _iter_visible_short_texts(page):
        tl = t.lower().replace(" ", "")
        if tl in ("登录", "login", "signin", "sign in", "登录/注册", "立即登录", "请登录"):
            return True
        if len(t) <= 3 and "登录" in t:
            return True
    try:
        body = page.inner_text("body", timeout=3000) or ""
    except Exception:
        body = ""
    if any(h in body for h in SCAN_LOGIN_TEXTS):
        return True
    return False


def has_login_signal(page):
    """正向登录信号：出现"退出/注销/个人中心/我的投递/我的申请"等可见元素。"""
    for _el, t in _iter_visible_short_texts(page):
        for p in POSITIVE_TEXTS:
            if p in t:
                return True
    return False


def _write_login_state(company, ok, url, reason=""):
    """写 patrol/login_state.json"""
    try:
        d = {}
        if LOGIN_STATE.exists():
            try:
                d = json.loads(LOGIN_STATE.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    d = {}
            except Exception:
                d = {}
        d[company] = {
            "ok": bool(ok),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": str(url or ""),
        }
        if reason:
            d[company]["reason"] = reason
        LOGIN_STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"    （写入登录态标记失败：{e}）")


def _page_alive(page):
    """窗口是否还开着"""
    try:
        page.evaluate("1")
        return True
    except Exception:
        return False


def main():
    auto_mode = "--auto" in sys.argv        # --auto：自动检测登录成功，不等按 Enter
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    targets = [t for t in cfg["targets"]
               if t.get("enabled", True) and (t.get("login_url") or "").strip()]
    if len(sys.argv) > 1:
        keys = [k for k in sys.argv[1:] if not k.startswith("-")]
        targets = [t for t in targets if any(k in t["company"] for k in keys)]
    if not targets:
        print("没有匹配到需要登录的公司（这些公司可能没配登录网址，属非官网渠道）。")
        return

    from playwright.sync_api import sync_playwright
    print(f"即将依次登录 {len(targets)} 家公司官网。登录态只保存在本机。")
    with sync_playwright() as pw:
        for i, t in enumerate(targets, 1):
            company, url = t["company"], t["login_url"].strip()
            profile_dir = PROFILE_ROOT / re.sub(r'[\\/:*?"<>|]', "_", company)
            profile_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n({i}/{len(targets)}) {company} → {url}")
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir), headless=False,
                viewport={"width": 1280, "height": 860}, locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"])
            # 抹除自动化指纹（百度等站点检测到 webdriver 会拦成空白页）
            try:
                ctx.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = window.chrome || { runtime: {} };
                """)
            except Exception:
                pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            loaded = False
            for _try in range(3):                # 空白/拦截时自动重载（百度偶发）
                try:
                    page.goto(url, timeout=60000, wait_until="domcontentloaded")
                except Exception:
                    try:
                        page.goto(url, timeout=60000, wait_until="load")
                    except Exception:
                        pass
                page.wait_for_timeout(3000)
                if page.url != "about:blank":
                    loaded = True
                    break
                print(f"    ⚠️ 页面空白，3秒后重载（第{_try+1}次）…")
            if not loaded:
                print("    ❌ 页面始终空白，可能被网站拦截，请手动在窗口里输网址")
            page.wait_for_timeout(1500)          # 等页面渲染完再填
            try:
                _autofill_login(page, company)   # ★ 自动填账号/密码/手机号（验证码你自己输）
            except Exception:
                pass

            logged_ok = False
            if auto_mode:
                print("    浏览器已打开，账号密码会自动填好。请完成登录（验证码自己输），"
                      "登录成功后窗口可自行关闭。")
                ok_streak = 0
                while True:
                    try:
                        page.wait_for_timeout(3000)
                    except Exception:
                        break                     # 窗口关了
                    if not _page_alive(page):
                        break
                    # 登录弹窗可能刚被打开 → 每 3 秒持续找机会补填（只填空框）
                    try:
                        _autofill_login(page, company, quiet=True)
                    except Exception:
                        pass
                    out = is_logged_out(page)
                    pos = has_login_signal(page)
                    ok_streak = ok_streak + 1 if ((not out) and pos) else 0
                    if ok_streak >= 2:
                        logged_ok = True
                        _write_login_state(company, True, page.url)
                        print(f"    ✅ 已检测到 {company} 登录成功，窗口 8 秒后自动关闭")
                        # 自动关窗：窗不关会锁住登录态目录，巡检起不了浏览器
                        for _ in range(4):              # 最多等 8 秒，用户抢先关也行
                            try:
                                page.wait_for_timeout(2000)
                            except Exception:
                                break
                            if not _page_alive(page):
                                break
                        break
                if not logged_ok:
                    # 窗口被提前关闭＝没证据，不覆盖旧状态（也许本来就登录着）。
                    # 登录态真失效的话，巡检点击时被拦登录页自然会报"未登录"。
                    print(f"    ⏭️ {company} 窗口已关闭，未做检测，保留原登录标记不动")
            else:
                try:
                    input("    在浏览器中完成登录后（验证码自己输），按 Enter 保存并继续（Ctrl+C 中止全部）：")
                except KeyboardInterrupt:
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    print("\n已手动中止。")
                    return
                try:
                    ok = (not is_logged_out(page)) and has_login_signal(page)
                    cur_url = page.url
                except Exception:
                    ok, cur_url = False, ""
                _write_login_state(company, ok, cur_url,
                                   reason="" if ok else "没检测到登录成功的标志")
                logged_ok = ok
                print(f"    {'✅ ' + company + ' 登录态已保存' if ok else '⚠️ 未确认到 ' + company + ' 的登录态'}")

            try:
                ctx.close()
            except Exception:
                pass
    print("\n全部处理完毕。之后双击 start_patrol.bat 即可开始低频巡检。")


if __name__ == "__main__":
    main()
