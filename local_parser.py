# -*- coding: utf-8 -*-
"""
local_parser.py — 本地规则引擎：不花一分钱，自动解析招聘邮件
============================================================
原理：招聘邮件格式高度模板化，用关键词+正则即可提取。
识别不出的字段留"未知"，由 main.py 决定是否再调 DeepSeek 兜底。
"""
import re
from datetime import datetime, timedelta

# 公司名提取模式（按命中率从高到低）
COMPANY_PATTERNS = [
    re.compile(r"【([^【】]{2,20}?)】"),                                  # 【快手】xxx
    re.compile(r"^([A-Za-z0-9一-龥·&]{2,20}?)\s*[|｜]"),                  # 腾讯 | xxx
    # —— 强模板标题（位置固定、噪声小，优先于"xx校园招聘"这类宽松匹配）——
    # 允许前面有【笔试邀请】这类标签（如"【笔试邀请】美团邀请您参加在线笔试"）
    re.compile(r"(?:^|[】｜|\s])([A-Za-z0-9一-龥·&]{2,12}?)\s*(?:邀请你|邀请您|诚邀|邀你)"),  # 瑞幸咖啡邀请你参加…
    re.compile(r"感谢(?:您|你)对\s*([A-Za-z0-9一-龥·&（）()]{2,15}?)\s*的关注"),  # 感谢您对基恩士的关注
    re.compile(r"(?:已成功)?投递\s*([A-Za-z0-9一-龥·&]{2,15}?(?:集团|股份有限公司|"
               r"有限公司|公司|科技|网络技术))(?:校招|校园招聘)"),               # 已成功投递广州兴趣岛信息科技有限公司校招
    re.compile(r"感谢(?:您|你)?投递\s*([A-Za-z0-9一-龥·&]{2,18}?)\s*[的「『【]"),       # 感谢投递乐信…公司的 / 感谢投递宝宝巴士「岗位」
    re.compile(r"([一-龥A-Za-z0-9]{2,15}?)(?:集团|股份有限公司|有限公司|公司)?"
               r"(?:\d{4}\s*届?\s*(?:秋季|夏季|春季|冬季)?|\d{2}\s*届)?"
               r"(?:校园招聘|校招|秋招|春招)"),                          # 美团2027届校园招聘 / 快手2027秋季校园招聘
    re.compile(r"([一-龥A-Za-z0-9]{2,15}?)\s*的?\s*"
               r"(?:AI\s*视频面试|AI\s*面试|在线初评|线上初评|笔试|测评|面试|录用|签约)"
               r"(?:通知|邀请|提醒|函)"),                                 # 小米AI 面试邀请 / 网易在线测评邀请
    # —— R6 正文增补 ——
    re.compile(r"(?:您|你)投递(?:了|的)?\s*("
               r"[A-Za-z0-9一-龥·&]{2,10}?(?:咖啡|集团|公司|科技|银行|证券|汽车|"
               r"控股|股份|实业|商业|餐饮|电器|通讯|网络))\s*"
               r"(?:AI视频面试|AI面试|在线初评|面试|笔试|测评|初筛)"),          # 您投递的 瑞幸咖啡 AI面试…
    # —— R7 调研/问卷类标题（公司名 + 面试体验调研）——
    re.compile(r"^([A-Za-z0-9一-龥·&]{2,15}?)(?:的)?"
               r"(?:面试|笔试|测评|申请|投递)?(?:体验)?(?:调研|问卷)"),         # 字节跳动面试体验调研
    re.compile(r"感谢(?:您|你)参加\s*([A-Za-z0-9一-龥·&]{2,15}?)\s*20\d{2}"),  # 感谢你参加字节跳动2026-…的面试
]
# 公司名里的噪声词，命中后剔除
COMPANY_NOISE = ("邮件", "系统", "通知", "平台", "招聘组", "人力资源", "校园",
                 "请勿回复", "自动", "发送", "感谢", "您好", "尊敬", "方式",
                 "注意事项", "邀请", "回复", "简介", "介绍", "是谁", "宣讲",
                 "投递", "笔试", "测评", "面试", "网申", "攻略", "指南",
                 "高兴", "非常")
# 注意："信息"不能放入噪声词——"广州兴趣岛信息科技有限公司"是正经公司名
# 【招聘信息】【公司信息】类标签已由"招聘/公司"等噪声词覆盖

# 纯噪声公司名（整个名字就是这些词的组合），直接判为无法识别
_COMPANY_BAD_FULL = re.compile(
    r"^(我们是谁|笔试邀请|参加测评方式|宣讲信息|简历投递回复|公司简介|公司信息|"
    r"招聘信息|岗位信息|职位信息|信息|关于我们|关于|变更职位|变更|免费|"
    r"我公司|我们公司|笔试注意事项|\d{4}秋|\d{4}届校招|\d{4}届|"
    r"\d{2}届校招|\d{2}届|\d{2}|\d{4}秋季|校招|秋招|春招)$")

