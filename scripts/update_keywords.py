#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI_KEYWORDS.json 自动补词（每周跑一次，由 .github/workflows/update-keywords.yml 调）

它干什么
  1. 抓一轮互动易 + 上证e互动的问答池（复用 fetch_qna.py 里的抓取函数）
  2. 挑出"当前词库已经命中的"那些条目，当语料 —— 这批才是 AI 产业链相关的问答
  3. 把 语料 + 现有分类清单 交给大模型，让它指出"语料里出现、但词库还没有的具体行业术语"
  4. 本地校验（分类必须存在、必须是语料里真实出现的字串、去重、每次最多加 MAX_NEW 个）
  5. 通过校验的追加进 AI_KEYWORDS.json，更新 updated_at，并记一条 changelog
  6. 没有任何新增 → 不写文件（不产生空提交）

刻意的边界
  - 只加词，绝不删词、改词、改分类：删改要人看。模型只能在既有分类下追加。
  - 每个新词都必须"原样出现在本次语料里"（防编造）。模型想加的新词如果语料里没有，直接丢。
  - 任何一步失败（没配 key、超时、返回乱码）→ 一律不动文件、退出码 0，不影响仓库其它流程。

环境变量
  DEEPSEEK_API_KEY   必填（GitHub 仓库 Settings → Secrets → Actions 里加）
  DEEPSEEK_BASE_URL  可选，默认 https://api.deepseek.com/v1
  DEEPSEEK_MODEL     可选，默认 deepseek-flash
  MAX_NEW            可选，本次最多新增几个词，默认 20
  CORPUS_CHARS       可选，语料字数上限，默认 16000

本地试跑（只看模型想加什么，不写文件）：
  python scripts/update_keywords.py --dry-run
"""
import html as html_lib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DICT_PATH = os.path.join(BASE_DIR, "AI_KEYWORDS.json")
TZ = timezone(timedelta(hours=8))

MAX_NEW = int(os.environ.get("MAX_NEW", "20"))
MAX_TOTAL = int(os.environ.get("MAX_TOTAL", "1200"))     # 词库总量上限：超过就不再加，防止越滚越大
CORPUS_CHARS = int(os.environ.get("CORPUS_CHARS", "16000"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
WECOM_WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "").strip()   # 可选：填了就把周报推到企微群

# 这些词太泛，塞进词库只会让 RSS 刷屏，模型提了也不收
TOO_GENERIC = {"发展", "业绩", "客户", "公司", "产品", "技术", "行业", "市场", "投资", "增长",
               "研发", "合作", "订单", "量产", "扩产", "涨价", "供应", "需求", "业务", "项目",
               "AI", "人工智能", "大模型", "服务器", "芯片", "存储", "机器人", "数据中心"}

PROMPT = """你是 A股 AI 产业链投资情报编辑。下面给你三样东西：

【一】现有词库的分类清单（格式：分类id = 分类名）
【二】最近的 A股互动平台问答原文（都是已经命中现有词库的、AI 产业链相关问答）
【三】最近一周的财经媒体要闻标题（华尔街见闻早餐 + 虎嗅），
    以及【四】本周/上周各关键词在互动问答里的命中次数（用于看热度变化；没有数据时这块会是空的）

你要做两件事，一次输出。

任务一（补词）：从【二】【三】里找出**词库里还没有的、具体的行业术语**，归到【一】的某个分类。
只要三类东西：
1. 产品/器件/材料的具体名称（例：硅光引擎、液冷板、铜连接、电子级氢氟酸）
2. 技术/工艺/型号/标准（例：CoWoS-L、3D 堆叠、浸没式液冷、CPO 交换机、800G DRAM）
3. 产业链环节的专用叫法（例：光引擎封装、晶圆再生、载板级封装）

任务二（看方向）：结合【三】的新闻和【四】的热度数字，指出**本周 A股 AI 方向里正在变热的 2~4 条线**
（写成能直接当行业名用的短语，不要写公司名、不要写具体事件），并给一句整体观察。

