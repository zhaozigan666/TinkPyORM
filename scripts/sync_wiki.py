#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库内 ``wiki/`` 同步为 GitHub Wiki 页面（可重复、可校验、失败即报错）。

GitHub Wiki 是**独立 git 仓库**（``<repo>.wiki.git``），与主仓库不共享文件树，
所以不能直接把 ``wiki/`` 复制过去：链接形态与"哪些文件可见"都不同。本脚本：

1. **改写链接**（唯一必需的语义变换，见 :func:`rewrite_link`）：

   ====================================  ==========================================
   源（仓库内相对路径）                    目标（wiki 仓库内）
   ====================================  ==========================================
   ``NN-主题.md``                          ``NN-主题``（Gollum 页面 URL 无扩展名）
   ``Home.md``                             ``Home``
   ``../README.md`` / ``../PERFORMANCE.md``  绝对 blob URL
   ``../docs/X.md``                        ``{REPO}/blob/{BRANCH}/docs/X.md``
   ``../docs``                             ``{REPO}/tree/{BRANCH}/docs``
   ====================================  ==========================================

   出现**未登记**的链接形态会直接报错退出，避免新增链接时静默产生死链。

2. 生成 ``_Sidebar.md``（从 ``Home.md`` 的分组表格提取，分组与顺序自动跟随）
   与 ``_Footer.md``（GitHub Wiki 会对每页统一渲染这两个文件）。

3. **校验零丢失**：页面齐全、内部链接可达、无残留 ``../``、代码围栏数量一致、
   正文行多重集一致（仅链接目标允许变化）。

用法::

    python scripts/sync_wiki.py --out ../TinkPyORM.wiki          # 仅生成 + 校验
    python scripts/sync_wiki.py --out ../TinkPyORM.wiki --push   # 再提交推送

``--out`` 目录若不存在会被创建并 ``git init``；已存在则视为 wiki 仓库工作区。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

REPO = "https://github.com/zhaozigan666/TinkPyORM"
BRANCH = "main"
WIKI_REMOTE = REPO + ".wiki.git"

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.abspath(os.path.join(HERE, os.pardir, "wiki"))

LINK_RE = re.compile(r"\]\(([^)]+)\)")
PAGE_RE = re.compile(r"^(\d\d-[^/]+)\.md$")
GROUP_RE = re.compile(r"^## (.+?)\s*$")
ROW_RE = re.compile(r"^\|\s*\[([^\]]+)\]\(([^)]+\.md)\)\s*\|")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

#: 侧边栏里跳过的小节（属于仓库结构说明，不是技术文档页）
SKIP_GROUPS = {"文档结构约定"}


def rewrite_link(target: str) -> str:
    """把仓库内相对链接改写为 wiki 仓库可解析的形式。未知形态直接抛错。"""
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return target
    if target == "Home.md":
        return "Home"
    if PAGE_RE.match(target):
        return target[:-3]                      # 去掉 .md
    if target.startswith("../"):
        rest = target[3:]
        if rest in ("README.md", "PERFORMANCE.md"):
            return "{}/blob/{}/{}".format(REPO, BRANCH, rest)
        if rest.rstrip("/") == "docs":
            return "{}/tree/{}/docs".format(REPO, BRANCH)
        if rest.startswith("docs/"):
            return "{}/blob/{}/{}".format(REPO, BRANCH, rest)
        raise SystemExit("sync_wiki: 未登记的仓库内链接 {!r}（请补 rewrite_link 规则）"
                         .format(target))
    raise SystemExit("sync_wiki: 未登记的链接形态 {!r}（请补 rewrite_link 规则）"
                     .format(target))


def transform(text: str) -> str:
    """逐行改写链接；围栏代码块内原样保留（行切片比块拼接更不易丢换行）。"""
    out = []
    fenced = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        out.append(line if fenced else LINK_RE.sub(
            lambda m: "](" + rewrite_link(m.group(1)) + ")", line))
    return "\n".join(out)


def parse_home(home_text: str):
    """从 Home.md 抽取 [(分组名, [(显示文字, 目标), ...]), ...]，保持原顺序。"""
    groups, cur = [], None
    for line in home_text.split("\n"):
        g = GROUP_RE.match(line)
        if g:
            cur = (g.group(1).strip(), [])
            groups.append(cur)
            continue
        r = ROW_RE.match(line)
        if r and cur is not None:
            cur[1].append((r.group(1).replace("`", ""), r.group(2)))
    return [(title, rows) for title, rows in groups if rows]


def build_sidebar(home_text: str) -> str:
    lines = ["**TinkPyORM 技术文档**", "", "- [索引](Home)"]
    for title, rows in parse_home(home_text):
        if title in SKIP_GROUPS:
            continue
        lines += ["", "**{}**".format(title)]
        lines += ["- [{}]({})".format(label, rewrite_link(target))
                  for label, target in rows]
    return "\n".join(lines) + "\n"


def build_footer() -> str:
    return ("TinkPyORM · [源码仓库]({}) · [README]({}/blob/{}/README.md) · "
            "本页由仓库内 `wiki/` 自动同步\n").format(REPO, REPO, BRANCH)


def count_fences(text: str) -> int:
    return sum(1 for line in text.split("\n") if FENCE_RE.match(line))


def mask_links(text: str) -> str:
    """把链接目标抹平，用于比较"除链接目标外"的正文是否逐字一致。"""
    return LINK_RE.sub("]()", text)