# 状态识别（顺序即优先级，上面优先）
# ★ AI 面试必须排在"待面试"前面："AI面试邀请"同时含"面试邀请"，排后面会被吞成普通面试
STATUS_RULES = [
    ("进入人才库", ["人才库", "talent pool"]),
    ("已拒绝", ["很遗憾", "感谢信", "未通过", "未能通过", "不再考虑", "不合适",
              "不予录用", "reject", "regret"]),
    ("Offer", ["录用通知", "录用函", "恭喜您通过", "offer letter", "录取通知书"]),
    ("OC", ["录用意向", "意向书", "意向沟通", "offer call", "oc沟通"]),
    ("终面", ["终面", "final面", "final interview", "高管面", "hr面", "hr面试"]),
    ("AI面试完成", ["ai面试完成", "完成ai面试", "ai面试已完成", "ai面已完成",
                "已完成ai面试"]),
    ("AI面试", ["ai面试", "ai视频面试", "智能面试", "ai video interview",
             "video ai interview"]),
    # 在线初评是独立环节（老板要求独立成状态），不归并进测评/面试
    ("在线初评", ["在线初评", "线上初评"]),
    ("待面试", ["面试邀请", "面试通知", "面试时间", "约面", "视频面试",
              "现场面试", "interview", "面试提醒", "进入面试", "面试环节"]),
    ("待笔试/测评", ["笔试", "测评", "在线测评", "测评邀请", "测评通知",
                 "assessment", "在线笔试", "笔试通知", "作答"]),
    ("简历筛选中", ["简历筛选", "初筛", "复筛", "筛选通过", "简历已进入",
                "正在评估", "评估中"]),
    ("已投递", ["投递成功", "已收到您的简历", "感谢您的投递", "投递确认",
              "已收到你的简历", "application received", "感谢投递",
              "网申成功", "报名成功"]),
]

# 岗位提取（按优先级；每条命中后都过 _clean_job 清洗，抓脏了就换一条试）
# 允许中英文/空格/括号/斜杠，遇到句读、竖线、换行停止
# 首字符不许是空白（防止跨行从上一行末尾开始抓）
_J_START = r"A-Za-z0-9一-龥（）()\-/、·&"
_J_CHARS = _J_START + r"\s"
# 破折号右侧像公司名（用于"岗位 - 公司（抖音旗下品牌）"类模板的右侧边界）
_COMPANY_SIDE = r"(?=[一-龥A-Za-z]{2,12}(?:[(（]|旗下|品牌|集团|公司|科技))"
JOB_PATTERNS = [
    # ⓪ 面试名称：27JDS-产品运营（标签最明确，排第一；项目码前缀交给 _clean_job 剥）
    re.compile(r"面试名称\s*[:：]\s*(\S{2,30})"),
    # ⓪b【2027秋】空调-海外后台管理(拓巡/拓司/船务) 职位（奥克斯风格）
    #    【2027届校招】用户增长校招生 职位（兴趣岛风格）
    re.compile(r"【\s*\d{4}\s*(?:届\s*(?:校招|秋招|春招)?|[秋春冬夏])?\s*】\s*"
               r"([" + _J_START + r"][^，。；;|｜\n]{1,28}?)\s*职位"),
    # ①a 多字标签（语义明确，标点可省）：应聘岗位/申请职位/岗位名称…
    re.compile(r"(?:岗位名称|职位名称|应聘岗位|应聘职位|申请岗位|申请职位|投递岗位|投递职位|"
               r"招聘岗位|招聘职位|目标岗位|目标职位)"
               r"\s*[:：是为\-—]?\s*([" + _J_START + r"][^，。；;|｜\n]{1,38})"),
    # ①b 裸"岗位/职位"必须紧跟冒号（否则"XX岗位的面试"会误抓正文）
    re.compile(r"(?:岗位|职位)\s*[:：]\s*([" + _J_START + r"][^，。；;|｜\n]{1,38})"),
    # ①c 英文标签
    re.compile(r"(?:Position|Job\s*Title)\s*[:：\-]?\s*"
               r"([" + _J_START + r"][^，。；;|｜\n]{1,38})", re.I),
    # ② 投递/申请/应聘/报名/进入（了）的 XX 岗位/职位
    re.compile(r"(?:投递|申请|应聘|报名|进入)(?:了|过)?(?:的)?"
               r"\s*([" + _J_START + r"][^，。；;|｜\n]{1,38}?)\s*(?:岗位|职位)"),
    # ②b 通过/入围 XX 的简历初筛（如"恭喜您已通过储备店长的简历初筛"）
    re.compile(r"(?:通过|入围|进入)(?:了)?\s*"
               r"([" + _J_START + r"][^，。；;|｜\n]{1,28}?)\s*(?:的)?"
               r"(?:简历初筛|简历筛选|初筛|复筛|简历评估|笔试筛选)"),
    # ②c "诚挚邀请你参加 商家BD（广州） - 万葭灯火（…品牌）"：破折号左侧是岗位
    # 若"参加"后紧接着"XX校园招聘-"结构（如"…参加万葭灯火校园招聘-岗位 - 公司"），让位给②d
    re.compile(r"(?:邀请你|邀请您|诚邀你?|邀你)\s*参加\s*"
               r"(?![^，。；;|｜\n]{0,12}校园招聘)"
               r"([" + _J_START + r"][^，。；;|｜\n]{1,28}?)\s*[-—]\s*" + _COMPANY_SIDE),
    # ②d "万葭灯火校园招聘-商家BD（广州） - 万葭灯火（…）"
    re.compile(r"校园招聘\s*[-—]\s*"
               r"([" + _J_START + r"][^，。；;|｜\n]{1,28}?)\s*[-—]\s*" + _COMPANY_SIDE),
    # ③ XX 岗位/职位 的 笔试/测评/面试/初评…（面试前加负向后顾，避免捕获尾部残留"AI"）
    re.compile(r"([" + _J_START + r"][^，。；;|｜\n]{1,38}?)\s*(?:岗位|职位)\s*"
               r"(?:的|相关)?\s*(?:AI面试|在线初评|笔试|测评|(?<!AI)面试|初评|录用|通知|邀请|流程)"),
    # ④ 标题：【公司】或 | 后到环节词之前的一段（命中率高但噪，放最后；AI面试要排在面试前）
    re.compile(r"[】｜|]\s*([" + _J_START + r"][^，。；;|｜\n]{1,30}?)"
               r"(?:AI面试|在线初评|在线测评|在线笔试|笔试|测评|(?<!AI)面试|初评|录用|通知函|邀请函|通知|邀请)"),
]