硬规则：
- 新词必须**原样出现在【二】或【三】的原文里**（不许自己造、不许改字）。没出现的词一律不要输出。
- 新词每个只能归到一个分类，分类 id 必须来自【一】。
- 新词最多 20 个；太泛的词（发展、业绩、客户、技术、量产、涨价这种）不要提；有明显的才提，没有就空数组。
- directions 里用的是"方向/环节"名称（如 存储涨价、液冷散热、电力配套、光互联），不是公司名也不是新闻标题。
- trend 只能填 升 / 平 / 降；没有热度数据可对比时填 平。
- 只输出 JSON，不要任何解释文字，格式：
{"add": [{"category": "分类id", "keyword": "词", "why": "不超过15字的理由"}],
 "directions": [{"direction": "方向名", "trend": "升|平|降", "evidence": "不超过30字，说明依据"}],
 "note": "不超过60字的整体观察"}"""


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------- 词库读写 ----------
def flat_keywords(data):
    out = set()

    def walk(node):
        for k in node.get("keywords") or []:
            if k and k.strip():
                out.add(k.strip())
        for e in node.get("entities") or []:
            if e and e.strip():
                out.add(e.strip())
        for sub in node.get("subcategories") or []:
            walk(sub)

    for c in data.get("categories") or []:
        walk(c)
    return out


def category_index(data):
    """返回 {分类id: 分类名} 和 {叶子分类id: 归属链}"""
    ids, leaves = {}, {}

    def walk(node, chain):
        cid, cname = node.get("id"), node.get("name", "")
        if cid:
            ids[cid] = cname
        subs = node.get("subcategories") or []
        if cid and not subs:
            leaves[cid] = " / ".join(chain + [cname])
        for sub in subs:
            walk(sub, chain + [cname])

    for c in data.get("categories") or []:
        walk(c, [])
    return ids, leaves


def add_keywords(data, additions):
    """把 {分类id: [词...]} 追加进 JSON 结构，返回实际加进去的词"""
    added = []

    def walk(node):
        cid = node.get("id")
        if cid in additions and additions[cid]:
            node.setdefault("keywords", [])
            for kw in additions[cid]:
                node["keywords"].append(kw)
                added.append(kw)
            additions[cid] = []
        for sub in node.get("subcategories") or []:
            walk(sub)

    for c in data.get("categories") or []:
        walk(c)
    return added


# ---------- 大模型 ----------
def deepseek(messages, max_tokens=2000, timeout=180):
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},   # 不关会吃光 max_tokens、content 返回空
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        DEEPSEEK_BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + DEEPSEEK_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)["choices"][0]["message"]["content"]


# ---------- 新闻语料（补词的第二来源：董秘问答比行情慢半拍，新主题往往先在新闻里冒头） ----------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def http_text(url, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def html_lines(frag, min_len=8):
    s = re.sub(r"<script.*?</script>", " ", frag, flags=re.S | re.I)
    s = re.sub(r"<(?:br|/p|/div|/h[1-6]|/li)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s).replace("\u00a0", " ")
    out = []
    for ln in s.split("\n"):
        ln = re.sub(r"[\s\u3000]+", " ", ln).strip(" -—|·•")
        if len(ln) >= min_len:
            out.append(ln)
    return out


def wscn_headlines(days=7):
    """华尔街见闻早餐的「要闻」段：一天一句话一条，正好是"最近发生了什么"。
    （给用户推送时这段摘要是要丢掉的——放这儿反而正合适。）"""
    out = []
    head = {"Referer": "https://wallstreetcn.com/"}
    try:
        lst = json.loads(http_text("https://api-one-wscn.awtmt.com/apiv1/content/articles"
                                   "?category=breakfast&limit=20", head))["data"]["items"]
    except Exception as e:
        log(f"见闻列表失败：{type(e).__name__}")
        return out
    picks = [it for it in lst if "早餐FM-Radio" in (it.get("title") or "")][:days]
    for it in picks:
        m = re.search(r"/articles/(\d+)", it.get("uri") or "")
        if not m:
            continue
        d = re.search(r"(\d{1,2})月(\d{1,2})日", it.get("title") or "")
        tag = "%s-%s" % (d.group(1), d.group(2)) if d else ""
        try:
            c = json.loads(http_text("https://api-one-wscn.awtmt.com/apiv1/content/articles/%s?extract=0"
                                     % m.group(1), head))["data"].get("content") or ""
        except Exception as e:
            log(f"见闻正文失败 {m.group(1)}：{type(e).__name__}")
            continue
        lines = []
        for blk, body in re.findall(r"<h2[^>]*>(.*?)</h2>(.*?)(?=<h2|$)", c, re.S):
            if "要闻" in re.sub(r"<[^>]+>", "", blk) and "详情" not in re.sub(r"<[^>]+>", "", blk):
                lines = html_lines(body, 10)
                break
        if not lines:
            lines = html_lines(c, 10)[:12]
        lines = [x for x in lines if len(x) >= 10][:14]
        if lines:
            out.append("[%s 见闻早餐要闻] %s" % (tag, "；".join(lines)))
    log(f"见闻语料：{len(out)} 天")
    return out


def huxiu_titles(limit=30):
    """虎嗅 RSS 标题（公开，无鉴权）"""
    out = []
    try:
        raw = http_text("https://rss.huxiu.com/")
    except Exception as e:
        log(f"虎嗅 RSS 失败：{type(e).__name__}")
        return out
    for t in re.findall(r"<title>(.*?)</title>", raw, re.S)[1:limit + 1]:
        t = html_lib.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t)).strip()
        if t:
            out.append(t)
    log(f"虎嗅标题：{len(out)} 条")
    return out


def heat_from_state(keep=3, top=30):
    """读 state.json 的 kw_hits（fetch_qna.py 每轮累计，按 ISO 周分桶），给出最近几周的命中榜。
    周任务在周一早上跑，所以"本周"桶基本是空的 —— 要连着给两周，模型才看得出趋势。"""
    p = os.path.join(BASE_DIR, "state.json")
    weeks = {}
    if os.path.exists(p):
        try:
            weeks = (json.load(open(p, encoding="utf-8")) or {}).get("kw_hits") or {}
        except Exception as e:
            log(f"state.json 读取失败：{type(e).__name__}")
    keys = sorted(weeks)[-keep:]
    blocks, summary = [], {}
    cur = datetime.now(TZ).strftime("%G-W%V")
    for k in keys:
        items = sorted(weeks[k].items(), key=lambda kv: -kv[1])[:top]
        summary[k] = dict(items)
        label = "本周（刚开始，数据不完整）" if k == cur else "已结束的那一周"
        blocks.append("%s %s：%s" % (k, label, "、".join("%s %d" % (a, b) for a, b in items) or "（无）"))
    if not keys:
        blocks.append("（还没有命中数据：state.json 里没有 kw_hits，等抓取任务跑几轮后才有）")
    return blocks, summary


# ---------- 企微推送（可选） ----------
def weekly_md(added, directions, note, heat_weeks):
    """拼周报正文（企微 markdown：没有表格，只能用标题/加粗/列表/链接）"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    lines = ["### 🧭 AI 词库周报（%s）" % today,
             "> 新增 %d 词 ｜ 方向 %d 条 ｜ 热度数据 %d 周" % (len(added), len(directions), len(heat_weeks))]
    if note:
        lines += ["", "**一句话**：%s" % note]
    if directions:
        lines += ["", "**本周在变热的方向**"]
        for i, d in enumerate(directions, 1):
            lines.append("%d. %s（%s）：%s" % (i, d.get("direction", ""), d.get("trend", "?"),
                                              str(d.get("evidence", ""))[:40]))
    if added:
        lines += ["", "**新收进词库**", "、".join(added[:18]) + ("…" if len(added) > 18 else "")]
    lines += ["", "> [词库文件](https://github.com/liuchao88/hudong-rss/blob/main/AI_KEYWORDS.json)"]
    return "\n".join(lines)