def sync(src_dir: str, out_dir: str) -> None:
    if not os.path.isdir(src_dir):
        raise SystemExit("sync_wiki: 源目录不存在: " + src_dir)

    pages = sorted(f for f in os.listdir(src_dir) if f.endswith(".md"))
    if not pages:
        raise SystemExit("sync_wiki: 源目录没有 .md 页面: " + src_dir)

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    keep = {".git", "_Sidebar.md", "_Footer.md"}
    for name in os.listdir(out_dir):
        if name not in keep:
            path = os.path.join(out_dir, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)

    for name in pages:
        with open(os.path.join(src_dir, name), "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(transform(text))

    home = os.path.join(src_dir, "Home.md")
    with open(home, "r", encoding="utf-8") as fh:
        home_text = fh.read()
    with open(os.path.join(out_dir, "_Sidebar.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_sidebar(home_text))
    with open(os.path.join(out_dir, "_Footer.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_footer())

    problems = verify(src_dir, out_dir, pages)
    if problems:
        print("校验未通过：")
        for p in problems:
            print("  -", p)
        raise SystemExit(1)
    print("已生成：{} 页 + _Sidebar.md + _Footer.md".format(len(pages)))
    print("校验通过：链接改写完整、内部链接可达、正文零丢失")


def verify(src_dir: str, out_dir: str, pages) -> list:
    problems = []

    # [1] 无残留的仓库内相对链接
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(out_dir, name), "r", encoding="utf-8") as fh:
            body = fh.read()
        for m in LINK_RE.finditer(body):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue          # 绝对 URL 的 blob 链接必须保留 .md 后缀
            if target.startswith("../") or target.startswith("./"):
                problems.append("{} 残留相对链接 {}".format(name, target))
            if target.endswith(".md"):
                problems.append("{} 目标仍带 .md 后缀：{}".format(name, target))

        # [2] 内部链接可达
        for m in LINK_RE.finditer(body):
            target = m.group(1).split("#")[0]
            if target.startswith(("http://", "https://", "mailto:", "#")) or not target:
                continue
            if not os.path.exists(os.path.join(out_dir, target + ".md")):
                problems.append("{} 内部链接不可达：{}".format(name, target))

    # [3] 页面齐全 + 代码围栏数量一致 + 正文（除链接目标）逐字一致
    for name in pages:
        with open(os.path.join(src_dir, name), "r", encoding="utf-8") as fh:
            src = fh.read()
        dst_path = os.path.join(out_dir, name)
        if not os.path.exists(dst_path):
            problems.append("缺少页面：" + name)
            continue
        with open(dst_path, "r", encoding="utf-8") as fh:
            dst = fh.read()
        if count_fences(src) != count_fences(dst):
            problems.append("{} 代码围栏数量不一致 {} -> {}".format(
                name, count_fences(src), count_fences(dst)))
        if Counter(mask_links(src).split("\n")) != Counter(mask_links(dst).split("\n")):
            problems.append("{} 正文行多重集不一致（除链接目标外应逐字相同）".format(name))

    # [4] 侧边栏链接均可达
    with open(os.path.join(out_dir, "_Sidebar.md"), "r", encoding="utf-8") as fh:
        sidebar = fh.read()
    for m in LINK_RE.finditer(sidebar):
        target = m.group(1)
        if target.startswith(("http://", "https://")):
            continue
        if not os.path.exists(os.path.join(out_dir, target + ".md")):
            problems.append("_Sidebar.md 链接不可达：" + target)

    return problems


def run_git(out_dir: str, *args: str, check: bool = True):
    proc = subprocess.run(["git"] + list(args), cwd=out_dir,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit("sync_wiki: git {} 失败\n{}\n{}".format(
            " ".join(args), proc.stdout, proc.stderr))
    return proc


def push(out_dir: str) -> None:
    if not os.path.isdir(os.path.join(out_dir, ".git")):
        run_git(out_dir, "init", "-b", "master")
        run_git(out_dir, "remote", "add", "origin", WIKI_REMOTE)
    remotes = run_git(out_dir, "remote").stdout.split()
    if "origin" not in remotes:
        run_git(out_dir, "remote", "add", "origin", WIKI_REMOTE)

    run_git(out_dir, "add", "-A")
    staged = run_git(out_dir, "diff", "--cached", "--quiet", check=False)
    if staged.returncode != 0:
        run_git(out_dir, "commit", "-m",
                "docs(wiki): 由主仓库 wiki/ 同步生成\n\n"
                "来源: {}/tree/{}/wiki".format(REPO, BRANCH))
    else:
        print("内容无变化，跳过提交")

    branch = run_git(out_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for target in (branch if branch != "HEAD" else "master", "master", "main"):
        proc = run_git(out_dir, "push", "-u", "origin", "HEAD:" + target, check=False)
        if proc.returncode == 0:
            print("已推送 -> {} (分支 {})".format(WIKI_REMOTE, target))
            return
        last = proc.stderr.strip()
    raise SystemExit("sync_wiki: 推送失败（wiki 仓库可能尚未创建）\n" + last)


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 wiki/ 到 GitHub Wiki")
    ap.add_argument("--out", required=True, help="wiki 仓库工作区目录")
    ap.add_argument("--src", default=SRC_DIR, help="源目录（默认 仓库根/wiki）")
    ap.add_argument("--push", action="store_true", help="生成后提交并推送")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    sync(os.path.abspath(args.src), out_dir)
    if args.push:
        push(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
