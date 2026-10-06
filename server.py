# -*- coding: utf-8 -*-
"""
server.py — 追踪表本地小服务（AI添加 功能专用）
================================================
用法：双击运行 或 python server.py
然后浏览器打开：http://localhost:8788
页面上的「AI添加」按钮就能用了：粘网址+岗位，其余自动识别。

关闭：在这个黑窗口按 Ctrl+C
"""
import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dashboard import (HTML_FILE, load_embedded, merge, csv_to_records, norm_company,
                       read_snapshot, write_snapshot, SNAPSHOT_MAIL, SNAPSHOT_PATROL,
                       same_company, dedupe_records, resolve_unmapped_stages)
from add_job import fetch_page_text, extract_job_info
import main as mail_main

# 端口：支持命令行传入（启动追踪表.bat 会自动挑一个空闲端口），默认 8788
# 注意：8788 是分发版独立端口，与原版 job_track 用的 8768 彻底隔离
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
SCAN_LOG = mail_main.SCAN_LOG
LOGINS_FILE = Path(__file__).resolve().parent / "patrol" / "logins.json"   # 各公司登录信息（只存本机，不进交付模板）


def same_co(a, b):
    """两家是不是同一家（与 dashboard.same_company 完全一致，前端也按同一套规则归一）。
    子公司不会被误合："网易" vs "网易互娱" → 不是同一家 ✗"""
    return same_company(a, b)

def url_key(u):
    """网址归一：只保留 域名+路径，丢掉 ?query 和 #hash。
    招聘链接里的 share_token / 跟踪参数会变，不能整串比。"""
    u = (u or "").strip()
    for cut in ("?", "#"):
        u = u.split(cut)[0]
    return u.rstrip("/").lower()

