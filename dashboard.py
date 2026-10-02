# -*- coding: utf-8 -*-
"""
dashboard.py — 把邮件台账同步进「秋招投递追踪表.html」
======================================================
运行：python dashboard.py
作用：读取 job_tracker.csv，把邮件识别到的公司/状态合并进追踪表内嵌数据块。
     你在追踪表里手动填的备注、内推码、链接、截止日期都不会被覆盖。
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
CSV_FILE = BASE_DIR / "job_tracker.csv"
HTML_FILE = BASE_DIR / "秋招投递追踪表.html"
AUTO_STATUS_FILE = BASE_DIR / "auto_status_map.json"   # AI/老板确认过的「中文词 -> 状态key」

# ── 台账状态 -> 追踪表状态key ──────────────────────────────
# ⚠️ 覆盖实际出现过的所有中文阶段值（不然映射不到会走到"自动归类"通道）
STATUS_MAP = {
    # 已投递
    "已投递": "applied", "投递成功": "applied", "已申请": "applied", "已提交": "applied",
    # 简历筛选中（→ in_progress，不是 applied！）
    "简历筛选中": "in_progress", "简历筛选": "in_progress", "筛选中": "in_progress",
    "简历评估": "in_progress", "评估中": "in_progress", "审核中": "in_progress",
    "处理中": "in_progress", "待筛选": "in_progress", "简历处理中": "in_progress",
    # 通过初筛
    "通过初筛": "passed_first", "通过筛选": "passed_first", "初筛通过": "passed_first",
    "筛选通过": "passed_first",
    # 测评
    "待笔试/测评": "assess", "待测评": "assess", "测评": "assess", "在线测评": "assess",
    "测评邀请": "assess", "测评通知": "assess", "笔试/测评": "assess",
    "测评通过": "assess_done", "测评完成": "assess_done",
    # 笔试
    "待笔试": "test", "笔试": "test", "笔试邀请": "test", "在线笔试": "test",
    "笔试通过": "test_done", "笔试完成": "test_done",
    # 面试（含各种轮次）
    # 注意："初评/在线初评"不映射——老板要求这类邮件原词独立成状态，走自动建档，不并入面试
    "一面": "interview", "二面": "interview",
    "三面": "interview", "群面": "interview", "复试": "interview", "HR面": "interview",
    "hr面": "interview", "待面试": "interview", "面试": "interview", "终面": "interview",
    "终审": "interview", "约面": "interview", "待沟通": "interview", "沟通中": "interview",
    "面试沟通": "interview", "面试邀请": "interview", "面试通知": "interview",
    "面试完成": "interview_done", "复试通过": "interview_done", "一面通过": "interview_done",
    # AI 面试
    "AI面试": "aiiv", "ai面试": "aiiv", "AI面": "aiiv", "AI面试邀请": "aiiv",
    "AI面试完成": "aiiv_done", "ai面试完成": "aiiv_done",
    # Offer / 录用后环节
    "OC": "offer", "Offer": "offer", "offer": "offer", "录用": "offer", "拟录用": "offer",
    "录用审批": "offer", "offer审批": "offer", "审批中": "offer", "offer沟通": "offer",
    "待发offer": "offer", "签约": "offer", "三方": "offer", "接受offer": "offer",
    "体检": "offer", "背调": "offer", "背景调查": "offer",
    # 人才库
    "进入人才库": "talent", "人才库": "talent", "人才储备": "talent", "备选池": "talent",
    "进入备选": "talent",
    # 挂了
    "已挂": "rejected", "已拒绝": "rejected", "已淘汰": "rejected", "流程终止": "rejected",
    "不匹配": "rejected", "暂不合适": "rejected", "未入选": "rejected", "落选": "rejected",
    "未通过": "rejected",
    # 旧名
    "流程中": "in_progress",
}

# 无效阶段词：只占位 applied，绝不送 AI、绝不自动建状态（否则会建出 label="未知" 的垃圾状态）
BAD_STAGE_WORDS = {
    "", "未知", "不确定", "未识别", "无法识别", "访问失败", "无",
    "none", "null", "n/a", "na", "null", "暂无",
}

# 映射不到的词（外部可读，用于报告）
UNMAPPED = set()

# 公司名归一化别名（左=台账里可能出现的写法，右=追踪表里的写法）
# ⚠️ 前端 秋招投递追踪表.html 里的 COMPANY_ALIAS_JS 是同一张表，改这里要同步改那边
COMPANY_ALIAS = {
    "深圳传音控股股份有限公司": "传音控股",
    "小米集团": "小米",
    # 纽劢科技（NueHCT）现在的公司名是"智驾新程（上海）智能科技有限公司"，统一叫「智驾新程」
    "纽劢科技": "智驾新程",
    "智驾新程（上海）智能科技有限公司": "智驾新程",
    # 同一主体的长短名/中英变体（防止短名每次扫描重生重复卡）
    "合合信息": "上海合合信息科技股份有限公司",
    "阅文集团": "上海阅文信息技术有限公司",
    "掌上先机": "北京掌上先机网络科技有限公司（慧策旺店通）",
    "碧橙数字": "杭州碧橙数字技术股份有限公司",
    "蔚来NIO": "蔚来", "NIO蔚来": "蔚来",
}

# ── 公司名归一化 ──────────────────────────────────────────
# 结尾"公司后缀词 + 行业词"（可循环去掉）
NORM_TAIL = ("股份有限公司", "有限责任公司", "有限公司", "集团", "控股", "公司",
             "信息技术", "技术服务", "电子商务", "文化传媒",
             "科技", "网络", "文化", "传媒", "软件", "电子")
# same_company 判"长短名差异"时额外允许的城市/通用词
_FILLER = NORM_TAIL + (
    "在线", "技术", "信息", "服务", "国际", "中国", "股份", "责任", "有限",
    "北京", "上海", "深圳", "广州", "杭州", "南京", "成都", "武汉", "西安", "苏州",
    "天津", "重庆", "厦门", "长沙", "郑州", "合肥", "青岛", "济南", "大连", "宁波",
    "四川省", "四川", "广东省", "广东", "浙江省", "浙江", "江苏省", "江苏",
    "湖北省", "湖北", "湖南省", "湖南", "山东省", "山东", "福建省", "福建",
    "河南省", "河南", "河北省", "河北", "辽宁省", "辽宁", "陕西省", "陕西",
    "云南省", "云南", "贵州省", "贵州", "广西", "江西省", "江西", "山西省", "山西",
    "黑龙江省", "黑龙江", "吉林省", "吉林", "甘肃省", "甘肃", "海南省", "海南",
)

# 状态推进级别（缺省值给自动状态用）
STATUS_RANK = {"undelivered": 0, "applied": 10, "passed_first": 30,
               "assess": 40, "assess_done": 42, "test": 50, "test_done": 52,
               "aiiv": 55, "aiiv_done": 56, "interview": 60, "interview_done": 62,
               "in_progress": 20, "talent": 85, "rejected": 90, "offer": 100}
DEFAULT_AUTO_RANK = 45          # 自动新建状态默认级别（流程中段）
TERMINAL = {"rejected", "offer", "talent"}   # 老板手动改过这些终态 → 不自动覆盖

# ===== 状态同步：快照 + statusAt/statusBy =====
SNAPSHOT_MAIL = BASE_DIR / "mail_last_seen.json"
SNAPSHOT_PATROL = BASE_DIR / "patrol" / "patrol_last_seen.json"


def norm_job(job):
    """岗位名归一（用于快照键）"""
    return re.sub(r"\s+", "", str(job or "").strip()).lower()


def snapshot_key(company, job):
    """快照键：公司归一 || 岗位名"""
    return norm_company(company) + "||" + norm_job(job)


def read_snapshot(path):
    """读快照文件；不存在/坏了都当空"""
    try:
        p = Path(path)
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def write_snapshot(path, data):
    """写快照文件"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def now_str():
    return time.strftime("%Y-%m-%d %H:%M")


