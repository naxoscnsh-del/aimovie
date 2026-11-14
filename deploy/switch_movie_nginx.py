#!/usr/bin/env python3
"""把 nginx 里 /movie 下原有的 location 全部换成 aimovie 的反代配置。

用法（root）：python3 switch_movie_nginx.py [配置文件] [端口]
默认：/etc/nginx/sites-enabled/ainewimage 8010

- 修改前把原文件备份到 /etc/nginx/backup/（不在 sites-enabled 里，不会被 nginx 加载）
- 删除所有 `location ... /movie...` 块（不动 `location = /_portal_gate_movie` 门户登录检查）
- 原 /movie/ 块里的 auth_request / auth_request_set / error_page 原样带到新配置，门户登录继续生效
- nginx -t 不通过则自动还原；通过则 reload
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOC_RE = re.compile(r"^\s*location\s+(?:\^~\s+|=\s+|~\*?\s+)?/movie(?:/|\s|\{|$)")
KEEP_DIRECTIVES = ("auth_request ", "auth_request_set ", "error_page ")


def block_end(lines: list[str], start: int) -> int:
    """从 location 行开始数大括号，返回块结束行的下标。"""
    depth = 0
    for i in range(start, len(lines)):
        line = re.sub(r"#.*", "", lines[i])
        line = re.sub(r"'[^']*'|\"[^\"]*\"", "", line)
        depth += line.count("{") - line.count("}")
        if depth == 0 and "}" in line:
            return i
    raise ValueError(f"第 {start + 1} 行开始的 location 块没有闭合")


def rewrite(text: str, port: int) -> tuple[str, int, list[str]]:
    lines = text.split("\n")
    out: list[str] = []
    removed = 0
    auth: list[str] = []
    insert_at = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if LOC_RE.match(line):
            end = block_end(lines, i)
            body = lines[i:end + 1]
            if re.match(r"^\s*location\s+\^~\s+/movie/\s*\{", line):  # 主 /movie/ 块：取登录拦截指令
                auth = [b.strip() for b in body[1:-1] if b.strip().startswith(KEEP_DIRECTIVES)]
            # 去掉紧挨在前面的说明注释（如 “# ==== huashu-motion studio ...”）
            while out and out[-1].strip().startswith("#") and "movie" in out[-1].lower():
                out.pop()
            if insert_at is None:
                insert_at = len(out)
                indent = re.match(r"^(\s*)", line).group(1)
            removed += 1
            i = end + 1
            continue
        out.append(line)
        i += 1
    if insert_at is None:
        raise ValueError("没找到 /movie 的 location，文件可能已经改过")
    ind = indent
    new = [
        f"{ind}# ==== aimovie（AI 短片工作台，反代 127.0.0.1:{port}）====",
        f"{ind}location = /movie {{ return 301 /movie/; }}",
        f"{ind}location ^~ /movie/ {{",
        *[f"{ind}    {a}" for a in auth],
        f"{ind}    proxy_pass http://127.0.0.1:{port};",
        f"{ind}    proxy_set_header Host $host;",
        f"{ind}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        f"{ind}    proxy_set_header X-Forwarded-Proto $scheme;",
        f"{ind}    proxy_buffering off;",
        f"{ind}    proxy_read_timeout 3600s;",
        f"{ind}    client_max_body_size 30m;",
        f"{ind}}}",
    ]
    out[insert_at:insert_at] = new
    text = re.sub(r"\n(\s*\n){2,}", "\n\n", "\n".join(out))  # 删块后留下的多余空行
    return text, removed, auth


def main() -> int:
    conf = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/nginx/sites-enabled/ainewimage")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8010
    original = conf.read_text(encoding="utf-8")
    new, removed, auth = rewrite(original, port)
    backup_dir = Path("/etc/nginx/backup")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{conf.name}.before-aimovie.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(conf, backup)
    conf.write_text(new, encoding="utf-8")
    print(f"已备份原配置：{backup}")
    print(f"删除 /movie 相关 location：{removed} 段；沿用登录拦截：{auth or '无'}")
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        shutil.copy2(backup, conf)
        print("✗ nginx -t 未通过，已还原原配置：\n" + test.stderr[-1500:])
        return 1
    subprocess.run(["nginx", "-s", "reload"], check=True)
    print("✓ nginx 已重新加载，/movie/ 现在指向 aimovie")
    return 0


if __name__ == "__main__":
    sys.exit(main())