def _read_logins():
    """读各公司登录信息（账号/密码/手机号）。只存本机。"""
    try:
        if LOGINS_FILE.exists():
            d = json.loads(LOGINS_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


def _write_logins(rows):
    LOGINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOGINS_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # 避免 HTTP/1.0 下 POST body 读取和响应关闭的 2 秒延迟
    def log_message(self, *args):  # 静音默认日志
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")  # 仅本机；保存接口另有 Origin 校验防线
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")  # 发完即关连接，避免 keep-alive 延迟
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_OPTIONS(self):
        # 处理跨域预检（file:// 双击打开时浏览器会先发 OPTIONS）
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/logins":
            self._send_json({"ok": True, "items": _read_logins()})
            return
        if self.path == "/api/scan_status":
            # 返回上次扫描简报（没有则 null）
            if SCAN_LOG.exists():
                try:
                    self._send_json({"ok": True, "log": json.loads(
                        SCAN_LOG.read_text(encoding="utf-8"))})
                    return
                except Exception:
                    pass
            self._send_json({"ok": True, "log": None})
            return
        if self.path == "/api/patrol_status":
            # 返回上次巡检报告（report=简报，status=逐家详情）
            base = Path(__file__).parent / "patrol"
            rep, st = None, None
            try:
                f1 = base / "patrol_report.json"
                if f1.exists():
                    rep = json.loads(f1.read_text(encoding="utf-8"))
            except Exception:
                rep = None
            try:
                f2 = base / "patrol_status.json"
                if f2.exists():
                    st = json.loads(f2.read_text(encoding="utf-8"))
            except Exception:
                st = None
            self._send_json({"ok": True, "report": rep, "status": st})
            return
        if self.path == "/api/settings":
            self._handle_settings_get()
            return
        if self.path.split("?")[0] == "/api/patrol_targets":
            self._handle_patrol_targets_get()
            return
        if self.path in ("/", "/index.html"):
            body = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def _handle_save(self):
        """浏览器每次新增/编辑/删除后，把全量记录写回 html 内嵌数据块。
        合并策略：浏览器数据为主，但保留它不知道的 id（防止覆盖脚本刚同步的记录）。
        隔离防线：只接受本服务页面（http://127.0.0.1:*）的保存；
        拒绝 file:// 直接打开页面的写回（Origin: null），防止其他追踪表副本/旧页面把个人数据写进本模板。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            origin = (self.headers.get("Origin") or "").strip().lower()
            if origin.startswith("null"):
                self._send_json(
                    {"ok": False,
                     "error": "拒绝 file:// 页面的写回：请用「启动追踪表.bat」启动后，访问弹出的网页使用（数据才能存进文件）"},
                    403)
                return
            if origin and not (origin.startswith("http://127.0.0.1")
                               or origin.startswith("http://localhost")):
                self._send_json({"ok": False, "error": "拒绝来自非本服务页面的写入"}, 403)
                return

            data = json.loads(self.rfile.read(length).decode("utf-8"))
            html_text = HTML_FILE.read_text(encoding="utf-8")
            embedded, match = load_embedded(html_text)

            incoming = data.get("records", [])
            incoming_ids = {r.get("id") for r in incoming}
            # 前端传回的"已删除 id"名单：这些记录永远不再恢复（修复"删了又复活"）
            deleted_ids = set(data.get("deletedIds") or [])
            incoming_ids |= deleted_ids
            # 保留浏览器不知道的（脚本刚加进去的）记录，但被删除的除外
            kept = [r for r in embedded["records"]
                    if r.get("id") not in incoming_ids and r.get("id") not in deleted_ids]
            embedded["records"] = incoming + kept

            # 保存时更新版本号：这样任何打开的页面都能看出"文件比我的缓存新" → 以文件为准
            embedded["version"] = int(time.time() * 1000)
            if data.get("statuses"):
                embedded["statuses"] = data["statuses"]
            # 存量重复公司去重（防止前端把重复公司又写回来）
            try:
                embedded["records"], _mc = dedupe_records(
                    embedded["records"], embedded.get("statuses"))
            except Exception:
                pass
            # 去重可能以另一个 id 恢复已删公司 → 再滤一次删除名单
            if deleted_ids:
                embedded["records"] = [r for r in embedded["records"]
                                       if r.get("id") not in deleted_ids]

            new_html = (html_text[:match.start(1)]
                        + json.dumps(embedded, ensure_ascii=False)
                        + html_text[match.end(1):])
            HTML_FILE.write_text(new_html, encoding="utf-8")
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_scan(self):
        """一键扫描邮箱：调 mail_main.run_scan()，跑完后自动同步进 html"""
        # 配置检查：没填邮箱/专用密码时给友好提示，而不是抛 Python 内部报错
        mail_ok = bool((getattr(mail_main, "GMAIL_EMAIL", None) or "").strip()
                       and (getattr(mail_main, "GMAIL_APP_PASSWORD", None) or "").strip())
        if not mail_ok:
            self._send_json({"ok": False, "error": "还没有配置邮箱，请先点右上角「设置」，填邮箱地址和应用专用密码/授权码后再扫描（生成教程在设置弹窗里）"})
            return
        try:
            report = mail_main.run_scan()
            # 同步进追踪表（按"状态变化"同步，快照保护手动修改）
            try:
                html_text = HTML_FILE.read_text(encoding="utf-8")
                embedded, match = load_embedded(html_text)
                csv_records = csv_to_records()
                statuses = embedded.get("statuses") or []
                # ② 未知状态词：AI 归类 + 自动建状态（单入口，逻辑在 dashboard.resolve_unmapped_stages）
                new_statuses = resolve_unmapped_stages(csv_records, statuses)
                snap = read_snapshot(SNAPSHOT_MAIL)          # 上次观察到的状态
                _stats = {}
                merged, added, updated, conflicts = merge(
                    embedded["records"], csv_records, channel="mail",
                    snapshot=snap, statuses=statuses, stats=_stats)
                # ③ 存量重复公司去重
                merged, merged_companies = dedupe_records(merged, statuses)
                embedded["records"] = merged
                embedded["statuses"] = statuses
                # 报告：本轮新变化 / 已同步 / 冲突 / 自动新建状态 / 合并公司
                report["changed"] = _stats.get("changed", 0)
                report["added"] = added
                report["synced"] = updated
                report["conflicts"] = conflicts
                report["updates"] = _stats.get("updates", [])
                report["new_statuses"] = new_statuses
                report["merged_companies"] = merged_companies
                # 报告里补上"表里最终的状态"（key 用归一后的公司名，匹配更稳）
                _final = {}
                for _r in embedded["records"]:
                    _lb = ""
                    for _s in statuses:
                        if _s.get("key") == _r.get("status"):
                            _lb = _s.get("label") or ""
                            break
                    _final[norm_company(_r.get("company"))] = _lb or _r.get("status")
                for _it in (report.get("items") or []):
                    _k = norm_company(_it.get("company"))
                    if _k in _final:
                        _it["final_status"] = _final[_k]
                embedded["version"] = int(time.time() * 1000)
                new_html = (html_text[:match.start(1)]
                            + json.dumps(embedded, ensure_ascii=False)
                            + html_text[match.end(1):])
                HTML_FILE.write_text(new_html, encoding="utf-8")
                write_snapshot(SNAPSHOT_MAIL, snap)          # 写回快照
                try:                                          # 把统计一起写回扫描简报（刷新页面也能看到）
                    SCAN_LOG.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
            except Exception:
                pass  # 同步失败不影响返回简报
            self._send_json({"ok": True, "report": report})
        except BaseException as e:   # BaseException：未配置邮箱/模型时 sys.exit 也要转成 JSON 错误
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_patrol(self):
        """一键官网巡检：调 patrol.run_cycle（带4小时间隔保护），自动同步进追踪表"""
        import sys
        sys.path.insert(0, str(Path(__file__).parent / "patrol"))
        import patrol as patrol_mod
        try:
            if not patrol_mod.CONFIG_PATH.exists():
                self._send_json({"ok": False,
                                 "error": "还没有配置巡检公司：请点上面的「登录信息」按钮，添加要巡检的公司（公司名、登录网址、账号密码），保存后即可开始官网巡检"}, 400)
                return
            cfg = json.loads(patrol_mod.CONFIG_PATH.read_text(encoding="utf-8"))
            if not patrol_mod.guard(cfg, force=False):
                self._send_json({"ok": False,
                                 "error": "距上次巡检不足4小时，请稍后再点（保护账号防风控）"}, 429)
                return
            ok = patrol_mod.run_cycle(cfg, force=False, only=None, headed=False)
            if not ok:
                self._send_json({"ok": False,
                                 "error": "没有可巡检的公司（还没登录过），先跑 patrol/login.py"}, 400)
                return
            rp = patrol_mod.BASE / "patrol_report.json"
            report = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
            self._send_json({"ok": True, "report": report})
        except BaseException as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ═══ 巡检公司清单管理（读写 patrol/config.json）═══
    def _patrol_config_path(self):
        return Path(__file__).resolve().parent / "patrol" / "config.json"

    # ═══ 页面「设置」：邮箱 + AI 模型配置（写入 .env，全部只存本机）═══
    def _read_settings(self):
        """读 .env 里的配置（脱敏返回：密码 / key 只告诉"有没有"）"""
        try:
            from dotenv import dotenv_values
            vals = dotenv_values(Path(__file__).resolve().parent / ".env")
        except Exception:
            vals = {}
        return {
            "ok": True,
            "gmail_email": vals.get("GMAIL_EMAIL", ""),
            "gmail_provider": vals.get("GMAIL_PROVIDER", ""),
            "gmail_configured": bool((vals.get("GMAIL_EMAIL") or "").strip()
                                     and (vals.get("GMAIL_APP_PASSWORD") or "").strip()),
            "imap_host": vals.get("GMAIL_IMAP_HOST", "") or "imap.gmail.com",
            "model_provider": vals.get("MODEL_PROVIDER", ""),
            "model_base_url": vals.get("MODEL_BASE_URL", "") or "https://api.deepseek.com",
            "model_name": vals.get("MODEL_NAME", "") or "deepseek-chat",
            "model_key_configured": bool((vals.get("MODEL_API_KEY") or vals.get("DEEPSEEK_API_KEY") or "").strip()),
        }

    def _handle_settings_get(self):
        self._send_json(self._read_settings())

    def _handle_settings_post(self):
        """页面「设置」保存：写 .env，并立即刷新当前进程里的配置（不用重启服务）"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            env_path = Path(__file__).resolve().parent / ".env"
            try:
                from dotenv import dotenv_values
                cur = dotenv_values(env_path)
            except Exception:
                cur = {}
            # 邮箱 / 接口 / 模型名：空值 = 清除；密码 / API Key：空值 = 保持不变（避免误清）
            _secret_keys = {"GMAIL_APP_PASSWORD", "MODEL_API_KEY"}
            updates = {
                "GMAIL_EMAIL": str(data.get("gmail_email") or "").strip(),
                "GMAIL_APP_PASSWORD": str(data.get("gmail_app_password") or "").strip(),
                "GMAIL_IMAP_HOST": str(data.get("imap_host") or "").strip(),
                "GMAIL_PROVIDER": str(data.get("gmail_provider") or "").strip(),
                "MODEL_API_KEY": str(data.get("model_api_key") or "").strip(),
                "MODEL_BASE_URL": str(data.get("model_base_url") or "").strip(),
                "MODEL_NAME": str(data.get("model_name") or "").strip(),
                "MODEL_PROVIDER": str(data.get("model_provider") or "").strip(),
            }
            for k, v in updates.items():
                if v:
                    cur[k] = v
                elif k in _secret_keys:
                    pass
                else:
                    cur.pop(k, None)
            lines = []
            for k in ("GMAIL_EMAIL", "GMAIL_APP_PASSWORD", "GMAIL_IMAP_HOST", "GMAIL_PROVIDER",
                      "MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME", "MODEL_PROVIDER"):
                if cur.get(k):
                    lines.append(f"{k}={cur[k]}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # 立即刷新当前进程的配置：页面改完直接能用，不用重启服务
            try:
                from dotenv import load_dotenv
                load_dotenv(env_path, override=True)
            except Exception:
                pass
            mail_main.GMAIL_EMAIL = cur.get("GMAIL_EMAIL") or None
            mail_main.GMAIL_APP_PASSWORD = cur.get("GMAIL_APP_PASSWORD") or None
            mail_main.DEEPSEEK_API_KEY = cur.get("MODEL_API_KEY") or cur.get("DEEPSEEK_API_KEY") or ""
            mail_main.MODEL_BASE_URL = (cur.get("MODEL_BASE_URL") or "").strip() or "https://api.deepseek.com"
            mail_main.MODEL_NAME = (cur.get("MODEL_NAME") or "").strip() or "deepseek-chat"
            mail_main.IMAP_HOST = (cur.get("GMAIL_IMAP_HOST") or "").strip() or "imap.gmail.com"
            os.environ["GMAIL_EMAIL"] = mail_main.GMAIL_EMAIL or ""
            os.environ["GMAIL_APP_PASSWORD"] = mail_main.GMAIL_APP_PASSWORD or ""
            os.environ["MODEL_API_KEY"] = mail_main.DEEPSEEK_API_KEY
            os.environ["MODEL_BASE_URL"] = mail_main.MODEL_BASE_URL
            os.environ["MODEL_NAME"] = mail_main.MODEL_NAME
            os.environ["GMAIL_IMAP_HOST"] = mail_main.IMAP_HOST
            self._send_json(self._read_settings())
        except BaseException as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_patrol_targets_get(self):
        """GET /api/patrol_targets → {ok, targets, saved_logins}"""
        try:
            _p = self._patrol_config_path()
            cfg = json.loads(_p.read_text(encoding="utf-8")) if _p.exists() else {}
            saved = []
            try:
                import sys as _sys
                root = str(Path(__file__).resolve().parent / "patrol")
                if root not in _sys.path:
                    _sys.path.insert(0, root)
                import patrol as _pm
                saved = sorted(_pm.saved_login_companies())
            except Exception:
                pass
            self._send_json({"ok": True, "targets": cfg.get("targets") or [],
                             "saved_logins": saved})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_patrol_targets_post(self):
        """POST /api/patrol_targets body:{company,login_url,aliases,note,enabled,dismissed}
        按公司归一去重：重名就更新网址/别名/备注；enabled=false 即停用；
        dismissed=true 表示"永不提示加入巡检"（前端候选列表不再出现，可恢复）。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            company = str(data.get("company") or "").strip()
            if not company:
                self._send_json({"ok": False, "error": "公司名不能为空"}, 400)
                return
            p = self._patrol_config_path()
            cfg = {}
            if p.exists():  # 新用户第一次使用：config.json 还不存在，从空配置开始
                cfg = json.loads(p.read_text(encoding="utf-8"))
            targets = cfg.get("targets") or []
            aliases = data.get("aliases") or ""
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.replace("，", ",").split(",") if a.strip()]
            enabled = bool(data.get("enabled", True))
            dismissed = bool(data.get("dismissed", False))
            key = norm_company(company)
            hit = None
            for t in targets:
                if norm_company(str(t.get("company") or "")) == key:
                    hit = t
                    break
            if hit is None:
                targets.append({
                    "company": company,
                    "aliases": aliases or [company],
                    "login_url": str(data.get("login_url") or "").strip(),
                    "enabled": enabled,
                    "dismissed": dismissed,
                    "note": str(data.get("note") or "").strip(),
                })
            else:
                if data.get("login_url") is not None:
                    hit["login_url"] = str(data.get("login_url") or "").strip()
                if aliases:
                    hit["aliases"] = aliases
                if data.get("note") is not None:
                    hit["note"] = str(data.get("note") or "").strip()
                hit["enabled"] = enabled
                hit["dismissed"] = dismissed
            cfg["targets"] = targets
            p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send_json({"ok": True, "targets": targets})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        if self.path == "/api/login_browser":
            # 弹出真浏览器让用户登录（自动填好账号密码，验证码用户自己输）
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                company = str(data.get("company") or "").strip()
                if not company:
                    self._send_json({"ok": False, "error": "没拿到公司名"}, 400)
                    return
                import subprocess
                base = Path(__file__).resolve().parent
                flags = 0x00000008 if os.name == "nt" else 0   # DETACHED_PROCESS：独立窗口
                # 登录进程输出写日志：独立进程闪退（如 profile 被锁）时无控制台，
                # 不留日志就只看到窗口一闪而过，问题无从排查
                logf = open(base / "patrol" / "login_last.log", "ab")
                subprocess.Popen(
                    ["python", "-u", str(base / "patrol" / "login.py"), company, "--auto"],
                    cwd=str(base / "patrol"),
                    stdout=logf, stderr=subprocess.STDOUT,
                    creationflags=flags)
                self._send_json({"ok": True, "msg": "浏览器已打开"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return
        if self.path == "/api/logins":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                rows = data.get("items")
                if isinstance(rows, list):
                    _write_logins([r for r in rows if isinstance(r, dict) and str(r.get("company", "")).strip()])
                self._send_json({"ok": True, "items": _read_logins()})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return
        if self.path == "/api/settings":
            self._handle_settings_post()
            return
        if self.path == "/api/save":
            self._handle_save()
            return
        if self.path == "/api/scan":
            self._handle_scan()
            return
        if self.path == "/api/patrol":
            self._handle_patrol()
            return
        if self.path.split("?")[0] == "/api/patrol_targets":
            self._handle_patrol_targets_post()
            return
        if self.path != "/api/add_job":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            url = (data.get("url") or "").strip()
            job = (data.get("job") or "").strip()
            referral = (data.get("referral") or "").strip()
            if not url.startswith("http"):
                self._send_json({"ok": False, "error": "网址要以 http 开头"}, 400)
                return

            # ★ 快速预览通道：只从网址本地猜公司名（不调浏览器、不调 AI、不写表，毫秒级返回）
            if data.get("stage") == "quick":
                known = ""
                try:
                    from add_job import quick_company_from_url, guess_company_from_url
                    known = quick_company_from_url(url)
                    if not known:
                        known = guess_company_from_url(url)
                except Exception:
                    pass
                self._send_json({"ok": True, "quick": True,
                                 "preview": {"company": known, "job": job}, "title": ""})
                return

            # 1) 抓网页（失败不致命，用网址本身让AI猜）
            try:
                page_text = fetch_page_text(url)
                if len(page_text) < 12:   # 有页面标题就算有内容
                    page_text = ""  # 内容太少等于没抓到（JS动态页）
            except Exception:
                page_text = ""

            # 2) DeepSeek 提取（抓不到网页也能凭域名+岗位名推断）
            try:
                info = extract_job_info(url, page_text, job)
            except Exception as e:
                self._send_json({"ok": False, "error": f"AI识别失败：{e}"}, 500)
                return
            company = info.get("company", "")
            if not company:
                self._send_json({"ok": False,
                                 "error": "没识别出公司名，请改用「新增」手动填写"}, 400)
                return

            # 3) 先看这家公司是不是已经在看板里了（按网址 → 按公司名）
            html_text = HTML_FILE.read_text(encoding="utf-8")
            embedded, match = load_embedded(html_text)
            # 备注：手动填的内推码 + AI 识别到的（内推码优先放前面）
            note = info.get("note", "") or ""
            if referral:
                ref_txt = f"内推码 {referral}"
                note = f"{ref_txt}；{note}" if note else ref_txt
            new_job = job or info.get("job", "") or ""
            existing = None
            for rec in embedded["records"]:                      # ① 网址一模一样 → 铁定同一家
                if url and (rec.get("url") or "").strip() and url_key(rec["url"]) == url_key(url):
                    existing = rec
                    break
            if existing is None:                                  # ② 公司名归一后相同，且岗位不冲突
                for rec in embedded["records"]:
                    if not same_co(rec.get("company"), company):
                        continue
                    rjob = (rec.get("job") or "").strip()
                    if rjob == new_job or not rjob or not new_job:
                        existing = rec
                        break
            if existing is not None:
                # 已经在看板 → 不新增；只把空着的字段补上，然后告诉用户当前状态
                filled = []
                if new_job and not (existing.get("job") or "").strip():
                    existing["job"] = new_job; filled.append("岗位")
                if info.get("location") and not (existing.get("location") or "").strip():
                    existing["location"] = info["location"]; filled.append("地点")
                if url and not (existing.get("url") or "").strip():
                    existing["url"] = url; filled.append("链接")
                if note and note not in (existing.get("note") or ""):
                    existing["note"] = ((existing.get("note") or "") + ("；" if existing.get("note") else "") + note)
                    filled.append("备注")
                if filled:
                    embedded["version"] = int(time.time() * 1000)
                    new_html = (html_text[:match.start(1)]
                                + json.dumps(embedded, ensure_ascii=False)
                                + html_text[match.end(1):])
                    HTML_FILE.write_text(new_html, encoding="utf-8")
                st = existing.get("status", "")
                print(f"[AI添加] {existing.get('company')} 已在看板（{st}）" + ("，补了：" + "、".join(filled) if filled else ""))
                self._send_json({"ok": True, "action": "已存在", "record": existing,
                                 "status": st, "filled": filled})
                return

            # 4) 确实是新公司 → 写进追踪表

            record = {
                "id": 0,
                "company": company,
                "job": job or info.get("job", ""),
                "location": info.get("location", ""),
                "date": time.strftime("%Y-%m-%d"),
                "status": "applied",
                "channel": info.get("channel", "官网网申"),
                "url": url,
                "note": note,
                "receiveDate": "",
                "dueDate": "",
                "remindDays": 1,
            }
            merged, added, updated, _conflicts = merge(embedded["records"], [record])
            embedded["records"] = merged
            embedded["version"] = int(time.time() * 1000)
            new_html = (html_text[:match.start(1)]
                        + json.dumps(embedded, ensure_ascii=False)
                        + html_text[match.end(1):])
            HTML_FILE.write_text(new_html, encoding="utf-8")

            print(f"[AI添加] {company} | {record['job']} | "
                  f"{'新增' if added else '更新'}")
            self._send_json({"ok": True, "record": record,
                             "action": "新增" if added else "更新"})
        except BaseException as e:
            self._send_json({"ok": False, "error": str(e)}, 500)


if __name__ == "__main__":
    print("=" * 44)
    print("  秋招投递追踪表已启动")
    print(f"  请打开浏览器访问：http://localhost:{PORT}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 44)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError as e:
        print(f"\n[错误] 端口 {PORT} 无法使用：{e}")
        print("请关闭占用该端口的程序后重试（或双击「启动追踪表.bat」自动换端口）。")
        input("按回车退出…")