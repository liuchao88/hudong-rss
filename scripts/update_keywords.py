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

# 这些词太泛，塞进词库只会让 RSS 刷屏，模型提了也不收
TOO_GENERIC = {"发展", "业绩", "客户", "公司", "产品", "技术", "行业", "市场", "投资", "增长",
               "研发", "合作", "订单", "量产", "扩产", "涨价", "供应", "需求", "业务", "项目",
               "AI", "人工智能", "大模型", "服务器", "芯片", "存储", "机器人", "数据中心"}

PROMPT = """你是 AI 产业链投资情报编辑。下面给你两样东西：

【一】现有词库的分类清单（格式：分类id = 分类名）
【二】一批最近的 A股互动平台问答原文（都是已经命中现有词库的、AI 产业链相关的问答）

你的任务：从【二】的原文里找出**词库里还没有的、具体的行业术语**，把它们补进【一】的某个分类。
只要三类东西：
1. 产品/器件/材料的具体名称（例：硅光引擎、液冷板、铜连接、电子级氢氟酸）
2. 技术/工艺/型号/标准（例：CoWoS-L、3D 堆叠、浸没式液冷、CPO 交换机、800G DRAM）
3. 产业链环节的专用叫法（例：光引擎封装、晶圆再生、载板级封装）

不要提这些：泛泛的词（发展、业绩、客户、技术）、公司名、股票代码、一次性事件描述、
纯数字、英文通用词（New、AI、CPU 这种词库里已经有了）。

硬规则：
- 每个新词必须**原样出现在【二】的原文里**（不许自己造、不许改字）。语料里没有的词一律不要输出。
- 每个词只能归到一个分类，分类 id 必须来自【一】。
- 最多输出 20 个；宁少勿滥，有明显的才提，一个都没有就返回空数组。
- 只输出 JSON，不要任何解释文字，格式：
{"add": [{"category": "分类id", "keyword": "词", "why": "不超过15字的理由"}]}"""


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

    corpus, used = [], 0
    for it in corpus_items:
        piece = f"[{it['company']}] 问：{it['question']}\n答：{it['answer']}"
        if used + len(piece) > CORPUS_CHARS:
            break
        corpus.append(piece)
        used += len(piece)
    log(f"取材 {len(corpus)} 条（{used} 字）")

    cat_list = "\n".join(f"{cid} = {leaves.get(cid) or ids[cid]}" for cid in ids)
    user = f"【一】现有分类：\n{cat_list}\n\n【二】问答原文：\n" + "\n---\n".join(corpus)

    try:
        raw = deepseek([{"role": "system", "content": PROMPT}, {"role": "user", "content": user}])
        proposals = json.loads(raw).get("add") or []
    except Exception as e:
        log(f"模型调用/解析失败：{type(e).__name__}: {e}（不动文件）")
        return 0

    log(f"模型提出 {len(proposals)} 个候选")
    corpus_text = "\n".join(corpus)
    corpus_low = corpus_text.lower()
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
    if not added:
        log("没有通过校验的新词，文件保持不动")
        return 0

    if dry_run:
        log(f"[试跑] 会新增 {len(added)} 个：{'、'.join(added)}（不写文件）")
        return 0

    today = datetime.now(TZ).strftime("%Y-%m-%d")
    data["updated_at"] = today
    chlog = data.setdefault("changelog", [])
    chlog.append({"date": today, "added": added, "source": "auto:update_keywords.py",
                  "note": "由每周任务从互动问答语料里补充；只增不删"})
    data["changelog"] = chlog[-12:]
    with open(DICT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"已写入 {DICT_PATH}：新增 {len(added)} 个词（{'、'.join(added)}）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"致命错误：{type(e).__name__}: {e}（不动文件）")
        sys.exit(0)      # 失败也不让整个 Action 变红
