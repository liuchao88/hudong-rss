#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股互动问答监控：深交所互动易 + 上证e互动 → 关键词过滤 → RSS
运行环境：GitHub Actions (Ubuntu)，每 10 分钟一次
数据源：
  1. 深交所互动易  POST https://irm.cninfo.com.cn/newircs/index/search (keyWord 为空 = 全市场最新)
  2. 上证e互动     GET  https://sns.sseinfo.com/ajax/feeds.do?type=11 (最新答复流)
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED_DIR = os.path.join(BASE_DIR, "feed")
RSS_PATH = os.path.join(FEED_DIR, "rss.xml")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
KEYWORDS_PATH = os.path.join(BASE_DIR, "keywords.txt")
MAX_ITEMS = 300          # RSS 最多保留条数
MAX_PAGES = 2            # 每个平台翻几页
PAGE_SIZE = 50           # 每页条数

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TZ = timezone(timedelta(hours=8))  # 北京时间

FEED_TITLE = "A股互动问答关键词监控"
FEED_LINK = "https://github.com/liuchao88/a-share-qna-watch"
FEED_DESC = "深交所互动易 + 上证e互动 董秘回答关键词监控（自动生成）"


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://sns.sseinfo.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_post(url, form_data, timeout=30):
    body = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://irm.cninfo.com.cn/ircs/index",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------- 深交所互动易 ----------
def fetch_szse():
    """全市场最新回答流（JSON），翻 MAX_PAGES 页"""
    items = []
    for page in range(1, MAX_PAGES + 1):
        try:
            raw = http_post("https://irm.cninfo.com.cn/newircs/index/search",
                            {"keyWord": "", "pageNo": page, "pageSize": PAGE_SIZE})
            data = json.loads(raw)
        except Exception as e:
            log(f"互动易第{page}页失败: {e}")
            break
        for r in data.get("results", []):
            q = (r.get("mainContent") or "").strip()
            a = (r.get("attachedContent") or "").strip()
            if not q or not a:
                continue
            ts_ms = r.get("attachedPubDate") or 0
            items.append({
                "guid": "szse_" + str(r.get("indexId", "")),
                "platform": "深交所互动易",
                "company": (r.get("companyShortName") or "").strip(),
                "code": (r.get("stockCode") or "").strip(),
                "question": q,
                "answer": a,
                "ts": int(ts_ms) / 1000 if ts_ms else 0,
                "link": f"https://irm.cninfo.com.cn/ircs/question/questionDetail?questionId={r.get('indexId', '')}",
            })
        time.sleep(0.5)
    return items


# ---------- 上证e互动 ----------
def parse_relative_time(s, now_ts):
    """解析'刚刚/N分钟前/N小时前/昨天 HH:MM/MM-DD HH:MM' → 时间戳，失败返回 0"""
    s = s.strip()
    if not s:
        return 0
    if s == "刚刚":
        return int(now_ts)
    m = re.match(r"(\d+)\s*分钟前", s)
    if m:
        return int(now_ts) - int(m.group(1)) * 60
    m = re.match(r"(\d+)\s*小时前", s)
    if m:
        return int(now_ts) - int(m.group(1)) * 3600
    m = re.match(r"昨天\s*(\d{1,2}):(\d{2})", s)
    if m:
        t = datetime.now(TZ) - timedelta(days=1)
        return int(t.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0).timestamp())
    m = re.match(r"(\d{2})-(\d{2})\s*(\d{1,2}):(\d{2})", s)
    if m:
        y = datetime.now(TZ).year
        return int(datetime(y, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))).timestamp())
    return 0


def clean_sse_tail(s):
    """清理互动平台页面残留的操作按钮文字"""
    s = re.sub(r"\s*\|?\s*收藏\s*\|?\s*评论.*$", "", s)
    s = re.sub(r"--+>?\s*$", "", s)
    s = re.sub(r"[◆●]+", "", s)
    s = re.sub(r"请登录后再(点赞|收藏)!?", "", s)
    return s.strip()