# 抓出来若是这些词/前缀，说明抓的是"岗位职责"之类的正文标签，不是岗位名
JOB_STOP_EXACT = {
    "职责", "岗位职责", "岗位描述", "职位描述", "岗位要求", "任职要求", "工作职责",
    "岗位介绍", "职位介绍", "岗位详情", "职位详情", "岗位信息", "招聘详情",
    "岗位", "职位", "此岗位", "该岗位", "未知", "无", "招聘", "校园招聘", "校招",
    "秋招", "春招", "点击查看", "查看详情", "更多", "详情", "名称", "岗位名称",
    "多个", "多个岗位", "多个职位", "此邮件由系统发出", "本邮件由系统发出",
    "系统发出", "由系统发出", "请勿直接回复", "当前", "邀您参加", "邀请你参加",
    "简历投递成功", "投递成功", "简历成功投递", "在线人才", "心仪",
}
JOB_STOP_PREFIX = ("职责", "描述", "要求", "介绍", "详情", "信息", "列表", "内容",
                   "薪资", "地点", "部门", "多个", "若干", "此邮件", "本邮件",
                   "系统", "请勿", "自动发", "如果", "希望", "请在", "中如果",
                   "邀您", "邀请", "线上宣讲会", "宣讲会", "流程结束",
                   "必读", "本次", "温馨提示")
# 岗位名结尾常粘连的环节/招聘噪声（剥掉再判）
# 注意："校招/秋招/春招"可能是岗位名一部分（如"产品运营-校招"），不剥；只剥"校园招聘"
JOB_TAIL_NOISE = re.compile(
    r"(校园招聘|招聘简章|招聘公告|笔试通知|面试通知|测评通知|"
    r"笔试邀请|面试邀请|测评邀请|在线初评|在线测评|在线笔试|笔试|测评|面试|初评|"
    r"录用|通知函|邀请函|通知|邀请|招聘)$")


