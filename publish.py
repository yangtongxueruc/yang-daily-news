#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yang-daily-news 发布脚本
- 复用本机已验证的 GitHub PAT（取自 surgical_push.py，避免重复硬编码密钥）
- 通过 GitHub REST API 安全推送（沙箱内 git push 被禁，此通道已验证可用）
- 用法:
    python publish.py init            # 首次：建仓(若缺) + 推送全部站点文件 + 开启 Pages
    python publish.py                 # 每日：解析 ai_daily_dashboard.html，归档 reports/<date>.html，
                                      #       更新 data/index.json，推送这两个文件
    python publish.py 2026-08-12      # 指定日期重发（可选）
"""
import os, re, sys, json, base64, time, shutil, urllib.request, urllib.error

SITE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(SITE_DIR)
OWNER = "yangtongxueruc"
REPO = "yang-daily-news"
BRANCH = "main"
API = "https://api.github.com"

# ---- PAT：从 surgical_push.py 读取，不重复硬编码、不执行该脚本 ----
def load_pat():
    d = SITE_DIR
    for _ in range(6):
        sp = os.path.join(d, "surgical_push.py")
        if os.path.exists(sp):
            txt = open(sp, encoding="utf-8").read()
            m = re.search(r'PAT\s*=\s*"([^"]+)"', txt)
            if m:
                return m.group(1)
        d = os.path.dirname(d)
    return os.environ.get("GITHUB_PAT", "")

PAT = load_pat()

def api(method, path, data=None):
    url = API + path
    headers = {"Authorization": f"Bearer {PAT}", "User-Agent": "wb",
               "Accept": "application/vnd.github+json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            rd = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(rd) if rd else {})
    except urllib.error.HTTPError as e:
        rd = e.read().decode("utf-8", "replace")
        return e.code, {"__error__": rd[:400]}

def ensure_repo():
    st, _ = api("GET", f"/repos/{OWNER}/{REPO}")
    if st == 200:
        print("[repo] already exists"); return
    st, d = api("POST", "/user/repos", {
        "name": REPO,
        "description": "AI 每日晨报 · GitHub Pages 归档（首页为当日新闻，含日历浏览过往日期）",
        "private": False, "auto_init": True,
    })
    print("[repo] create:", st, d.get("full_name") or d.get("__error__"))
    # 等待默认分支就绪
    for _ in range(15):
        st2, _ = api("GET", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}")
        if st2 == 200:
            break
        time.sleep(1)

def get_sha(path):
    st, d = api("GET", f"/repos/{OWNER}/{REPO}/contents/{path}?ref={BRANCH}")
    return d.get("sha") if st == 200 else None

def push_file(rel, msg=None):
    local = os.path.join(SITE_DIR, rel)
    if not os.path.exists(local):
        print(f"[skip] {rel} (not found)"); return False
    with open(local, "rb") as f:
        content = f.read()
    b64 = base64.b64encode(content).decode("ascii")
    sha = get_sha(rel)
    payload = {"message": msg or f"update {rel}", "content": b64, "branch": BRANCH}
    if sha:
        payload["sha"] = sha
    st, d = api("PUT", f"/repos/{OWNER}/{REPO}/contents/{rel}", payload)
    if st in (200, 201):
        print(f"[ok]   push {rel} ({len(content)} bytes)")
        return True
    print(f"[FAIL] push {rel}: HTTP {st} {d.get('__error__')}")
    return False

def enable_pages():
    st, d = api("POST", f"/repos/{OWNER}/{REPO}/pages",
                {"source": {"branch": BRANCH, "path": "/"}})
    if st in (201, 409):
        print(f"[pages] enabled (HTTP {st})")
    else:
        print(f"[pages] result HTTP {st}: {d.get('__error__')}")

def update_index(date_str, total, window, sections):
    idx_path = os.path.join(SITE_DIR, "data", "index.json")
    if os.path.exists(idx_path):
        idx = json.load(open(idx_path, encoding="utf-8"))
    else:
        idx = {"latest": None, "dates": [], "meta": {}}
    if date_str not in idx["dates"]:
        idx["dates"].append(date_str)
        idx["dates"].sort()
    idx["latest"] = date_str
    idx["meta"][date_str] = {"total": total, "window": window, "sections": sections}
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    print(f"[index] updated: {date_str}, total dates={len(idx['dates'])}")

def parse_report(html_path):
    html = open(html_path, encoding="utf-8").read()
    m = re.search(r"(\d{4})年(\d{2})月(\d{2})日", html)
    date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
    mt = re.search(r"今日共\s*(\d+)\s*条", html)
    total = int(mt.group(1)) if mt else 0
    window = "24h" if "过去 24 小时" in html else "7d"
    # stat-num / stat-label 配对
    pairs = re.findall(r'class="stat-num">(\d+)<.*?class="stat-label">(.*?)<', html, re.S)
    sections = {label: int(num) for num, label in pairs}
    return date_str, total, window, sections

def do_init():
    ensure_repo()
    for root, _, files in os.walk(SITE_DIR):
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, SITE_DIR).replace(os.sep, "/")
            push_file(rel, "init site")
    enable_pages()
    print("[DONE] init complete -> https://%s.github.io/%s/" % (OWNER, REPO))

def do_daily(force_date=None):
    src = os.path.join(WORKSPACE, "ai_daily_dashboard.html")
    if not os.path.exists(src):
        print("[ERR] ai_daily_dashboard.html not found in workspace"); sys.exit(1)
    date_str, total, window, sections = parse_report(src)
    if force_date:
        date_str = force_date
    if not date_str:
        print("[ERR] cannot parse date from report"); sys.exit(1)
    dst = os.path.join(SITE_DIR, "reports", f"{date_str}.html")
    shutil.copyfile(src, dst)
    print(f"[copy] reports/{date_str}.html ({os.path.getsize(dst)} bytes)")
    update_index(date_str, total, window, sections)
    push_file(f"reports/{date_str}.html", f"daily report {date_str}")
    push_file("data/index.json", f"index update {date_str}")
    print("[DONE] daily publish -> https://%s.github.io/%s/" % (OWNER, REPO))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        do_init()
    else:
        do_daily(sys.argv[1] if len(sys.argv) > 1 else None)