def parse_sse_html(raw_html):
    """解析 feeds.do 返回的 HTML，提取问答条目"""
    items = []
    now_ts = time.time()
    blocks = re.findall(r'<div class="m_feed_item[^"]*" id="item-(\d+)">(.*?)(?=<div class="m_feed_item|$)', raw_html, re.S)
    for iid, body in blocks:
        text = re.sub(r"<[^>]+>", " ", body)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        # 拆出公司名和代码：某某公司(600166)
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]+)\((\d{6})\)", text)
        company, code = (m.group(1), m.group(2)) if m else ("", "")
        # 提取回答时间（相对时间），提问时间在问题文本里保留
        tm = re.search(r"(刚刚|\d+\s*分钟前|\d+\s*小时前|昨天\s*\d{1,2}:\d{2}|\d{2}-\d{2}\s*\d{1,2}:\d{2})", text)
        ts = parse_relative_time(tm.group(1), now_ts) if tm else 0
        # 问题部分：第一个"来自"之前的内容，去掉"投资者_xxx :"前缀
        q_part = text.split("来自")[0]
        q_part = re.sub(r"^投资者_\d+\s*[:：]?\s*", "", q_part).strip()
        # 回答部分：从第二个公司名出现处开始（第一个是问题里的引用）
        a_part = text
        if company:
            first = a_part.find(company)
            second = a_part.find(company, first + 1)
            if second > 0:
                a_part = a_part[second + len(company):]
        # 去掉开头的 ◆ 标记和相对时间/来源
        a_part = re.sub(r"^\s*[◆●]+\s*", "", a_part)
        a_part = re.sub(r"^(刚刚|\d+\s*分钟前|\d+\s*小时前|昨天\s*\d{1,2}:\d{2}|\d{2}-\d{2}\s*\d{1,2}:\d{2})\s*来自\s*\S+", "", a_part)
        a_part = clean_sse_tail(a_part)
        if not q_part or not a_part:
            continue
        items.append({
            "guid": "sse_" + iid,
            "platform": "上证e互动",
            "company": company,
            "code": code,
            "question": q_part,
            "answer": a_part,
            "ts": ts,
            "link": f"https://sns.sseinfo.com/qaDetail.do?stockcode={code}&id={iid}" if code else "",
        })
    return items


def fetch_sse():
    items = []
    for page in range(1, MAX_PAGES + 1):
        try:
            url = f"https://sns.sseinfo.com/ajax/feeds.do?page={page}&type=11&pageSize={PAGE_SIZE}&lastid=-1&show=1"
            raw = http_get(url)
            page_items = parse_sse_html(raw)
            items.extend(page_items)
        except Exception as e:
            log(f"上证e互动第{page}页失败: {e}")
        time.sleep(0.5)
    return items


# ---------- 关键词 ----------
def load_keywords():
    kws = []
    try:
        with open(KEYWORDS_PATH, encoding="utf-8") as f:
            for line in f:
                kw = line.strip()
                if kw and not kw.startswith("#"):
                    kws.append(kw)
    except FileNotFoundError:
        log("keywords.txt 不存在，创建默认关键词")
        with open(KEYWORDS_PATH, "w", encoding="utf-8") as f:
            f.write("# 每行一个关键词，修改后下次运行自动生效\n光模块\n存储\n人形机器人\n")
        kws = ["光模块", "存储", "人形机器人"]
    return kws


def match_keywords(text, kws):
    low = text.lower()
    return [kw for kw in kws if kw.lower() in low]


# ---------- 去重 ----------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": []}


def load_existing_guids():
    """从现有 rss.xml 提取 guid 集合"""
    guids = set()
    if os.path.exists(RSS_PATH):
        raw = open(RSS_PATH, encoding="utf-8").read()
        guids = set(re.findall(r"<guid>(.*?)</guid>", raw))
    return guids