def wecom_push(text):
    """把周报推到企微群。没配 WECOM_WEBHOOK_URL 就跳过；推失败也只记日志，不影响词库更新。"""
    if not WECOM_WEBHOOK_URL:
        log("未配置 WECOM_WEBHOOK_URL，跳过推送")
        return
    payload = {"msgtype": "markdown", "markdown": {"content": text}}
    try:
        req = urllib.request.Request(WECOM_WEBHOOK_URL, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        log(f"企微推送：{body[:120]}")
    except Exception as e:
        log(f"企微推送失败（不影响词库更新）：{type(e).__name__}: {e}")


# ---------- 主流程 ----------
def main():
    dry_run = "--dry-run" in sys.argv
    if not os.path.exists(DICT_PATH):
        log(f"没有 {DICT_PATH}，跳过")
        return 0
    data = json.load(open(DICT_PATH, encoding="utf-8"))
    known = flat_keywords(data)
    ids, leaves = category_index(data)
    log(f"词库：{len(known)} 个词，{len(ids)} 个分类")

    if not DEEPSEEK_API_KEY:
        log("未配置 DEEPSEEK_API_KEY，跳过（不动文件）")
        return 0
    if len(known) >= MAX_TOTAL:
        log(f"词库已有 {len(known)} 个词，达到上限 {MAX_TOTAL}，这次不再加（不动文件）")
        return 0

    # 抓语料：只用命中的条目（那批才是 AI 相关的）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fetch_qna as fq

    matcher = fq.build_matcher(sorted(known))
    corpus_items = []
    for platform, fn in (("互动易", fq.fetch_szse), ("上证e互动", fq.fetch_sse)):
        try:
            got = fn()
        except Exception as e:
            log(f"{platform} 抓取失败：{e}")
            continue
        log(f"{platform} 抓到 {len(got)} 条")
        for it in got:
            text = it["question"] + " " + it["answer"]
            if matcher(text):
                corpus_items.append(it)

    if not corpus_items:
        log("这轮没有命中词库的问答，没法取材，跳过")
        return 0
    corpus_items.sort(key=lambda x: x.get("ts") or 0, reverse=True)

    news = wscn_headlines(7) + huxiu_titles(30)
    heat_blocks, heat_summary = heat_from_state()
    log(f"新闻语料 {len(news)} 条；热度数据 {'有' if heat_summary else '无'}")

    qa_budget = int(CORPUS_CHARS * 0.6)          # 问答占 6 成、新闻占 4 成
    corpus, used = [], 0
    for it in corpus_items:
        piece = f"[{it['company']}] 问：{it['question']}\n答：{it['answer']}"
        if used + len(piece) > qa_budget:
            break
        corpus.append(piece)
        used += len(piece)
    log(f"取材 问答 {len(corpus)} 条（{used} 字）")

    cat_list = "\n".join(f"{cid} = {leaves.get(cid) or ids[cid]}" for cid in ids)
    user = ("【一】现有分类：\n" + cat_list
            + "\n\n【二】互动问答原文：\n" + ("\n---\n".join(corpus) or "（无）")
            + "\n\n【三】最近财经媒体要闻：\n" + ("\n".join(news) or "（没抓到，这次只靠问答判断）")
            + "\n\n【四】关键词命中数（看热度变化）：\n" + "\n".join(heat_blocks))

    try:
        parsed = json.loads(deepseek([{"role": "system", "content": PROMPT},
                                      {"role": "user", "content": user[:CORPUS_CHARS + 6000]}]))
    except Exception as e:
        log(f"模型调用/解析失败：{type(e).__name__}: {e}（不动文件）")
        return 0
    proposals = parsed.get("add") or []
    directions = [d for d in (parsed.get("directions") or [])
                  if isinstance(d, dict) and str(d.get("direction") or "").strip()][:4]
    note = str(parsed.get("note") or "").strip()[:200]
    log("模型提出 %d 个候选词、%d 个方向；观察：%s" % (len(proposals), len(directions), note or "（无）"))
    for d in directions:
        log("  方向 %s（%s）：%s" % (d.get("direction"), d.get("trend", "?"), str(d.get("evidence", ""))[:40]))

    corpus_low = ("\n".join(corpus) + "\n" + "\n".join(news)).lower()
    additions, rejected = {}, []
    for p in proposals:
        cid = str(p.get("category", "")).strip()
        kw = str(p.get("keyword", "")).strip().strip("，。、；：\"'（）()[]【】")
        why = str(p.get("why", ""))[:40]
        if not kw or not cid:
            continue
        if cid not in ids:
            rejected.append((kw, "分类不存在"))
            continue
        if kw in known:
            rejected.append((kw, "词库里已有"))
            continue
        if kw in TOO_GENERIC or len(kw) < 2 or len(kw) > 30:
            rejected.append((kw, "太泛或长度不合适"))
            continue
        if re.search(r"[\s]{2,}|https?://|[{}\[\]\\\"']", kw):
            rejected.append((kw, "含非法字符"))
            continue
        if kw.lower() not in corpus_low:      # 关键的防编造闸门
            rejected.append((kw, "语料里没出现"))
            continue
        additions.setdefault(cid, [])
        n_total = sum(len(v) for v in additions.values())
        if n_total >= MAX_NEW:
            rejected.append((kw, "超出本次上限"))
            continue
        additions[cid].append(kw)
        known.add(kw)
        log(f"  收下 {kw}  →  {leaves.get(cid) or ids[cid]}   （{why}）")

    for kw, why in rejected:
        log(f"  丢掉 {kw}（{why}）")

    added = add_keywords(data, additions) if not dry_run else [k for v in additions.values() for k in v]

    if dry_run:
        log(f"[试跑] 会新增 {len(added)} 个词：{'、'.join(added) or '（无）'}")
        log(f"[试跑] 会写入 heat（{len(heat_summary)} 周命中榜）+ weekly_note（方向 {len(directions)} 条）（不写文件）")
        return 0

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    data["updated_at"] = today
    if heat_summary:
        data["heat"] = {"generated_at": today, "weeks": heat_summary,
                        "source": "state.json 的 kw_hits：互动问答里各关键词的命中数，按 ISO 周"}
    if note or directions:
        data["weekly_note"] = {"date": today, "note": note, "directions": directions}
        hist = data.setdefault("weekly_notes", [])
        hist.append({"date": today, "note": note, "directions": directions, "added": added})
        data["weekly_notes"] = hist[-12:]        # 只留最近 12 周
    if added:
        chlog = data.setdefault("changelog", [])
        chlog.append({"date": today, "added": added, "source": "auto:update_keywords.py",
                      "note": "由每周任务从互动问答 + 新闻语料里补充；只增不删"})
        data["changelog"] = chlog[-12:]
    with open(DICT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"已写入 {DICT_PATH}：新增词 {len(added)} 个、本周方向 {len(directions)} 条、热度 {len(heat_summary)} 周")
    if added or directions or note:
        wecom_push(weekly_md(added, directions, note, heat_summary))
    else:
        log("这周没有可报的内容，不推送")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"致命错误：{type(e).__name__}: {e}（不动文件）")
        sys.exit(0)      # 失败也不让整个 Action 变红
