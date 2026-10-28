#!/usr/bin/env python3
"""
C++ Library Manager —— 本地收集服务

只依赖 Python 标准库 + 系统 git。职责：
  1. 把 C++ 库以 git submodule 形式收集到 ./external/<name>/
  2. 维护 ./data/libraries/libraries.json 这份"已收集"清单
  3. 给 web-interface.html 提供 REST 接口，供手动收集 / 更新 / 移除

用法:
    python server.py [--port 8080] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent import futures
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
EXTERNAL = ROOT / "external"
DATA_FILE = ROOT / "data" / "libraries" / "libraries.json"
CATALOG_FILE = ROOT / "data" / "catalog" / "recommended.json"
INDEX_FILE = ROOT / "web-interface.html"

GIT_TIMEOUT_CLONE = 900   # 大仓库克隆留足时间
GIT_TIMEOUT_FAST = 60

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_API = "https://api.github.com/search/repositories"

_URL_RE = re.compile(r"^(https?://|git://|ssh://|git@)", re.IGNORECASE)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_lock = threading.Lock()   # 写 data/libraries.json 的互斥锁


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    """服务端日志走 stderr，避免和 HTTP 输出混在一起。"""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def run_git(args: list[str], cwd: Path = ROOT, timeout: int = GIT_TIMEOUT_FAST):
    """执行 git 命令，永远返回 (returncode, stdout, stderr)，不抛异常。"""
    cmd = ["git", "-C", str(cwd)] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "找不到 git 命令，请先安装 Git 并加入 PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git 命令超时（{timeout}s）：git {' '.join(args)}"


def norm(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def git_available() -> tuple[bool, str]:
    code, out, err = run_git(["--version"], timeout=10)
    if code != 0:
        return False, err or "git 不可用"
    return True, out


def ensure_repo() -> str:
    """
    确保 ROOT 是一个独立 git 仓库。

    这是整个功能的前提：如果当前目录嵌在别的仓库里（例如它只是某个大仓的
    子目录），`git submodule add` 会把 .gitmodules 写到父仓库去，彻底跑偏。
    这里主动 `git init`，不做任何提交。
    """
    code, out, _ = run_git(["rev-parse", "--show-toplevel"])
    if code == 0 and out and norm(out) == norm(ROOT):
        return "ok"

    if code == 0 and out:
        log(f"当前目录嵌在仓库 {out} 中，正在为其单独 git init …")

    init_code, _, init_err = run_git(["init", "-q"])
    if init_code != 0:
        raise RuntimeError(f"git init 失败：{init_err}")
    return "initialized"


def sync_gitmodules() -> None:
    """让 .gitmodules 的索引状态与磁盘状态一致。

    移除子模块时会把变空的 .gitmodules 从磁盘删掉，但索引里仍留着「新增」
    记录（git status 显示 AD），下一次 `git submodule add` 就会报
    "please make sure that the .gitmodules file is in the working tree"。
    这里统一收敛两种不一致。
    """
    gm = ROOT / ".gitmodules"
    tracked = run_git(["ls-files", "--error-unmatch", "--", ".gitmodules"])[0] == 0

    if gm.exists():
        if gm.read_text(encoding="utf-8", errors="replace").strip():
            if not tracked:
                run_git(["add", ".gitmodules"])
        else:
            gm.unlink(missing_ok=True)
            run_git(["rm", "-q", "--cached", "--ignore-unmatch", "--", ".gitmodules"])
    elif tracked:
        run_git(["rm", "-q", "--cached", "--ignore-unmatch", "--", ".gitmodules"])


# --------------------------------------------------------------------------
# 数据读写
# --------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log(f"读取 {path} 失败：{exc}")
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def load_libraries() -> list[dict]:
    data = load_json(DATA_FILE, [])
    return data if isinstance(data, list) else []


def save_libraries(items: list[dict]) -> None:
    save_json(DATA_FILE, items)


# --------------------------------------------------------------------------
# 库元信息探测
# --------------------------------------------------------------------------

def detect_license(path: Path) -> str:
    for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING", "COPYING.txt", "LICENCE"):
        f = path / name
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            low = text.lower()
            if "apache license" in low:
                return "Apache-2.0"
            if "gnu lesser general public" in low:
                return "LGPL"
            if "gnu general public license" in low:
                return "GPL"
            if "mozilla public license" in low:
                return "MPL-2.0"
            if "mit license" in low or "permission is hereby granted, free of charge" in low:
                return "MIT"
            if "redistribution and use in source and binary forms" in low:
                return "BSD"
            if "the unlicense" in low:
                return "Unlicense"
            return "Other"
    return ""


def detect_score(path: Path) -> tuple[int, list[str]]:
    """离线可算的工程完整度评分（0-100），每项 20 分。"""
    score, hits = 0, []

    if any(path.glob("README*")):
        score += 20
        hits.append("README")

    if detect_license(path):
        score += 20
        hits.append("LICENSE")

    build_files = ["CMakeLists.txt", "meson.build", "Makefile", "configure.ac", "BUILD.bazel"]
    if any((path / f).exists() for f in build_files) or any(path.glob("*.cmake")):
        score += 20
        hits.append("构建脚本")

    ci_paths = [
        ".github/workflows", ".travis.yml", ".gitlab-ci.yml",
        "azure-pipelines.yml", "appveyor.yml", ".circleci",
    ]
    if any((path / p).exists() for p in ci_paths):
        score += 20
        hits.append("CI")

    test_dirs = ["test", "tests", "testing", "spec", "unittest"]
    if any((path / d).is_dir() for d in test_dirs):
        score += 20
        hits.append("测试目录")

    return score, hits


def detect_header_only(path: Path) -> bool:
    if (path / "single_include").is_dir():
        return True
    inc = path / "include"
    if inc.is_dir() and not (path / "src").is_dir():
        if any(inc.rglob("*.hpp")) or any(inc.rglob("*.h")):
            return True
    return False


def inspect_worktree(path: Path) -> dict:
    """读取一个已落盘子模块的实时状态。"""
    info = {"commit": "", "branch": "", "dirty": False}
    if not path.exists():
        return info

    code, out, _ = run_git(["rev-parse", "--short", "HEAD"], cwd=path, timeout=20)
    if code == 0:
        info["commit"] = out

    code, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path, timeout=20)
    if code == 0 and out != "HEAD":
        info["branch"] = out

    code, out, _ = run_git(["status", "--porcelain"], cwd=path, timeout=30)
    if code == 0:
        info["dirty"] = bool(out)

    return info


# --------------------------------------------------------------------------
# 收集 / 更新 / 移除
# --------------------------------------------------------------------------

def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("仓库地址不能为空")
    if url.startswith("-"):
        raise ValueError("仓库地址不能以 - 开头")
    if not _URL_RE.match(url):
        raise ValueError("仅支持 https:// 、http:// 、ssh:// 或 git@host:repo 形式的地址")
    if url.startswith("file://"):
        raise ValueError("出于安全考虑，不允许 file:// 协议")
    return url


def derive_name(url: str) -> str:
    tail = url.rstrip("/").replace(":", "/").split("/")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail


def sanitize_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise ValueError("库名称不能为空")
    if not _NAME_RE.match(name):
        raise ValueError("库名称只能包含字母、数字、点、下划线和连字符，且必须以字母或数字开头")
    if name in (".", ".."):
        raise ValueError("非法的库名称")
    return name


def submodule_path_of(rel: str) -> Path:
    target = (ROOT / rel).resolve()
    if EXTERNAL.resolve() not in target.parents:
        raise ValueError("非法的目标路径")
    return target


def cleanup_failed_add(rel: str) -> None:
    """`git submodule add` 失败后清理残留（目录、index 项、.gitmodules 段）。"""
    target = ROOT / rel
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    run_git(["rm", "-q", "--cached", "--ignore-unmatch", "--", rel])
    run_git(["config", "-f", ".gitmodules", "--remove-section", f"submodule.{rel}"])
    run_git(["config", "-f", ".gitmodules", "--remove-section", f'submodule."{rel}"'])
    sync_gitmodules()


def collect(payload: dict) -> dict:
    """核心动作：把一个远程仓库收成子模块。"""
    url = validate_url(payload.get("url", ""))
    name = sanitize_name(payload.get("name") or derive_name(url))
    branch = (payload.get("branch") or "").strip()

    if branch and branch.startswith("-"):
        raise ValueError("分支名非法")

    rel = f"external/{name}"
    target = submodule_path_of(rel)

    with _lock:
        existing = load_libraries()
        if any(item.get("name") == name for item in existing):
            raise ValueError(f"库里已经有同名条目：{name}")
    if target.exists():
        raise ValueError(f"目录已存在：{rel}，请换个名称或先移除")

    ensure_repo()
    sync_gitmodules()
    EXTERNAL.mkdir(parents=True, exist_ok=True)

    args = ["submodule", "add"]
    if branch:
        args += ["-b", branch]
    args += ["--", url, rel]

    log(f"收集 {url} -> {rel}")
    code, out, err = run_git(args, timeout=GIT_TIMEOUT_CLONE)

    if code != 0:
        cleanup_failed_add(rel)
        raise RuntimeError(err or out or "git submodule add 执行失败")

    # 探测元信息
    score, hits = detect_score(target)
    live = inspect_worktree(target)

    entry = {
        "id": name,
        "name": name,
        "url": url,
        "path": rel,
        "branch": live["branch"] or branch,
        "commit": live["commit"],
        "description": (payload.get("description") or "").strip(),
        "category": (payload.get("category") or "").strip(),
        "tags": [t.strip() for t in (payload.get("tags") or []) if str(t).strip()],
        "license": detect_license(target),
        "quality": score,
        "qualityHits": hits,
        "headerOnly": detect_header_only(target),
        "collectedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with _lock:
        items = load_libraries()
        items = [i for i in items if i.get("name") != name]
        items.append(entry)
        items.sort(key=lambda i: i.get("name", "").lower())
        save_libraries(items)

    return {"entry": entry, "log": "\n".join(x for x in (out, err) if x)}


def update_library(name: str) -> dict:
    name = sanitize_name(name)
    rel = f"external/{name}"
    target = submodule_path_of(rel)
    if not target.exists():
        raise ValueError(f"目录不存在：{rel}")

    code, out, err = run_git(
        ["submodule", "update", "--remote", "--merge", "--", rel],
        timeout=GIT_TIMEOUT_CLONE,
    )
    if code != 0:
        raise RuntimeError(err or out or "子模块更新失败")

    live = inspect_worktree(target)
    with _lock:
        items = load_libraries()
        for item in items:
            if item.get("name") == name:
                item["commit"] = live["commit"]
                if live["branch"]:
                    item["branch"] = live["branch"]
                item["updatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_libraries(items)

    return {"log": "\n".join(x for x in (out, err) if x), "commit": live["commit"]}


def remove_library(name: str) -> dict:
    name = sanitize_name(name)
    rel = f"external/{name}"
    target = submodule_path_of(rel)

    # 刻意不使用 `git submodule deinit`：它在 Windows 上要先对整个子模块工作树做
    # 一次清理，实测单个库要 60 秒以上；而我们紧接着就会把目录整个删掉，
    # 这一步纯属浪费。改为直接从索引摘除 gitlink + 手工清理配置。
    run_git(["rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", rel], timeout=120)
    run_git(["config", "-f", ".gitmodules", "--remove-section", f"submodule.{rel}"])
    run_git(["config", "-f", ".gitmodules", "--remove-section", f'submodule."{rel}"'])
    run_git(["config", "--remove-section", f"submodule.{rel}"])
    run_git(["config", "--remove-section", f'submodule."{rel}"'])

    modules_dir = ROOT / ".git" / "modules" / "external" / name
    if modules_dir.exists():
        shutil.rmtree(modules_dir, ignore_errors=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    sync_gitmodules()

    with _lock:
        items = [i for i in load_libraries() if i.get("name") != name]
        save_libraries(items)

    return {"removed": name}


# --------------------------------------------------------------------------
# 自动筛选：多数据源发现 + 规则判定
# --------------------------------------------------------------------------
#
# 数据源严格对齐 SKILL.md 的「库收集策略」三类来源：
#   GitHub 仓库  —— github
#   官方仓库      —— official（Boost / Google / STL 扩展）
#   包管理器      —— vcpkg、conan
#
# 刻意不收录 awesome-cpp 之类的二手清单：它不在策略里，条目本身也指向 GitHub，
# 只是少了 star / 活跃度 / 许可证，拿它当源等于用残缺数据重复 GitHub 已有的结果。
#
# apt、Brew 也没有纳入：它们分发的是二进制包而不是可克隆仓库，
# 无法以 git submodule 形式收进 external/，与"收集"目标不符。

VCPKG_BASELINE_URL = "https://raw.githubusercontent.com/microsoft/vcpkg/master/versions/baseline.json"
VCPKG_PORT_URL = "https://raw.githubusercontent.com/microsoft/vcpkg/master/ports/{name}/vcpkg.json"
CONAN_ROOT_TREE_URL = "https://api.github.com/repos/conan-io/conan-center-index/git/trees/master"
CONAN_TREE_URL = "https://api.github.com/repos/conan-io/conan-center-index/git/trees/{sha}"
CONAN_RECIPE_URL = "https://raw.githubusercontent.com/conan-io/conan-center-index/master/recipes/{name}/all/conandata.yml"
# conandata.yml 只有源码地址，没有许可证和描述；这些在 conanfile.py 里。
CONAN_CONANFILE_URL = "https://raw.githubusercontent.com/conan-io/conan-center-index/master/recipes/{name}/all/conanfile.py"
OFFICIAL_FILE = ROOT / "data" / "catalog" / "official.json"

SOURCE_LABELS = {
    "github": "GitHub 搜索",
    "vcpkg": "vcpkg 端口库",
    "conan": "Conan Center",
    "official": "官方 / 标准扩展",
}

# 来源权威度，作为评分的一个维度（满分 15）
SOURCE_TRUST = {"official": 15, "vcpkg": 10, "conan": 10, "github": 5}

GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")

_cache: dict = {}
_cache_lock = threading.Lock()


# ---- 通用网络 ----

def http_get(url: str, timeout: int = 45, as_json: bool = False):
    headers = {"User-Agent": "cpp-library-manager", "Accept": "application/json, text/plain, */*"}
    if GITHUB_TOKEN and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            meta = {"remaining": resp.headers.get("X-RateLimit-Remaining")}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        if exc.code == 403:
            raise RuntimeError(
                f"GitHub API 限流或拒绝访问（{detail or '403'}）。"
                "未认证时搜索接口额度很低，可设置环境变量 GITHUB_TOKEN 提高。"
            )
        if exc.code == 404:
            raise RuntimeError("资源不存在（404）")
        raise RuntimeError(f"HTTP {exc.code}：{detail or exc.reason}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络不可达：{exc.reason}")
    except TimeoutError:
        raise RuntimeError("请求超时，请检查网络或代理")

    text = raw.decode("utf-8", "replace")
    return (json.loads(text), meta) if as_json else (text, meta)


def cached(key: str, ttl: int, producer):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


# ---- URL 归一 ----

def is_git_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in GIT_HOSTS)


def repo_key(url: str) -> str:
    u = (url or "").strip().split("#")[0].split("?")[0].rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    if u.startswith("http://"):
        u = "https://" + u[7:]
    if u.lower().startswith("https://www."):
        u = "https://" + u[12:]
    return u.lower()


def repo_name(url: str) -> str:
    u = (url or "").rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    tail = u.replace(":", "/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip("-")


def clone_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith(".git"):
        return u
    return u + ".git"


def archive_to_repo(url: str) -> str:
    """把下载归档地址还原成仓库根地址。"""
    m = re.match(r"^(https?://(?:github\.com|gitlab\.com|bitbucket\.org)/[^/]+/[^/]+)", url or "")
    if not m:
        return ""
    root = m.group(1)
    return root[:-4] if root.endswith(".git") else root


def _days_since(iso: str):
    if not iso:
        return None
    try:
        ts = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return max(0, (datetime.utcnow() - ts).days)


def _py_field(text: str, field: str) -> str:
    """从 conanfile.py 里抠一个字段（license / description / homepage）。

    CCI 的 license 有两种写法：`license = "MIT"` 和
    `license = ("IJG", "BSD-3-Clause", "Zlib")`，后者按 SPDX 的 AND 拼起来。
    """
    text = text or ""
    m = re.search(rf'^\s*{field}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    m = re.search(rf'^\s*{field}\s*=\s*\(([^)]*)\)', text, re.MULTILINE | re.DOTALL)
    if m:
        parts = [p.strip() for p in re.findall(r'"([^"]*)"', m.group(1))]
        return " AND ".join(p for p in parts if p)
    return ""


# ---- 许可证 ----
# 只做展示与归一，不做筛选判定。

def clean_license(expr: str) -> str:
    """把 GitHub 的占位值归一成空串，避免被当成"有许可证"加分。"""
    value = str(expr or "").strip()
    if value.upper() in ("NOASSERTION", "NONE", "UNKNOWN", "OTHER"):
        return ""
    return value


# ---- 各数据源 ----

def build_search_query(keywords: str, topics: list[str]) -> str:
    """只保留"范围"类条件。

    star 数和更新时间刻意不写进查询：写进去会让 GitHub 直接少返回结果，
    界面上只看到一个缩水的数字，用户无法判断是"库里没有"还是"被条件筛掉了"。
    这两个条件统一放到 screen_candidate 里逐条判定，命中的总数照实显示。
    """
    parts = ["language:C++", "archived:false", "fork:false"]
    if keywords:
        parts.append(keywords)
    for topic in topics:
        parts.append(f"topic:{topic}")
    return " ".join(parts)


def source_github(p: dict):
    query = build_search_query(p["keywords"], p["topics"])
    url = f"{GITHUB_API}?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={p['limit']}"
    body, meta = http_get(url, as_json=True)

    out = []
    for it in body.get("items") or []:
        out.append({
            "source": "github",
            "name": it.get("name") or "",
            "fullName": it.get("full_name") or "",
            "url": it.get("clone_url") or "",
            "homepage": it.get("html_url") or "",
            "description": it.get("description") or "",
            "license": ((it.get("license") or {}) or {}).get("spdx_id") or "",
            "topics": it.get("topics") or [],
            "stars": int(it.get("stargazers_count") or 0),
            "forks": int(it.get("forks_count") or 0),
            "updatedDaysAgo": _days_since(it.get("pushed_at") or ""),
            "sizeKb": int(it.get("size") or 0),
            "branch": it.get("default_branch") or "",
            "category": "",
            "archived": bool(it.get("archived")),
            "fork": bool(it.get("fork")),
        })
    return out, {
        "query": query,
        "rateLimitRemaining": meta.get("remaining"),
        "totalCount": int(body.get("total_count") or len(out)),
        "fetched": len(out),
    }



def source_vcpkg(p: dict):
    if not p["match_tokens"]:
        return [], {"note": "请至少填写关键词或 topic"}

    baseline = cached(
        "vcpkg-baseline",
        3600,
        lambda: http_get(VCPKG_BASELINE_URL, as_json=True)[0],
    )
    ports = (baseline.get("default") or {})
    toks = p["match_tokens"]
    matched = [n for n in ports if any(t in n.lower() for t in toks)]
    matched = sorted(matched)[: max(p["limit"], 12) * 2]

    def fetch(name):
        try:
            data = http_get(VCPKG_PORT_URL.format(name=name), timeout=25, as_json=True)[0]
            return name, data
        except Exception:  # noqa: BLE001
            return name, None

    out = []
    with futures.ThreadPoolExecutor(max_workers=12) as pool:
        for name, data in pool.map(fetch, matched):
            if not data:
                continue
            home = data.get("homepage") or ""
            if not is_git_host(home):
                continue
            lic = data.get("license") or ""
            if isinstance(lic, list):
                lic = " AND ".join(str(x) for x in lic)
            out.append({
                "source": "vcpkg",
                "name": repo_name(home) or name,
                "fullName": name,
                "url": clone_url(home),
                "homepage": home,
                "description": data.get("description") or "",
                "license": lic,
                "topics": [],
                "stars": None,
                "forks": None,
                "updatedDaysAgo": None,
                "sizeKb": 0,
                "branch": "",
                "category": "",
                "version": data.get("version") or data.get("version-semver") or "",
                "archived": False,
                "fork": False,
            })
    return out, {"ports": len(ports), "matched": len(matched)}


def source_conan(p: dict):
    if not p["match_tokens"]:
        return [], {"note": "请至少填写关键词或 topic"}

    root, _ = cached("conan-root", 3600, lambda: http_get(CONAN_ROOT_TREE_URL, as_json=True))
    recipes_sha = next((e["sha"] for e in root.get("tree", []) if e.get("path") == "recipes"), None)
    if not recipes_sha:
        raise RuntimeError("无法定位 conan-center-index 的 recipes 目录")

    tree, _ = cached(
        f"conan-recipes:{recipes_sha}",
        3600,
        lambda: http_get(CONAN_TREE_URL.format(sha=recipes_sha), as_json=True),
    )
    names = [e["path"] for e in tree.get("tree", []) if e.get("type") == "tree"]

    toks = p["match_tokens"]
    matched = [n for n in names if any(t in n.lower() for t in toks)]
    matched = sorted(matched)[: max(p["limit"], 12) * 2]

    def fetch(name):
        try:
            text = http_get(CONAN_RECIPE_URL.format(name=name), timeout=25)[0]
        except Exception:  # noqa: BLE001
            return name, "", ""
        try:
            meta = http_get(CONAN_CONANFILE_URL.format(name=name), timeout=25)[0]
        except Exception:  # noqa: BLE001
            meta = ""
        return name, text, meta

    out = []
    with futures.ThreadPoolExecutor(max_workers=12) as pool:
        for name, text, meta in pool.map(fetch, matched):
            if not text:
                continue
            urls = re.findall(r"url:\s*\"?([^\"\s]+)\"?", text)
            repo = ""
            for u in urls:
                repo = archive_to_repo(u)
                if repo:
                    break
            if not repo:
                # conandata.yml 里拿不到时，退回到 conanfile.py 的 homepage。
                repo = _py_field(meta, "homepage")
            if not repo or not is_git_host(repo):
                continue
            out.append({
                "source": "conan",
                "name": repo_name(repo),
                "fullName": name,
                "url": clone_url(repo),
                "homepage": repo,
                "description": _py_field(meta, "description"),
                "license": clean_license(_py_field(meta, "license")),
                "topics": [],
                "stars": None,
                "forks": None,
                "updatedDaysAgo": None,
                "sizeKb": 0,
                "branch": "",
                "category": "",
                "archived": False,
                "fork": False,
            })
    return out, {"recipes": len(names), "matched": len(matched)}


def source_official(p: dict):
    entries = load_json(OFFICIAL_FILE, [])
    if not isinstance(entries, list):
        return [], {}

    toks = p["match_tokens"]
    out = []
    for e in entries:
        haystack = " ".join([e.get("name", ""), e.get("description", ""), e.get("category", "")]).lower()
        if toks and not any(t in haystack for t in toks):
            continue
        out.append({
            "source": "official",
            "name": e.get("name") or repo_name(e.get("url", "")),
            "fullName": e.get("fullName") or e.get("name", ""),
            "url": clone_url(e.get("url", "")),
            "homepage": e.get("url", ""),
            "description": e.get("description", ""),
            "license": e.get("license", ""),
            "topics": e.get("tags") or [],
            "stars": None,
            "forks": None,
            "updatedDaysAgo": None,
            "sizeKb": 0,
            "branch": "",
            "category": e.get("category", ""),
            "archived": False,
            "fork": False,
        })
    return out, {"entries": len(entries)}


SOURCE_FUNCS = {
    "github": source_github,
    "vcpkg": source_vcpkg,
    "conan": source_conan,
    "official": source_official,
}


# ---- 合并 / 评分 / 判定 ----

def merge_candidates(raw: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in raw:
        # 先归一许可证，否则 GitHub 的 NOASSERTION 会挡住 vcpkg/conan 给出的真实值
        item = dict(item)
        item["license"] = clean_license(item.get("license", ""))

        key = repo_key(item.get("url", ""))
        if not key:
            continue
        if key not in merged:
            entry = dict(item)
            entry["sources"] = [item["source"]]
            merged[key] = entry
            continue

        base = merged[key]
        if item["source"] not in base["sources"]:
            base["sources"].append(item["source"])
        for field in ("description", "license", "category", "branch", "homepage", "fullName"):
            if not base.get(field) and item.get(field):
                base[field] = item[field]
        if base.get("stars") is None and item.get("stars") is not None:
            base["stars"] = item["stars"]
            base["forks"] = item.get("forks")
            base["updatedDaysAgo"] = item.get("updatedDaysAgo")
            base["sizeKb"] = item.get("sizeKb", 0)
        if not base.get("topics") and item.get("topics"):
            base["topics"] = item["topics"]
    for entry in merged.values():
        entry["license"] = clean_license(entry.get("license", ""))
    return list(merged.values())


def compute_score(c: dict) -> tuple[int, list[str]]:
    """0-100，对齐 SKILL.md 的质量评估维度；数据缺失时记入 gaps。"""
    gaps = []
    score = 0

    stars = c.get("stars")
    if stars is None:
        gaps.append("star 数未知")
    elif stars >= 5000:
        score += 25
    elif stars >= 3000:
        score += 23
    elif stars >= 1000:
        score += 20
    elif stars >= 500:
        score += 15
    elif stars >= 100:
        score += 10
    else:
        score += 5

    days = c.get("updatedDaysAgo")
    if days is None:
        gaps.append("活跃度未知")
    elif days <= 90:
        score += 25
    elif days <= 180:
        score += 20
    elif days <= 365:
        score += 15
    else:
        score += 5

    if c.get("license"):
        score += 20
    else:
        gaps.append("许可证未知")

    if len(c.get("description") or "") >= 20:
        score += 15

    # 同一库命中多个源时，取最高权威度
    sources = c.get("sources") or [c.get("source", "")]
    score += max((SOURCE_TRUST.get(s, 5) for s in sources), default=5)
    return min(score, 100), gaps


def tier_of(score: int, gaps: list[str]) -> str:
    """SKILL.md 的三级分档；star 数据缺失时不硬判为"需要改进"。"""
    if any(g.startswith("star") for g in gaps):
        return "数据不足"
    if score >= 80:
        return "优秀"
    if score >= 60:
        return "良好"
    return "需要改进"


def screen_candidate(c: dict, criteria: dict) -> dict:
    failures, warnings = [], []

    if c.get("archived"):
        failures.append("仓库已归档")
    if c.get("fork"):
        failures.append("是 fork 仓库")

    stars = c.get("stars")
    if criteria["minStars"]:
        if stars is None:
            warnings.append("star 数未知，未参与门槛判定")
        elif stars < criteria["minStars"]:
            failures.append(f"star {stars} 低于门槛 {criteria['minStars']}")

    days = c.get("updatedDaysAgo")
    if criteria["updatedWithinDays"]:
        if days is None:
            warnings.append("活跃度未知，未参与判定")
        elif days > criteria["updatedWithinDays"]:
            failures.append(f"已 {days} 天未更新（上限 {criteria['updatedWithinDays']} 天）")

    # 许可证只做展示，不参与筛选：GPL / 未知许可证的库同样会列出来，
    # 由使用者自己判断能不能用。
    size = c.get("sizeKb") or 0
    if criteria["maxSizeKb"] and size > criteria["maxSizeKb"]:
        failures.append(f"体积 {size}KB 超过上限 {criteria['maxSizeKb']}KB")

    score, gaps = compute_score(c)

    return {
        "name": c.get("name", ""),
        "fullName": c.get("fullName") or c.get("name", ""),
        "url": c.get("url", ""),
        "homepage": c.get("homepage", ""),
        "description": c.get("description", ""),
        "category": c.get("category", ""),
        "license": c.get("license", ""),
        "topics": c.get("topics") or [],
        "branch": c.get("branch", ""),
        "stars": stars,
        "forks": c.get("forks"),
        "updatedDaysAgo": days,
        "sizeKb": size,
        "sources": c.get("sources", [c.get("source", "")]),
        "score": score,
        "tier": tier_of(score, gaps),
        "gaps": gaps,
        "warnings": warnings,
        "passed": not failures,
        "failures": failures,
    }


def discover(payload: dict) -> dict:
    keywords = (payload.get("keywords") or "").strip()
    topics = [str(t).strip() for t in (payload.get("topics") or []) if str(t).strip()]
    if not keywords and not topics:
        raise ValueError("请至少填写关键词或一个 topic")

    sources = [s for s in (payload.get("sources") or list(SOURCE_LABELS)) if s in SOURCE_FUNCS]
    if not sources:
        raise ValueError("请至少选择一个数据源")

    match_tokens = [t for t in re.split(r"[\s,]+", keywords.lower()) if t]
    # topic 也参与包名/描述匹配：只填 topic 时，vcpkg / Conan / 官方清单才不至于
    # 因为"没有关键词"而整体放行或整体跳过。
    for t in topics:
        token = t.lower()
        if token and token not in match_tokens:
            match_tokens.append(token)

    params = {
        "keywords": keywords,
        "topics": topics,
        "match_tokens": match_tokens,
        "minStars": max(0, int(payload.get("minStars") or 0)),
        "updatedWithinDays": max(0, int(payload.get("updatedWithinDays") or 0)),
        "limit": min(100, max(1, int(payload.get("limit") or 30))),
        "maxSizeKb": max(0, int(payload.get("maxSizeKb") or 0)),
    }

    raw, per_source, errors, notes, metas = [], {}, [], {}, {}
    for name in sources:
        try:
            items, meta = SOURCE_FUNCS[name](params)
            per_source[name] = len(items)
            metas[name] = meta or {}
            if meta and meta.get("note"):
                notes[name] = meta["note"]
            raw.extend(items)
        except Exception as exc:  # noqa: BLE001
            per_source[name] = 0
            errors.append(f"{SOURCE_LABELS[name]}：{exc}")
            log(f"数据源 {name} 失败：{exc}")

    merged = merge_candidates(raw)
    collected = {str(i.get("name", "")).lower() for i in load_libraries()}

    candidates = []
    for item in merged:
        cand = screen_candidate(item, params)
        cand["alreadyCollected"] = cand["name"].lower() in collected
        candidates.append(cand)

    candidates.sort(key=lambda c: (not c["passed"], -c["score"], c["name"]))

    return {
        "query": build_search_query(params["keywords"], topics),
        "total": len(candidates),
        "passedCount": sum(1 for c in candidates if c["passed"]),
        "criteria": {k: params[k] for k in ("minStars", "updatedWithinDays", "maxSizeKb")},
        "perSource": per_source,
        "sourceLabels": {k: SOURCE_LABELS[k] for k in per_source},
        "meta": metas,
        "notes": notes,
        "errors": errors,
        "authenticated": bool(GITHUB_TOKEN),
        "candidates": candidates,
    }


# --------------------------------------------------------------------------
# 视图
# --------------------------------------------------------------------------

def libraries_view() -> list[dict]:
    """已收集列表：以 json 为准，叠加磁盘上的实时状态。"""
    items = load_libraries()
    out = []
    for item in items:
        rel = item.get("path") or f"external/{item.get('name')}"
        target = ROOT / rel
        live = inspect_worktree(target) if target.exists() else {"commit": "", "branch": "", "dirty": False}
        merged = dict(item)
        merged["exists"] = target.exists()
        merged["commit"] = live["commit"] or item.get("commit", "")
        merged["branch"] = live["branch"] or item.get("branch", "")
        merged["dirty"] = live["dirty"]
        out.append(merged)
    return out


def status_view() -> dict:
    ok, version = git_available()
    repo_root, is_own = "", False
    if ok:
        code, out, _ = run_git(["rev-parse", "--show-toplevel"], timeout=10)
        if code == 0:
            repo_root = out
            is_own = norm(out) == norm(ROOT)

    return {
        "gitAvailable": ok,
        "gitVersion": version,
        "projectRoot": str(ROOT),
        "repoRoot": repo_root,
        "isOwnRepo": is_own,
        "collected": len(load_libraries()),
        "catalog": len(load_json(CATALOG_FILE, [])),
        "hasGitmodules": (ROOT / ".gitmodules").exists(),
        "githubAuthenticated": bool(GITHUB_TOKEN),
    }


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "CppLibManager/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 输出 ----
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _ok(self, data=None, log_text: str = "") -> None:
        self._json(200, {"ok": True, "data": data, "log": log_text})

    def _err(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    # ---- 解析 ----
    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    @staticmethod
    def _route(path: str) -> list[str]:
        return [unquote(p) for p in urlparse(path).path.strip("/").split("/") if p]

    # ---- GET ----
    def do_GET(self) -> None:
        parts = self._route(self.path)
        try:
            if parts == ["api", "status"]:
                return self._ok(status_view())
            if parts == ["api", "libraries"]:
                return self._ok(libraries_view())
            if parts == ["api", "catalog"]:
                return self._ok(load_json(CATALOG_FILE, []))
            if not parts or parts in (["web-interface.html"], ["index.html"]):
                return self._serve_index()
            if parts == ["favicon.ico"]:
                return self._send(204, b"", "image/x-icon")
            return self._err(404, f"未知路径：/{'/'.join(parts)}")
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            return self._err(500, str(exc))

    def _serve_index(self) -> None:
        if not INDEX_FILE.exists():
            return self._err(500, "找不到 web-interface.html")
        body = INDEX_FILE.read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    # ---- POST ----
    def do_POST(self) -> None:
        parts = self._route(self.path)
        try:
            if parts == ["api", "collect"]:
                payload = self._body()
                result = collect(payload)
                return self._ok(result["entry"], result["log"])

            if parts == ["api", "discover"]:
                return self._ok(discover(self._body()))

            # /api/libraries/<name>/update
            if len(parts) == 4 and parts[:2] == ["api", "libraries"] and parts[3] == "update":
                result = update_library(parts[2])
                return self._ok({"commit": result["commit"]}, result["log"])

            return self._err(404, "未知接口")
        except ValueError as exc:
            return self._err(400, str(exc))
        except RuntimeError as exc:
            return self._err(500, str(exc))
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            return self._err(500, str(exc))

    # ---- DELETE ----
    def do_DELETE(self) -> None:
        parts = self._route(self.path)
        try:
            if len(parts) == 3 and parts[:2] == ["api", "libraries"]:
                result = remove_library(parts[2])
                return self._ok(result)
            return self._err(404, "未知接口")
        except ValueError as exc:
            return self._err(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            return self._err(500, str(exc))

    # ---- 日志 ----
    def log_message(self, fmt: str, *args) -> None:
        log("%s - %s" % (self.address_string(), fmt % args))


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="C++ Library Manager 本地收集服务")
    parser.add_argument("--port", type=int, default=int(os.environ.get("GUI_PORT", 8080)))
    parser.add_argument("--host", default=os.environ.get("GUI_HOST", "127.0.0.1"))
    args = parser.parse_args()

    ok, version = git_available()
    if not ok:
        print(f"[错误] {version}", file=sys.stderr)
        print("请先安装 Git：https://git-scm.com/downloads", file=sys.stderr)
        return 1

    try:
        state = ensure_repo()
    except RuntimeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    if state == "initialized":
        log("已为当前目录初始化独立 git 仓库（未做任何提交）")

    log(f"git: {version}")
    log(f"项目根目录: {ROOT}")
    log(f"已收集 {len(load_libraries())} 个库")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 56)
    print("  C++ Library Manager 已启动")
    print(f"  打开浏览器访问: {url}")
    print("  Ctrl+C 停止")
    print("=" * 56, flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