# ---------- RSS ----------
def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def build_rss(items):
    now = datetime.now(TZ).strftime("%a, %d %b %Y %H:%M:%S %z")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{xml_escape(FEED_TITLE)}</title>",
        f"<link>{xml_escape(FEED_LINK)}</link>",
        f"<description>{xml_escape(FEED_DESC)}</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items:
        if it["ts"]:
            dt = datetime.fromtimestamp(it["ts"], TZ)
            pub = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
        else:
            pub = now
        kw_hit = "、".join(it.get("kws", []))
        title = f"[{it['platform']}][{it['company']}{it['code']}][{kw_hit}] {it['question'][:50]}"
        desc = (f"<b>{it['platform']}</b> | {it['company']} ({it['code']}) | 命中关键词: {kw_hit}<br/>"
                f"<b>问:</b> {xml_escape(it['question'])}<br/>"
                f"<b>答:</b> {xml_escape(it['answer'])}")
        parts.append("<item>")
        parts.append(f"<title>{xml_escape(title)}</title>")
        parts.append(f"<link>{xml_escape(it['link'])}</link>")
        parts.append(f"<guid isPermaLink=\"false\">{xml_escape(it['guid'])}</guid>")
        parts.append(f"<pubDate>{pub}</pubDate>")
        parts.append(f"<description>{desc}</description>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "\n".join(parts)


# ---------- 主流程 ----------
def main():
    kws = load_keywords()
    log(f"关键词: {kws}")
    state = load_state()
    seen = set(state.get("seen", []))
    seen |= load_existing_guids()

    new_items = []
    for platform, fetcher in [("深交所互动易", fetch_szse), ("上证e互动", fetch_sse)]:
        log(f"抓取 {platform} ...")
        for it in fetcher():
            if it["guid"] in seen:
                continue
            text = it["question"] + "\n" + it["answer"] + "\n" + it["company"]
            hit = match_keywords(text, kws)
            if hit:
                it["kws"] = hit
                new_items.append(it)
                seen.add(it["guid"])

    new_items.sort(key=lambda x: x["ts"] or time.time(), reverse=True)
    log(f"新增命中 {len(new_items)} 条")

    # 保留 state 中最近 2000 个 id（防膨胀）
    state["seen"] = list(seen)[-2000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    if not new_items:
        log("无新条目，不更新 RSS")
        return

    os.makedirs(FEED_DIR, exist_ok=True)
    # 解析已有 RSS 条目（保留，防重复）
    old_items = []
    if os.path.exists(RSS_PATH):
        raw = open(RSS_PATH, encoding="utf-8").read()
        for block in re.findall(r"<item>.*?</item>", raw, re.S):
            g = re.search(r"<guid[^>]*>(.*?)</guid>", block)
            if not g or g.group(1) in {n["guid"] for n in new_items}:
                continue
            t = re.search(r"<title>(.*?)</title>", block, re.S)
            d = re.search(r"<description>(.*?)</description>", block, re.S)
            p = re.search(r"<pubDate>(.*?)</pubDate>", block)
            lk = re.search(r"<link>(.*?)</link>", block, re.S)
            old_items.append({
                "guid": g.group(1), "title": t.group(1) if t else "", "desc": d.group(1) if d else "",
                "pub": p.group(1) if p else "", "link": lk.group(1) if lk else "",
            })
    # 合并：新条目在前 + 旧条目（限 MAX_ITEMS）
    merged = new_items + old_items
    merged = merged[:MAX_ITEMS]
    # 构建 RSS
    now = datetime.now(TZ).strftime("%a, %d %b %Y %H:%M:%S %z")
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
           f"<title>{xml_escape(FEED_TITLE)}</title>", f"<link>{xml_escape(FEED_LINK)}</link>",
           f"<description>{xml_escape(FEED_DESC)}</description>", f"<lastBuildDate>{now}</lastBuildDate>"]
    for it in merged:
        if "kws" in it:  # 新条目（原始文本）
            out.append(build_item_xml(it, now))
        else:            # 旧条目（已转义，原样输出）
            out.append("<item>")
            out.append(f"<title>{it['title']}</title>")
            out.append(f"<link>{it['link']}</link>")
            out.append(f"<guid isPermaLink=\"false\">{it['guid']}</guid>")
            out.append(f"<pubDate>{it.get('pub', now)}</pubDate>")
            out.append(f"<description>{it['desc']}</description>")
            out.append("</item>")
    out.append("</channel></rss>")
    with open(RSS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    log(f"RSS 已更新: {RSS_PATH} (共 {len(merged)} 条)")


def build_item_xml(it, now):
    if it["ts"]:
        dt = datetime.fromtimestamp(it["ts"], TZ)
        pub = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
    else:
        pub = now
    kw_hit = "、".join(it.get("kws", []))
    title = f"[{it['platform']}][{it['company']}{it['code']}][{kw_hit}] {it['question'][:50]}"
    desc = (f"<b>{it['platform']}</b> | {it['company']} ({it['code']}) | 命中关键词: {kw_hit}<br/>"
            f"<b>问:</b> {xml_escape(it['question'])}<br/>"
            f"<b>答:</b> {xml_escape(it['answer'])}")
    return ("<item>\n"
            f"<title>{xml_escape(title)}</title>\n"
            f"<link>{xml_escape(it['link'])}</link>\n"
            f"<guid isPermaLink=\"false\">{xml_escape(it['guid'])}</guid>\n"
            f"<pubDate>{pub}</pubDate>\n"
            f"<description>{desc}</description>\n"
            "</item>")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"致命错误: {e}")
        sys.exit(1)