def _clean_job(j, company=""):
    """岗位抓取结果清洗；无效返回"未知"。company=已识别公司名，用来剔除"岗位抓成公司名"。"""
    # 注意：不剥中英文括号——岗位名本身常带括号，如"商家BD（广州）"
    j = str(j or "").strip(" _-—：:，,。.；;|｜【】[]\t")
    # 标签值与后续正文之间常用连续空格/Tab 分隔（如"技术运营    考试时间"），只取第一段
    j = re.split(r"\s{2,}|\t", j, maxsplit=1)[0]
    j = re.sub(r"\s+", " ", j).strip(" _-—：:，,。.；;|｜")
    if not j:
        return "未知"
    # 多抓了正文起头的连接词/动词/泛指代词
    # "其他"开头多为条件句泛指（如"若申请其他咖啡师岗位"），不是真岗位
    if j[0] in "的了是为此" or j.startswith(("其他", "其它", "我们", "贵司", "本公司", "该公司")):
        return "未知"
    # —— 整句抢抓的二次提纯（各厂模板差异大，集中在这里剥）——
    # "2027届储备店长-杭州 结果：不匹配 张三" → 在"结果："处截断
    j = re.split(r"\s*结果\s*[:：]", j, maxsplit=1)[0]
    # "产品策划（用户增长） 意向部门： 音乐事业部 工作地点： 杭" → 在标签处截断
    j = re.split(r"\s+(?:意向部门|意向工作地|工作地点|工作城市|意向城市|投递时间|面试时间)\s*[:：]",
                 j, maxsplit=1)[0]
    # "基恩士 2027秋季校园招聘：销售工程师/销售" → 取"校园招聘：/的"之后的真岗位
    m = re.search(r"(?:校园招聘|校招|秋招|春招)\s*[的:：]\s*(.+)$", j)
    if m:
        j = m.group(1)
    # "FunPlus公司的27校招-用户运营" → 取"公司的"后面的真岗位
    m = re.match(r"^.{1,20}?(?:公司|集团|有限)\s*的\s*(.+)$", j)
    if m and len(m.group(1).strip()) >= 2:
        j = m.group(1)
    # 反复剥前缀：我公司的 / 27届 / 27秋招 / 校招- / 蔚来校园招聘-校招- / of 校招-
    for _ in range(3):
        prev = j
        j = re.sub(r"^(我公司|贵公司|本公司|该公司|公司)\s*的\s*", "", j.strip())
        j = re.sub(r"^(?:20)?\d{2}\s*届?\s*(?:秋招|春招|校招|秋季校园招聘|春季校园招聘)?\s*[-—:：·]?\s*", "", j)
        j = re.sub(r"^(?:of\s+)?(?:秋招|春招|校招|校园招聘)\s*[-—:：·]\s*", "", j)
        j = re.sub(r"^.{2,12}?校园招聘\s*[-—]\s*(?:校招\s*[-—])?\s*", "", j)
        if j == prev:
            break
    # 英文尾巴：中文岗位后面粘连 " in NIO Campus Re..."（in 前后允许无空格，如"运营in NIO"）
    j = re.split(r"(?<=[一-龥])\s*in\s+[A-Z][a-zA-Z]", j, maxsplit=1)[0]
    # 剥招聘编号尾巴 "(J22636)" / "（2027届）" / "-5367" / "-5367(J12443)"
    # 编号可能叠两层，循环剥到稳定（城市括号如"（北京）"不受影响）
    for _ in range(3):
        prev = j
        j = re.sub(r"\s*[（(]\s*(?:[A-Za-z]?\d{4,}|J\s*\d+|(?:20)?\d{2}\s*届[^)）]*)[）)]\s*$", "", j)
        j = re.sub(r"\s*[-—]\s*\d{3,}\s*[)）]?\s*$", "", j)
        if j == prev:
            break
    # 残缺未闭合括号截掉（如"校园大使（27届校"）
    if "（" in j and "）" not in j:
        j = j.split("（", 1)[0]
    if "(" in j and ")" not in j:
        j = j.split("(", 1)[0]
    # 公司名+「岗位」（如"宝宝巴士「市场运营专员（校招）"）→ 取最后一个左引号之后
    if "「" in j:
        tail = j.rsplit("「", 1)[1]
        if len(tail.strip("」『』 ")) >= 2:
            j = tail
    # 剥书名/直角引号（如"市场运营专员（校招）」"）
    j = j.strip("「」『』\"'")
    j = j.strip(" _-—：:，,。.；;|｜")
    if not j:
        return "未知"
    # 句子片段不是岗位：编号列举句、日期钟点句（如"9月21号：上午10点 宣讲群…"）
    if re.match(r"^\d+\s*[、.]", j):
        return "未知"
    if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*[号日]", j) and re.search(r"[:：]\s*\d|\d+\s*点", j):
        return "未知"
    # "2027届秋季校园招聘投递进度查询"这类流程/查询标题不是岗位
    if re.match(r"^(秋季|夏季|春季|冬季)?\s*校园?招聘", j) or "投递进度查询" in j or "招聘进度查询" in j:
        return "未知"
    # 纯项目代号（JDS/TET）、纯数量（1个）、无中文的代码碎片（x: initial）不是岗位
    if re.fullmatch(r"(JDS|TET|JD\s*STAR)", j, re.I):
        return "未知"
    if re.fullmatch(r"\d+\s*(个|项|封)?", j):
        return "未知"
    if not re.search(r"[一-龥]", j):
        return "未知"
    # 剥项目码前缀（如"27JDS-产品运营"→"产品运营"、"JDS-产品运营"→"产品运营"）
    j = re.sub(r"^(?:JDS|TET|JD\s*STAR|(?:\d{2,4}\s*)?[A-Za-z]{2,5})\s*[-—:：·]\s*",
               "", j, flags=re.I).strip(" _-—：:，,。.；;|｜")
    j = JOB_TAIL_NOISE.sub("", j).strip(" _-—：:，,。.；;|｜")
    if j in JOB_STOP_EXACT or len(j) < 2:
        return "未知"
    if any(j.startswith(p) for p in JOB_STOP_PREFIX):
        return "未知"
    # 只剩"年份+项目代号"（如"2027 JDS""27JDS""2026 TET"）→ 是项目名不是岗位
    if re.search(r"(20\d{2}|2[0-9]届)", j):
        core = re.sub(r"(20\d{2}|2[0-9]届?|JDS|TET|JD\s*STAR|\s)", "", j, flags=re.I)
        if len(core) < 2 or not re.search(r"[一-龥]", core):
            return "未知"
    # 抓出来的"岗位"剥干净后就是公司名（如"应聘基恩士校园招聘岗位"→基恩士）→ 无效
    if company:
        _nj = re.sub(r"[\s（）()]", "", j).lower()
        _nc = re.sub(r"[\s（）()]", "", str(company)).lower()
        # 公司名尾部渠道词（海能达招聘→海能达）不参与比较
        _nc2 = re.sub(r"(招聘|校园招聘|校招|人才)$", "", _nc)
        if _nj and _nc2 and len(_nc2) >= 2 and \
                (_nj == _nc2 or _nc2 in _nj or _nj in _nc2):
            return "未知"
        # "金发科技的"：岗位以公司名（≥4字）开头，后面只剩"的"→ 是句子不是岗位
        for _L in range(min(len(_nc2), 10), 3, -1):
            if _nj.startswith(_nc2[:_L]):
                _rest = _nj[_L:]
                if not _rest or _rest == "的" or _rest.startswith("的"):
                    return "未知"
                break
    # 只剩公司名+年份/届数/季节（如"快手2027""腾讯2027秋季""字节27届"）→ 不是岗位
    if re.search(r"(20\d{2}|2[0-9]届|秋季|夏季|春季|冬季)", j) and \
            re.fullmatch(r"[一-龥A-Za-z0-9]{2,14}(20\d{2}|2[0-9]届?)?(秋季|夏季|春季|冬季)?", j):
        return "未知"
    # 纯编号（J12345 / 200559112）单独出现不算岗位
    if re.fullmatch(r"[A-Za-z]?\d{3,}", j):
        return "未知"
    return j[:30]

