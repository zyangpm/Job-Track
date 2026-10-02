#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
csv_to_js.py —— 把 JobHuntBot 的真实岗位池 / 简历路由表转成 Demo 能直接用的 JS 数据文件

用途
    读 dashboard/job_pool.csv 与 dashboard/resume_rules.csv，
    在 job_track/ 目录生成 job-data.js（window.JOB_POOL / window.RESUME_RULES），
    供 智能求职工作台-完整Demo-v3.html 用 <script src="job-data.js"> 引入。

为什么要有这个脚本
    Demo 是单文件 HTML，不能把 176 条岗位硬编码进去（体积大 + 以后更新不同步）。
    做成外部 job-data.js 后：双击 HTML 也能开（file:// 下同目录 script 允许加载），
    岗位池更新后只需重跑一次本脚本。

跑法
    python csv_to_js.py
    （会在脚本所在目录生成 job-data.js）

只读不改：不写回任何 CSV，不动 job_track 里其它业务文件。
"""

import csv
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
POOL_CSV = os.path.join(BASE, "JobHuntBot", "dashboard", "job_pool.csv")
RULES_CSV = os.path.join(BASE, "JobHuntBot", "dashboard", "resume_rules.csv")
OUT_JS = os.path.join(BASE, "job-data.js")

# 避雷岗关键词（来自 candidate_profile.json 的 roles_to_avoid）
AVOID = ("销售", "客服", "电销", "电话营销", "地推")


def clean(v):
    """去首尾空白 + 压缩连续空格；None 转空串"""
    if v is None:
        return ""
    return re.sub(r"[ \t　]+", " ", str(v)).strip()


def split_notes(notes):
    """
    notes 里塞了半结构化信息，拆成四块：
      分类：xxx ｜内推码：xxx 【推荐简历:张三-用户增长｜HR偏好:xxx｜简历匹配:xxx】｜后续：xxx
    返回 dict(cat, referral, resume, hr, match, extra)
    """
    out = {"cat": "", "referral": "", "resume": "", "hr": "", "match": "", "extra": ""}
    s = clean(notes)
    if not s:
        return out

    # 抓【...】块
    m = re.search(r"【(.+?)】", s, re.S)
    tail = ""
    if m:
        tail = m.group(1)
        s = s[: m.start()] + s[m.end():]
    else:
        return out

    # 【】里面按 ｜ 拆
    for part in re.split(r"[｜|]", tail):
        part = part.strip()
        if part.startswith("推荐简历:"):
            out["resume"] = part[len("推荐简历:"):].strip()
        elif part.startswith("HR偏好:"):
            out["hr"] = part[len("HR偏好:"):].strip()
        elif part.startswith("简历匹配:"):
            out["match"] = part[len("简历匹配:"):].strip()
        else:
            out["extra"] = (out["extra"] + " " + part).strip()

    # 【】外面的前半段：分类 / 内推码
    head = clean(s)
    mc = re.search(r"分类[：:]\s*([^｜|]*)", head)
    if mc:
        out["cat"] = mc.group(1).strip()
    mr = re.search(r"内推码[：:]\s*([^｜|]*)", head)
    if mr:
        out["referral"] = mr.group(1).strip()
    if not out["cat"]:
        out["cat"] = head[:60]
    return out


def normalize_cities(raw):
    """'上海/杭州/北京等' -> ['上海','杭州','北京']；'深圳龙岗' 保留原样（Demo 允许模糊匹配）"""
    s = clean(raw)
    if not s:
        return []
    parts = re.split(r"[/、,，\s]+", s)
    return [p for p in parts if p and p not in ("等", "全国不限")]


def build_pool():
    rows = []
    with open(POOL_CSV, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            company = clean(r.get("company"))
            title = clean(r.get("job_title"))
            if not company and not title:
                continue
            n = split_notes(r.get("notes"))
            cities = normalize_cities(r.get("location"))
            title_all = title + " " + clean(r.get("role_family"))
            avoid = [k for k in AVOID if k in title_all]
            # 硕士门槛：notes / skip_reason / role_family 里出现「硕士」「仅限硕士」
            blob = " ".join([title, clean(r.get("role_family")), clean(r.get("notes")),
                             clean(r.get("skip_reason"))])
            masters = bool(re.search(r"硕士|9/2|9月2日", blob))
            rows.append({
                "date_found": clean(r.get("date_found")),
                "company": company,
                "title": title,
                "fam": clean(r.get("role_family")) or "其他运营族",
                "level": clean(r.get("level")),
                "cities": cities,
                "loc_raw": clean(r.get("location")),
                "remote": clean(r.get("remote_policy")),
                "source": clean(r.get("source")),
                "url": clean(r.get("job_url")),
                "posted": clean(r.get("posted_date")),
                "priority": clean(r.get("priority")),
                "status": clean(r.get("status")),
                "resume": clean(r.get("resume_variant")),
                "skip_reason": clean(r.get("skip_reason")),
                "blocker": clean(r.get("blocker")),
                "next_action": clean(r.get("next_action")),
                "stage": clean(r.get("current_stage")),
                "cohort": clean(r.get("cohort_match_status")),
                "note_cat": n["cat"],
                "referral": n["referral"],
                "note_resume": n["resume"],
                "note_hr": n["hr"],
                "note_match": n["match"],
                "note_extra": n["extra"],
                "masters": masters,
                "avoid": avoid,
            })
    return rows


def build_rules():
    rows = []
    with open(RULES_CSV, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "fam": clean(r.get("role_family")),
                "resume": clean(r.get("resume_file_path")),
                "use_for": clean(r.get("use_for_titles")),
                "avoid_for": clean(r.get("avoid_for_titles")),
                "threshold": clean(r.get("tailor_threshold")),
                "note": clean(r.get("notes")),
            })
    return rows


def js_str(obj):
    """JSON → JS 字面量（保持中文原样，不转义）"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def main():
    pool = build_pool()
    rules = build_rules()

    lines = []
    lines.append("/* 本文件由 csv_to_js.py 自动生成，请勿手改。")
    lines.append("   源数据：JobHuntBot/dashboard/job_pool.csv + resume_rules.csv")
    lines.append("   岗位 %d 条 / 路由规则 %d 条　生成于 %s" % (len(pool), len(rules), "—"))
    lines.append("   重跑：python csv_to_js.py */")
    lines.append("window.JOB_POOL=" + js_str(pool) + ";")
    lines.append("window.RESUME_RULES=" + js_str(rules) + ";")
    lines.append("window.JOB_META=" + js_str({
        "total": len(pool),
        "rules": len(rules),
        "cities": sorted({c for r in pool for c in r["cities"]}),
        "fams": sorted({r["fam"] for r in pool if r["fam"]}),
        "priorities": sorted({r["priority"] for r in pool if r["priority"]}),
        "statuses": sorted({r["status"] for r in pool if r["status"]}),
        "masters": sum(1 for r in pool if r["masters"]),
        "avoided": sum(1 for r in pool if r["avoid"]),
    }) + ";")
    txt = "\n".join(lines)

    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(txt)

    print("OK  已生成 %s" % OUT_JS)
    print("    岗位 %d 条 / 路由规则 %d 条 / 城市 %d 个 / 岗位族 %d 个 / 硕士门槛 %d 条 / 避雷岗 %d 条"
          % (len(pool), len(rules), len({c for r in pool for c in r["cities"]}),
             len({r["fam"] for r in pool}), sum(1 for r in pool if r["masters"]),
             sum(1 for r in pool if r["avoid"])))


if __name__ == "__main__":
    main()