def _strip_brackets(name):
    return re.sub(r"[（(][^）)]*[）)]", "", name or "")


def _strip_tails(name):
    """循环去掉结尾的公司后缀词/行业词"""
    changed = True
    while changed and name:
        changed = False
        for suf in NORM_TAIL:
            if name.endswith(suf) and len(name) > len(suf) + 1:
                name = name[:-len(suf)]
                changed = True
                break
    return name


def norm_company(name):
    """公司名归一化：去别名 → 去括号说明 → 循环去结尾后缀/行业词 → 去空白转小写。
    用于前后端一致的"同一家公司"判定。"""
    name = str(name or "").strip()
    name = COMPANY_ALIAS.get(name, name)
    name = _strip_brackets(name)
    name = _strip_tails(name)
    return name.lower().replace(" ", "")


def _all_filler(s):
    """s 是否全部由"后缀/行业/城市"这类填充词拼成"""
    s = s or ""
    changed = True
    while changed and s:
        changed = False
        for w in _FILLER:
            if s.startswith(w):
                s = s[len(w):]
                changed = True
                break
    return s == ""


def same_company(a, b):
    """两家是不是同一家：
    ① 归一后相同；
    ② 短名是长名前缀、多出来的全是后缀/行业/城市词；
    ③ 短名包含在长名里、前后多出来的全是后缀/行业/城市词（覆盖"北京掌上先机…"这种情况）。
    子公司不会被误合："网易" vs "网易互娱" → 互娱不是填充词 → 不是同一家。
    """
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) < 2:
        return False
    if long_.startswith(short):
        return _all_filler(long_[len(short):])
    idx = long_.find(short)
    if idx >= 0:
        pre, post = long_[:idx], long_[idx + len(short):]
        return _all_filler(pre) and _all_filler(post)
    return False