# 面试时间：2026-09-30 14:00 / 9月30日14点 / 09/30 19:45
TIME_PATTERNS = [
    re.compile(r"(20\d\d)[-年/.](\d{1,2})[-月/.](\d{1,2})日?\s*"
               r"(\d{1,2})[:：点时](\d{2})?"),
    re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：点时](\d{2})?"),
]

# 截止时间：笔试/测评/AI面试这类"窗口期任务"的完成期限（区别于面试的赴约时间点）
# 带年份的（y 组），与只有月日的（年份用邮件日期推断）分开；钟点可带秒
_T = r"(?:(?P<h>\d{1,2})\s*[:：点]\s*(?P<mi>\d{2})(?::\d{2})?)?"   # 可选钟点
_DEADLINE_FULL = [
    # 截止(时间)：2026-09-19 23:59 / 截止至2026年9月20日24点
    re.compile(r"截(?:止|至)(?:时间|日期)?\s*[:：是为]?\s*"
               r"(?:20)?(?P<y>\d{2})\s*[-年/.]\s*(?P<mo>\d{1,2})\s*[-月/.]\s*(?P<d>\d{1,2})\s*日?\s*" + _T),
    # 请在/请于 2026年9月19日 23:59 前
    re.compile(r"请\s*(?:在|于)\s*(?:20)?(?P<y>\d{2})\s*年\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"
               r"[^。\n]{0,20}?" + _T + r"\s*前"),
    # 请在 2026-09-15 24:00 前 / 请在 2026-09-20 20:26:13 之前完成
    re.compile(r"请\s*(?:在|于)\s*(?:20)?(?P<y>\d{2})\s*[-/.]\s*(?P<mo>\d{1,2})\s*[-/.]\s*(?P<d>\d{1,2})"
               r"(?:[^\d。\n]{0,12}?)?" + _T + r"\s*之?\s*前"),
    # 有效期至 2026-09-15 24:00
    re.compile(r"有效(?:期|时间)[^。\n]{0,6}?(?:至|截止到?)\s*"
               r"(?:20)?(?P<y>\d{2})\s*[-年/.]\s*(?P<mo>\d{1,2})\s*[-月/.]\s*(?P<d>\d{1,2})\s*日?\s*" + _T),
    # 于 2026年09月25日 周五 16:10 失效
    re.compile(r"于\s*(?:20)?(?P<y>\d{2})\s*年\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"
               r"\s*(?:周[一二三四五六日天])?\s*" + _T + r"\s*失效"),
]
_DEADLINE_MD = [
    # 截止(时间)：9月20日 24:00
    re.compile(r"截(?:止|至)(?:时间|日期)?\s*[:：是为]?\s*"
               r"(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?\s*" + _T),
    # 请在/请于 9月19日 23:59 前 / 请于9月20日前
    re.compile(r"请\s*(?:在|于)\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"
               r"(?:[^。\n]{0,12})?" + _T + r"\s*前"),
    # 9月19日23:59前完成 / 9月19日前作答 / 9月20日之前参加
    re.compile(r"(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?\s*" + _T +
               r"\s*前(?:[^。\n]{0,6})?(?:完成|作答|提交|登录|参加|进入|确认|测评|面试|笔试)"),
    re.compile(r"(?:于|在)\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"
               r"(?:[^。\n]{0,10})?前(?:完成|作答|提交|参加|登录)"),
    # 有效期至9月15日24:00
    re.compile(r"有效(?:期|时间)[^。\n]{0,6}?(?:至|截止到?)\s*"
               r"(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?\s*" + _T),
]
# 相对期限：收到通知后72小时内有效 / 自发起之日起七日内有效
_DEADLINE_REL = [
    re.compile(r"(?P<n>\d+)\s*小时(?:之)?内有效"),
    re.compile(r"(?P<n>\d+|[一二两三四五六七八九十])\s*[日天](?:之)?内有效"),
]
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _infer_year(month, mail_date):
    """邮件里只写月日：用邮件年份，跨年（如1月邮件说12月截止）归到下一年"""
    try:
        y = int(str(mail_date or "")[:4])
        m0 = int(str(mail_date or "")[5:7])
    except (ValueError, TypeError):
        y, m0 = datetime.now().year, datetime.now().month
    if month < m0 and (m0 - month) > 6:
        y += 1
    return y


def _extract_relative_deadline(text, mail_date):
    """'72小时内有效/七日内有效' → 以邮件日期为起点推算"""
    try:
        base = datetime.strptime(str(mail_date)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return "无"
    for pat in _DEADLINE_REL:
        m = pat.search(text)
        if not m:
            continue
        ns = m.group("n")
        n = int(ns) if ns.isdigit() else _CN_NUM.get(ns)
        if not n:
            continue
        if "小时" in m.group(0):
            dt = base + timedelta(hours=n)
            return dt.strftime("%Y-%m-%d %H:%M") if dt.time() != base.time() \
                else dt.strftime("%Y-%m-%d")
        dt = base + timedelta(days=n)
        return dt.strftime("%Y-%m-%d")
    return "无"


def extract_deadline(mail):
    """笔试/测评/AI面试等的完成截止期限 → 'YYYY-MM-DD HH:mm' / 'YYYY-MM-DD' / '无'"""
    # 正文是 HTML：去掉标签和 &nbsp;，避免它们把日期句式切断
    text = (mail.get("subject") or "") + "\n" + (mail.get("body") or "")
    text = re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ").replace("\xa0", " ")
    mail_date = mail.get("date") or ""
    # 先找带年份的绝对日期，再找月日，最后相对期限（"72小时内"最模糊，放最后）
    for pat in _DEADLINE_FULL + _DEADLINE_MD:
        m = pat.search(text)
        if not m:
            continue
        try:
            mo, d = int(m.group("mo")), int(m.group("d"))
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                continue
            y = 2000 + int(m.group("y")) if m.group("y") else _infer_year(mo, mail_date)
            s = f"{y:04d}-{mo:02d}-{d:02d}"
            if m.group("h"):
                s += f" {int(m.group('h')):02d}:{int(m.group('mi') or 0):02d}"
            return s
        except (ValueError, TypeError):
            continue
    return _extract_relative_deadline(text, mail_date)


def _clean_company(name):
    name = name.strip(" _-—：:，,。.")
    # 宽松正则偶尔从句中抢抓（如"并应聘基恩士校园招聘"），动词/连词起头一律判脏
    if name[:1] in "并和与及了的":
        return ""
    # 副词/语气收尾（如"非常高兴地邀请您"被强模板抢抓）不是公司名
    if name[-1:] in "地得很":
        return ""
    if name.startswith(("应聘", "申请", "投递", "招聘", "参加", "进入")):
        return ""
    # 剥掉抢抓进来的登录引导前缀（如"您可以登录科大讯飞校园招聘官网"）
    name = re.sub(r"^(您可以登录|你可以登录|请登录|欢迎登录|登录|来自)", "", name).strip()
    # 第一人称前缀（"我公司的XX"抢抓、营销邮件"我是牛客"）一律判脏
    if name.startswith(("我公司", "我们公司", "本公司", "贵公司", "我是")):
        return ""
    # 正则后缀重复（如"美图公司公司""XX股份有限公司公司"）归一
    name = re.sub(r"(公司|集团)\1+$", r"\1", name)
    name = re.sub(r"(股份有限公司)公司$", r"\1", name)
    # 剥掉公司名尾部粘连的年份/季节/渠道词（如"快手2027秋季""网易在线""小米AI"）
    name = re.sub(r"(?:20\d{2}|2[0-9]{2})\s*届?\s*(?:秋季|夏季|春季|冬季)?$", "", name)
    name = re.sub(r"(?:秋|春|夏|冬)季$", "", name)
    name = re.sub(r"(?:在线|AI|ai)$", "", name).strip(" _-—")
    if len(name) < 2 or any(n in name for n in COMPANY_NOISE):
        return ""
    if _COMPANY_BAD_FULL.match(name):
        return ""
    return name


def extract_company(subject, body):
    for pat in COMPANY_PATTERNS:
        for m in pat.finditer(subject + "\n" + body[:300]):
            name = _clean_company(m.group(1))
            if name:
                return name
    return "未知"


# 假设/条件句信号：关键词前出现这些词时，该关键词只是"假设后果"，不代表当前状态。
# 如"如未作答则视为放弃……或将您的简历收藏至人才库"——这仍是 AI 面试邀请，不是进人才库。
# 如快手测评防作弊条款："如发现违规…招聘方将对您做出不予录用的最终决定"——不是已拒绝。
_HYPOTHETICAL_RE = re.compile(
    r"(如未|若未|如果未|若是未|如不|若不|否则|不然|或者|或将|亦或|可能|也许|"
    r"假设|一旦未|若未能|如未能|如发现|若发现|一旦|一经|视为放弃|视为|"
    r"将被|会被|则将|将对|将予以|将做出|将作出|会做出|有权)")
_HYP_LOOKBACK = 50       # 向前看 50 个字判断是否在假设句中

# 流程说明句："招聘流程：投递简历--简历筛选--笔试/面试--发放录用通知"
# 这里列举的所有环节都不是当前状态，命中要跳过（否则"录用通知"会被误判成 Offer）
_PROCESS_RE = re.compile(
    r"招聘流程|应聘流程|招考流程|流程(?:如下|介绍|说明|为|是|：|:)|"
    r"环节(?:如下|介绍|说明|为|：|:)|投递简历\s*[-—→>～~]{1,2}|"
    r"(?:--|—|→|->)\s*(?:发放|发)?(?:offer|录用)")
_PROC_LOOKBACK = 60


def _match_status_in(text):
    """在已去空白的文本里按规则优先级找状态；跳过假设句中的关键词。"""
    for status, keywords in STATUS_RULES:
        for kw in keywords:
            kw_c = re.sub(r"\s+", "", kw.lower())
            for m in re.finditer(re.escape(kw_c), text):
                prefix = text[max(0, m.start() - _HYP_LOOKBACK):m.start()]
                if _HYPOTHETICAL_RE.search(prefix):
                    continue            # 只是条件句里提到，不算当前状态
                if _PROCESS_RE.search(text[max(0, m.start() - _PROC_LOOKBACK):m.start()]):
                    continue            # 只是招聘流程说明里列举的环节
                return status
    return None


def extract_status(subject, body):
    subj_c = re.sub(r"\s+", "", (subject or "")).lower()
    subj_hit = _match_status_in(subj_c)
    # 全文去空白（解决"AI 面试"带空格），假设句里的关键词自动跳过
    text = re.sub(r"\s+", "", (subject or "") + "\n" + (body or "")).lower()
    body_hit = _match_status_in(text)
    # 正文出现明确终态（拒绝/Offer/OC/人才库）→ 终态优先，防止旧环节标题误导
    if body_hit in ("已拒绝", "Offer", "OC", "进入人才库"):
        return body_hit
    # 其余情况标题信号最强（如"邀请你参加在线AI面试"）
    return subj_hit or body_hit or "未知"


def extract_job(subject, body, company=""):
    # 全文搜索（fetch 时正文已截到 3000 字）；每条规则命中后都要过清洗，抓脏了换下一条
    text = (subject or "") + "\n" + (body or "")
    for pat in JOB_PATTERNS:
        for m in pat.finditer(text):
            job = _clean_job(m.group(1), company)
            if job != "未知":
                return job
    return "未知"


def extract_interview_time(subject, body):
    text = subject + "\n" + body
    year_now = datetime.now().year
    for pat in TIME_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g) == 5:  # 带年份
                y, mo, d, h, mi = int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4] or 0)
            else:            # 只有月日
                y, mo, d, h, mi = year_now, int(g[0]), int(g[1]), int(g[2]), int(g[3] or 0)
            return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"
        except ValueError:
            continue
    return "无"