def clean_company_local(name):
    """扫描同步时的公司名清洗（跟 main.py 那套一致，防止脏数据从 CSV 流进表）"""
    s = str(name or "").replace("\r", " ").replace("\n", " ").strip()
    if not s:
        return ""
    s = re.sub(r"^感谢您(申请|投递|应聘|投递了|已投递|报名|关注)\s*", "", s)
    s = re.sub(r"^(并|已|我|您|请|此|本)?(可以|能|可|要)?(应聘|申请|投递|登录|登陆|报名|关注|查看|收到)\s*", "", s)
    s = re.sub(r"^(恭喜您|您好|尊敬的|亲爱的)[^,，。:：]{0,8}(申请|投递|应聘)?\s*", "", s)
    if re.match(r"^(关于我们|我们是谁|公司简介|企业简介|集团简介|笔试邀请|参加测评方式|宣讲信息|宣讲会|简历投递回复|笔试注意事项|招聘公告|招聘信息|岗位介绍|投递须知|温馨提示|系统通知|此邮件由系统发出|尊敬的候选人)$", s):
        return ""
    s = re.sub(r"(在线|官方招聘|招聘官网|官方网站|官方|招聘系统|校园招聘|校招|招聘|官网|系统)$", "", s).strip()
    if len(s) < 2 or re.match(r"^(您好|谢谢|通知|提醒|邀请|测试|免费|系统|邮件|申请|投递)$", s):
        return ""
    return s


def rank_of(key, statuses=None):
    """取某个状态的推进级别：内置表查不到时读状态对象自带的 rank，再缺省 45"""
    if key in STATUS_RANK:
        return STATUS_RANK[key]
    for s in (statuses or []):
        if isinstance(s, dict) and s.get("key") == key:
            try:
                return int(s.get("rank", DEFAULT_AUTO_RANK))
            except Exception:
                return DEFAULT_AUTO_RANK
    return DEFAULT_AUTO_RANK