def parse_email(mail):
    """解析单封邮件，返回记录 dict"""
    subject, body = mail["subject"], mail["body"]
    company = extract_company(subject, body)
    interview = extract_interview_time(subject, body)
    deadline = extract_deadline(mail)
    # 同一时刻既被当面试时间又被当截止时间（如"9月19日23:59前完成AI面试"）
    # → 它是截止期限，不是赴约时间点
    if deadline != "无" and interview[:10] == deadline[:10] and \
            (len(deadline) == 10 or interview[:16] == deadline[:16]):
        interview = "无"
    return {
        "投递日期": mail.get("date", "未知"),
        "公司全称": company,
        "岗位名称": extract_job(subject, body, company),
        "招聘阶段": extract_status(subject, body),
        "面试时间": interview,
        "截止时间": deadline,
    }


def _load_learned():
    """读"AI 总结出来的规律"（learned_rules.json）：标题含某关键词 → 对应阶段"""
    try:
        import json as _j
        from pathlib import Path as _P
        f = _P(__file__).resolve().parent / "learned_rules.json"
        if f.exists():
            d = _j.loads(f.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


def _apply_learned(mail, rec, learned):
    """用学到的规律补一下（主要是把"未知"的阶段补上）"""
    txt = (mail.get("subject") or "") + " " + (mail.get("body") or "")[:300]
    for rule in learned:
        if not isinstance(rule, dict):
            continue
        kw = str(rule.get("keyword") or "").strip()
        stage = str(rule.get("stage") or "").strip()
        if kw and stage and kw in txt:
            if rec.get("招聘阶段") in (None, "", "未知"):
                rec["招聘阶段"] = stage
            return rec
    return None

# 调研/问卷类邮件：面完不管过不过都会发，不含任何状态信息，
# 若当状态卡展示会误导（如"字节跳动面试体验调研"被当成"面试完成"）。整封跳过。
_SURVEY_RE = re.compile(r"(体验调研|体验调查|满意度|调研问卷|问卷调查|问卷调研)")


def is_survey_mail(subject, body):
    return bool(_SURVEY_RE.search((subject or "") + "\n" + (body or "")[:200]))


# 账号/安全/产品营销类通知：与求职无关，整封跳过，尤其不能进 need_ai 让大模型脑补
_NONJOB_SUBJ_RE = re.compile(
    r"verification code|sudo (email|authentication)|验证码|校验码|"
    r"oauth application|two[- ]factor|2[- ]step verification|\b2fa\b|recovery codes?|"
    r"review this sign[ -]?in|new sign[ -]?in|signed in to|sign[ -]?in attempt|"
    r"has been added to your account|shared some google account data|"
    r"finish(?:ed)? setting up|almost done setting|set up google|"
    r"please download your|password (?:was|has been) (?:reset|changed)|"
    r"security alert|api[ -]?key|newsletter|unsubscribe|product update|月度资讯|"
    r"绑定验证|更换邮箱|邮箱绑定|"
    r"premium for|months? of|ad[- ]free|s\$\s?\d|playlist|fit you|accelerates inference|"
    r"access to daily help|隐私政策更新|申请快要完成|是不是忘了",
    re.I)
_WELCOME_EN_RE = re.compile(r"^welcome to\b", re.I)
_RECRUIT_CTX_RE = re.compile(
    r"applic|interview|campus|graduat|position|job offer|recruit|assessment|"
    r"笔试|面试|测评|招聘|校招|秋招|春招", re.I)


def is_non_job_mail(mail):
    subj = (mail.get("subject") or "").strip()
    if _NONJOB_SUBJ_RE.search(subj):
        return True
    if _WELCOME_EN_RE.search(subj) and not re.search(r"[一-龥]", subj) \
            and not _RECRUIT_CTX_RE.search(subj + " " + (mail.get("body") or "")[:300]):
        return True
    return False


def parse_emails(mails):
    """批量解析，合并去重（同公司同岗位保留最新）→ (records, need_ai)
    need_ai：[(mail, local_rec), ...] 本地有任一字段没认出的邮件，连同本地半成品
             一起交给 DeepSeek 兜底（AI 挂了也能用本地半成品兜底，不至于整封丢失）"""
    seen = {}
    need_ai = []
    learned = _load_learned()          # ★ 上次 AI 总结出来的规律
    for m in mails:
        if is_survey_mail(m.get("subject"), m.get("body")):
            continue                   # 调研/问卷邮件不产状态卡
        if is_non_job_mail(m):
            continue                   # 账号/验证码/营销通知不产状态卡，也不送 AI
        rec = parse_email(m)
        # ★ 本地没认出来时，先用"学到的规律"再试一次（越用越准）
        if learned and (rec["公司全称"] == "未知" or rec["招聘阶段"] == "未知"):
            try:
                _r2 = _apply_learned(m, rec, learned)
                if _r2:
                    rec = _r2
            except Exception:
                pass
        # 公司 / 阶段 / 岗位 任一没识别出来 → 都交 DeepSeek 兜底，不产出"未知岗位"的本地记录
        if rec["公司全称"] == "未知" or rec["招聘阶段"] == "未知" or rec["岗位名称"] == "未知":
            need_ai.append((m, rec))
            continue
        key = (rec["公司全称"], rec["岗位名称"])
        # 同键保留状态更靠后（规则表顺序）的那条
        if key not in seen:
            seen[key] = rec
        else:
            old = seen[key]
            order = [s for s, _ in STATUS_RULES]
            new_i = order.index(rec["招聘阶段"]) if rec["招聘阶段"] in order else 99
            old_i = order.index(old["招聘阶段"]) if old["招聘阶段"] in order else 99
            # 状态表里越靠后进度越深（已投递在最后、Offer/拒绝在最前是终态）
            # 终态优先；否则取进度更深的（索引大的）
            terminal = {"已拒绝", "Offer", "进入人才库"}
            if rec["招聘阶段"] in terminal:
                seen[key] = rec
            elif old["招聘阶段"] not in terminal and new_i > old_i:
                seen[key] = rec
            # 面试时间/截止时间新的覆盖旧的空的
            if seen[key]["面试时间"] == "无" and rec["面试时间"] != "无":
                seen[key]["面试时间"] = rec["面试时间"]
            if seen[key].get("截止时间", "无") == "无" and rec.get("截止时间") != "无":
                seen[key]["截止时间"] = rec["截止时间"]
    return list(seen.values()), need_ai