# ── 未知状态词：auto_status_map + AI 归类 + 自动建状态 ──────
def read_auto_status_map():
    try:
        if AUTO_STATUS_FILE.exists():
            d = json.loads(AUTO_STATUS_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def write_auto_status_map(d):
    try:
        AUTO_STATUS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def auto_status_key(label):
    """自动状态的 key：auto_ + 该词 md5 前 8 位"""
    return "auto_" + hashlib.md5(str(label).encode("utf-8")).hexdigest()[:8]


_AUTO_PALETTE = ["#8a8175", "#7a8b99", "#9a7b4f", "#6d7f6b", "#8d6e8d", "#a06a5a"]


def _extract_json_obj(txt):
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def resolve_unmapped_stages(records, statuses, use_ai=True):
    """把 records 里的 `_unmapped_stage`（没见过的中文阶段词）归类，三层：
    ① STATUS_MAP / auto_status_map 直接命中；
    ② 一批一次调 DeepSeek，问最接近的内置状态（返回 null 则进第三层）；
    ③ 仍无归属 → 在 statuses 里新建 auto 状态，记录直接归入。
    就地修改 records 的 status / 修改 statuses，返回本次新建的状态列表。
    """
    amap = read_auto_status_map()
    words = []
    for r in records:
        w = str(r.get("_unmapped_stage") or "").strip()
        # 双保险：无效词任何入口都不允许进入归类/建状态流程
        if w and w.lower() not in BAD_STAGE_WORDS and w not in words:
            words.append(w)
    if not words:
        return []

    # 老板要求独立成状态的原词：不许 AI 归并进面试/测评，直接走③自动建档
    # （右值=统一后的状态名，多种写法归到同一个状态）
    FORCE_INDEPENDENT_LABEL = {"在线初评": "在线初评", "线上初评": "在线初评"}

    resolved, rest, force_labels = {}, [], []
    for w in words:
        if w in STATUS_MAP:
            resolved[w] = STATUS_MAP[w]
        elif w in amap:
            resolved[w] = amap[w]
        elif w in FORCE_INDEPENDENT_LABEL:
            label = FORCE_INDEPENDENT_LABEL[w]
            resolved[w] = auto_status_key(label)
            if label not in force_labels:
                force_labels.append(label)
        else:
            rest.append(w)

    amap_changed = False
    # ② AI 归类（一批一次）
    if rest and use_ai:
        label2key = {}
        for s in (statuses or []):
            if isinstance(s, dict) and not s.get("auto"):
                label2key[s.get("label")] = s.get("key")
        try:
            import main as _main
            if getattr(_main, "DEEPSEEK_API_KEY", "") and label2key:
                prompt = (
                    "下面是招聘邮件里出现的、我们没见过的“招聘阶段”中文说法。\n"
                    "请对每一个，从这些内置状态里挑【最接近】的一个（只能挑一个；都不合适就返回 null）：\n"
                    + "、".join(label2key.keys()) + "\n\n"
                    + "\n".join(f"- {w}" for w in rest)
                    + '\n\n只输出 JSON 对象，形如：{"词":"最接近的内置状态名或null"}'
                )
                obj = _extract_json_obj(_main.call_deepseek(prompt))
                for w in list(rest):
                    lab = obj.get(w)
                    if lab and lab in label2key:
                        resolved[w] = label2key[lab]
                        amap[w] = label2key[lab]
                        amap_changed = True
                        rest.remove(w)
        except Exception:
            pass   # AI 失败不阻断

    # ③ 自动建状态（rest = AI 没归类的散词；force_labels = 强制独立词，统一过 label）
    created = []
    used_colors = [s.get("color") for s in (statuses or []) if isinstance(s, dict)]
    for w in rest + force_labels:
        key = auto_status_key(w)
        if not any(isinstance(s, dict) and s.get("key") == key for s in statuses):
            color = next((c for c in _AUTO_PALETTE if c not in used_colors), "#8a8175")
            statuses.append({"key": key, "label": w, "color": color,
                             "builtIn": False, "auto": True, "rank": DEFAULT_AUTO_RANK})
            used_colors.append(color)
            created.append({"word": w, "key": key, "label": w})
        resolved[w] = key

    # 应用到 records
    for r in records:
        w = str(r.get("_unmapped_stage") or "").strip()
        if w and resolved.get(w):
            r["status"] = resolved[w]
        r.pop("_unmapped_stage", None)

    if amap_changed:
        write_auto_status_map(amap)
    return created


def csv_to_records():
    """把 job_tracker.csv 转成追踪表记录格式"""
    if not CSV_FILE.exists():
        sys.exit("找不到 job_tracker.csv，请先运行 python main.py")
    df = pd.read_csv(CSV_FILE, dtype=str, encoding="utf-8-sig").fillna("")

    records = []
    for _, row in df.iterrows():
        company = row["公司全称"].strip()
        if company in ("", "未知"):
            continue
        company = COMPANY_ALIAS.get(company, company)
        company = clean_company_local(company)      # 清洗（脏的丢掉）
        if not company:
            continue
        job = row["岗位名称"].strip()
        if job in ("未知", "校招"):
            job = ""
        stage = row["招聘阶段"].strip()
        status = STATUS_MAP.get(stage)
        unmapped = ""
        if status is None:
            if stage.strip().lower() in BAD_STAGE_WORDS:
                # 无效词（未知/空/不确定/访问失败…）：只占位"已投递"，绝不建垃圾状态
                status = "applied"
            else:
                # 没见过的真阶段词：先占位，稍后由 resolve_unmapped_stages 归类（AI/自动建状态）
                UNMAPPED.add(stage)
                status = "applied"
                unmapped = stage
        date = row["投递日期"].strip()
        if date in ("未知", ""):
            date = ""

        note_parts = []
        interview = row["面试时间"].strip()
        deadline = row["截止时间"].strip() if "截止时间" in df.columns else ""
        due = ""
        due_tm = ""
        if interview and interview != "无":
            note_parts.append(f"面试时间：{interview}")
            due = interview[:10]  # 面试具体场次：作为赴约提醒
        # 笔试/测评/AI面试的完成期限：日历优先用它（含钟点）
        if deadline and deadline != "无":
            note_parts.append(f"截止时间：{deadline}")
            due = deadline[:10]
            if len(deadline) >= 16:
                due_tm = deadline[11:16]
        if row.get("人工复核", "") == "是":
            note_parts.append("⚠️ 信息不足，待人工复核")

        rec = {
            "id": 0,  # 合并时分配
            "company": company,
            "job": job,
            "location": "",
            "date": date,
            "status": status,
            "channel": "其他",   # 邮件扫出来的不知道投递渠道，默认"其他"
            "url": "",
            "note": "；".join(note_parts),
            "receiveDate": date,   # 收到信息时间 = 邮件日期
            "dueDate": due,
            "dueTime": due_tm,
            "remindDays": 1,
        }
        if unmapped:
            rec["_unmapped_stage"] = unmapped
        records.append(rec)
    return records


def _deadline_in_note(rec):
    """从 note 里取"截止时间：YYYY-MM-DD[ HH:mm]"，没有返回 ''"""
    m = re.search(r"截止时间：(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)",
                  str((rec or {}).get("note") or ""))
    return m.group(1).strip() if m else ""


def fill_observation_fields(hit, cv):
    """台账已有这条时，用本轮邮件观察补全时间类字段（状态不变也要补，
    这样新识别出的笔试/AI面试截止时间能跟随进日历；不覆盖已有的手动内容）。"""
    dl = _deadline_in_note(cv)
    if dl and not _deadline_in_note(hit):
        # 邮件明确给出最后期限 → 日历日期挂到截止日（比旧的面试场次日期更该提醒）
        hit["dueDate"] = dl[:10]
        hit["dueTime"] = dl[11:16] if len(dl) >= 16 else ""
    elif cv.get("dueDate") and not hit.get("dueDate"):
        hit["dueDate"] = cv["dueDate"]
        if cv.get("dueTime") and not hit.get("dueTime"):
            hit["dueTime"] = cv["dueTime"]
    if cv.get("receiveDate") and not hit.get("receiveDate"):
        hit["receiveDate"] = cv["receiveDate"]
    if cv.get("date") and (not hit.get("date") or len(str(hit.get("date"))) < 8) \
            and len(str(cv.get("date"))) >= 8:
        hit["date"] = cv["date"]
    if cv.get("note") and cv["note"] not in (hit.get("note") or ""):
        hit["note"] = (hit.get("note", "") + "；" + cv["note"]).strip("；")


def _join_note(a, b):
    """两条 note 去重后用；拼接"""
    parts = []
    for x in (a or "", b or ""):
        for p in str(x).split("；"):
            p = p.strip()
            if p and p not in parts:
                parts.append(p)
    return "；".join(parts)


def _status_score(rec, statuses=None):
    """状态选优打分：(是否手动终态, 推进级别)"""
    st = rec.get("status")
    manual_terminal = 1 if (rec.get("statusBy") == "manual" and st in TERMINAL) else 0
    return (manual_terminal, rank_of(st, statuses))


def _same_group(rep, r):
    """两条记录算不算"同一组重复"：公司相同 且（岗位归一后相等 或 有一方岗位为空）。"""
    if not same_company(rep.get("company", ""), r.get("company", "")):
        return False
    rj = norm_job(rep.get("job"))
    cj = norm_job(r.get("job"))
    return (not rj) or (not cj) or (rj == cj)


def _merge_into(rep, other, statuses=None):
    """把 other 合并进 rep（就地修改 rep）。
    注意：能进到这里的两条，岗位一定是"相等或一方为空"，所以不会出现两个不同非空岗位。"""
    # 公司名：保留最长的正式全称
    if len(str(other.get("company") or "")) > len(str(rep.get("company") or "")):
        rep["company"] = other.get("company")
    # 岗位：非空的优先（这是"补全"不是"合并"，不写"另投岗位"）
    rj = str(rep.get("job") or "").strip()
    oj = str(other.get("job") or "").strip()
    if not rj and oj:
        rep["job"] = oj
    # 状态：取更靠后的；手动终态最优先
    if _status_score(other, statuses) > _status_score(rep, statuses):
        rep["status"] = other.get("status")
        rep["statusAt"] = other.get("statusAt", rep.get("statusAt", ""))
        rep["statusBy"] = other.get("statusBy", rep.get("statusBy", ""))
    # 其他字段：非空优先
    for k in ("date", "receiveDate", "dueDate", "dueTime", "url", "channel", "location"):
        if not str(rep.get(k) or "").strip() and str(other.get(k) or "").strip():
            rep[k] = other.get(k)
    if other.get("dueDate") and len(str(other.get("dueDate"))) > len(str(rep.get("dueDate") or "")):
        rep["dueDate"] = other.get("dueDate")
    # 新邮件识别出"截止时间"（笔试/测评/AI面试的最后期限）→ 日历日期改挂到截止日，
    # 旧的"面试时间"仍保留在 note 里（赴约场次与完成期限不冲突）
    odl = _deadline_in_note(other)
    if odl and not _deadline_in_note(rep):
        rep["dueDate"] = odl[:10]
        rep["dueTime"] = odl[11:16] if len(odl) >= 16 else ""
    # note 去重拼接
    rep["note"] = _join_note(rep.get("note"), other.get("note"))
    # id 取最小
    try:
        if other.get("id") and (not rep.get("id") or other["id"] < rep["id"]):
            rep["id"] = other["id"]
    except Exception:
        pass
    return rep


def dedupe_records(records, statuses=None):
    """把"同一家公司 + 同一个岗位"的重复记录合并成一条。
    同公司但岗位不同的（如传音：用增运营 / AI投放增长专员）保持两条独立记录。
    返回 (新列表, 合并明细[{from:[原名...], into:保留名}])。
    """
    groups = []   # [[rep_dict, [names...]], ...]
    for r in records:
        placed = False
        for g in groups:
            # ★ 用组代表"当前"的公司+岗位判断（rep 的岗位可能刚从空被补上）
            if _same_group(g[0], r):
                _merge_into(g[0], r, statuses)
                g[1].append(str(r.get("company") or ""))
                placed = True
                break
        if not placed:
            groups.append([dict(r), [str(r.get("company") or "")]])

    merged_list = []
    out = []
    for rep, names in groups:
        out.append(rep)
        uniq = []
        for n in names:
            if n and n not in uniq:
                uniq.append(n)
        if len(names) > 1:          # 只要这一组并过（含同名重复），都记进合并清单
            merged_list.append({"from": uniq or names, "into": rep.get("company")})
    return out, merged_list


def load_embedded(html_text):
    """从 HTML 里取出内嵌 JSON 数据块"""
    m = re.search(
        r'<script type="application/json" id="embeddedData">\s*(\{.*?\})\s*</script>',
        html_text, re.S)
    if not m:
        sys.exit("追踪表 HTML 里没找到 embeddedData 数据块，文件可能被改坏了")
    return json.loads(m.group(1)), m


def merge(embedded_records, incoming, channel="mail", snapshot=None, statuses=None, stats=None):
    """按"状态变化"同步（邮箱 / 巡检 两套共用同一逻辑）。

    channel: "mail" 邮箱同步 | "patrol" 巡检同步
    snapshot: dict，本轮快照（会被就地更新，调用方负责写回文件）
    statuses: 状态表 [{key,label}]，用来把 key 转成中文给报告看
    stats: 可选 dict，回填本轮统计 {changed, added, updated, updates, conflicts}

    返回 (records, added, updated, conflicts)

    规则：
      1. 台账没有这家 → 新增，写快照，statusBy=渠道
      2. 快照没有（首次基线）→ 只写快照，不改台账
      3. 本轮观察 == 快照上次观察 → 完全不碰（保护手动修改）
      4. 有变化：
         a. 台账已经等于本轮 → 只更新快照
         b. statusBy=manual 且当前是终态(offer/rejected/talent) → 冲突待确认
         c. 其余 → 写新状态（受只进不退保护；倒退/不同方向的进冲突）
      5. 未识别/访问失败/空 → 不算有效观察，不写台账不写快照
    """
    added = updated = 0
    conflicts = []
    snap = snapshot if snapshot is not None else {}
    now = now_str()
    next_id = max((r.get("id", 0) for r in embedded_records), default=0) + 1
    label_of = {}
    for s in (statuses or []):
        if isinstance(s, dict) and s.get("key"):
            label_of[s["key"]] = s.get("label") or s["key"]
    src_cn = "邮箱" if channel == "mail" else "巡检"
    if stats is None:
        stats = {}
    stats["changed"] = 0
    stats["updates"] = []

    for cv in incoming:
        co = str(cv.get("company") or "").strip()
        job = str(cv.get("job") or "").strip()
        new_status = str(cv.get("status") or "").strip()
        # 5) 无效观察
        if not co or not new_status or new_status in ("未识别", "访问失败", "未知", ""):
            continue
        skey = snapshot_key(co, job)

        hit = None
        for rec in embedded_records:
            if not same_company(rec.get("company", ""), co):
                continue
            # 岗位口径与 dedupe 一致：归一后相等 或 一方为空（防止"用户运营/校招-用户与内容运营"误判为新卡）
            rj, cj = norm_job(rec.get("job") or ""), norm_job(job)
            if not rj or not cj or rj == cj:
                hit = rec
                break

        # 1) 新增
        if hit is None:
            cv = dict(cv)
            cv["id"] = next_id
            next_id += 1
            cv["statusAt"] = now
            cv["statusBy"] = channel
            cv.pop("_unmapped_stage", None)
            embedded_records.append(cv)
            added += 1
            snap[skey] = {"status": new_status, "at": now}
            print(f"  + 新增：{co} {job}（{new_status}）")
            continue

        prev = snap.get(skey)

        # 2) 首次基线：只写快照；但台账岗位为空而本轮认出了岗位 → 补全（不动状态）
        if prev is None:
            if job and not str(hit.get("job") or "").strip():
                hit["job"] = job
            fill_observation_fields(hit, cv)   # 截止时间等时间字段照样补
            snap[skey] = {"status": new_status, "at": now}
            continue

        # 3) 状态没变化 → 状态不碰；岗位从无到有仍要补全；截止时间新识别出也要跟进
        if prev.get("status") == new_status:
            if job and not str(hit.get("job") or "").strip():
                hit["job"] = job
            fill_observation_fields(hit, cv)
            continue

        stats["changed"] += 1     # 本轮相对上次观察确实有变化

        # 4a) 台账已经等于本轮 → 只更新快照
        if hit.get("status") == new_status:
            snap[skey] = {"status": new_status, "at": now}
            continue

        old_status = hit.get("status", "")
        old_label = label_of.get(old_status, old_status)
        new_label = label_of.get(new_status, new_status)

        # 4b) 手动改过且是终态 → 冲突
        if hit.get("statusBy") == "manual" and old_status in TERMINAL:
            conflicts.append({
                "company": co, "job": job,
                "tracker_status": old_status, "tracker_label": old_label,
                "incoming_status": new_status, "incoming_label": new_label,
                "source": src_cn,
                "reason": f"台账是「{old_label}」，{src_cn}里却是「{new_label}」，没自动改",
            })
            snap[skey] = {"status": new_status, "at": now}
            continue

        # 4c) 只进不退：观察到的级别比台账低 → 冲突
        if rank_of(new_status, statuses) < rank_of(old_status, statuses):
            conflicts.append({
                "company": co, "job": job,
                "tracker_status": old_status, "tracker_label": old_label,
                "incoming_status": new_status, "incoming_label": new_label,
                "source": src_cn,
                "reason": f"台账是「{old_label}」，{src_cn}里却是「{new_label}」，没自动改",
            })
            snap[skey] = {"status": new_status, "at": now}
            continue

        # 正常写入状态
        hit["status"] = new_status
        hit["statusAt"] = now
        hit["statusBy"] = channel
        updated += 1
        stats["updates"].append({"company": co, "job": job,
                                 "from": old_label, "to": new_label})
        print(f"  ~ 同步：{co} {job} → {new_status}（{channel}）")

        # 顺带补全空字段（不覆盖已有手动内容；截止时间优先挂到日历）
        if job and not hit.get("job"):
            hit["job"] = job
        fill_observation_fields(hit, cv)

        snap[skey] = {"status": new_status, "at": now}

    stats["added"] = added
    stats["updated"] = updated
    stats["conflicts"] = conflicts
    return embedded_records, added, updated, conflicts


def main():
    csv_records = csv_to_records()
    html_text = HTML_FILE.read_text(encoding="utf-8")
    embedded, match = load_embedded(html_text)
    statuses = embedded.get("statuses") or []

    # ② 未知状态词归类（AI + 自动建状态）
    new_statuses = resolve_unmapped_stages(csv_records, statuses)

    snap = read_snapshot(SNAPSHOT_MAIL)
    merged, added, updated, conflicts = merge(
        embedded["records"], csv_records, channel="mail",
        snapshot=snap, statuses=statuses)

    # ③ 存量重复公司去重
    merged, merged_companies = dedupe_records(merged, statuses)
    embedded["records"] = merged
    embedded["statuses"] = statuses
    embedded["version"] = int(time.time() * 1000)  # 版本号变化 → 浏览器自动合并新数据

    new_block = json.dumps(embedded, ensure_ascii=False)
    new_html = html_text[:match.start(1)] + new_block + html_text[match.end(1):]
    HTML_FILE.write_text(new_html, encoding="utf-8")
    write_snapshot(SNAPSHOT_MAIL, snap)

    print("\n===== 同步完成 =====")
    print(f"邮件台账记录：{len(csv_records)} 条")
    print(f"新增公司：{added} 个")
    print(f"状态同步：{updated} 条")
    print(f"冲突待确认：{len(conflicts)} 条")
    if conflicts:
        for c in conflicts[:10]:
            print(f"  ! {c['company']} {c['job']}：{c['reason']}")
    if UNMAPPED:
        print(f"没见过的阶段词（已自动归类）：{'、'.join(sorted(UNMAPPED))}")
    if new_statuses:
        print(f"自动新建状态：{'、'.join(x['label'] for x in new_statuses)}")
    if merged_companies:
        print(f"合并重复公司：{len(merged_companies)} 条")
        for m in merged_companies:
            print(f"  - {' / '.join(m['from'])} → {m['into']}")
    print(f"追踪表现有记录：{len(merged)} 条")
    print("\n打开「秋招投递追踪表.html」即可看到最新状态（如已打开请按 F5 刷新）")


if __name__ == "__main__":
    main()
