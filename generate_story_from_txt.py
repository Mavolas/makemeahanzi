#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从纯文本文案生成「多页」手写动画，每页两行（跳过空行），每页调用 generate_animated_text.py，
最后生成 index.html 用 iframe 按时间轴连续播放全部页。

用法示例：
  python3 generate_story_from_txt.py
    （默认文案 + 生成 HTML 后自动调用 export_mp4_from_html.js 导出 MP4）
  python3 generate_story_from_txt.py --no-export-mp4
    （只生成 page_*.html / index.html / story_meta.json，不导出视频）
  python3 generate_story_from_txt.py --out-dir story_好运靠近
  python3 generate_story_from_txt.py 其它文案.txt --out-dir out -- --speed 2 --char-size 140
  python3 generate_story_from_txt.py --mp4-out 成片.mp4

「--」 后面的参数会原样传给 generate_animated_text.py。
story_meta.json 的每页时长与 generate_animated_text 内笔画块解析一致；若需「慢速对照」可传「-- --speed 1」。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from generate_animated_text import estimate_phrase_html_duration_seconds

# 不传文案路径时的默认文件（与脚本同目录，即仓库根）
_DEFAULT_TXT_NAME = "文案7 好运靠近 短.txt"

# 页与页之间：当前页播完后淡出时长（秒），再加载下一页
_FADE_OUT_SECONDS = 1.0

def load_non_empty_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def lines_to_pages_two_per_page(lines: list[str]) -> list[str]:
    """两行为一页；若最后一页只有一行，单独成页。"""
    pages: list[str] = []
    i = 0
    while i < len(lines):
        if i + 1 < len(lines):
            pages.append(lines[i] + "\n" + lines[i + 1])
            i += 2
        else:
            pages.append(lines[i])
            i += 1
    return pages


def estimate_page_duration_seconds(html: str) -> float:
    return estimate_phrase_html_duration_seconds(html, fallback_seconds=3.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="文案按两页一行分页，连续生成手写动画并汇总 index.html")
    parser.add_argument(
        "txt_path",
        nargs="?",
        default=None,
        help=f"文案 txt 路径（省略则使用本仓库「{_DEFAULT_TXT_NAME}」）",
    )
    parser.add_argument(
        "--out-dir",
        default="story_output",
        help="输出目录（将写入 page_001.html … 与 index.html）",
    )
    parser.add_argument(
        "--page-padding",
        type=float,
        default=0.35,
        help="每页播完后到下一页之间的间隔（秒）",
    )
    parser.add_argument(
        "--no-export-mp4",
        action="store_true",
        help="不自动导出 MP4（默认会在生成 HTML 后调用 export_mp4_from_html.js）",
    )
    parser.add_argument(
        "--export-mp4",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--mp4-out",
        default=None,
        help="MP4 输出路径（默认：<--out-dir>/story.mp4）",
    )
    parser.add_argument(
        "--mp4-fps",
        default=None,
        help="已废弃：当前导出为浏览器 recordVideo（约 25fps），不再传此参数",
    )
    parser.add_argument(
        "--mp4-width",
        default=None,
        help="传给 export 的 --width（省略则用脚本默认）",
    )
    parser.add_argument(
        "--mp4-height",
        default=None,
        help="传给 export 的 --height（省略则用脚本默认）",
    )
    parser.add_argument(
        "--mp4-show-bar",
        action="store_true",
        help="导出时保留 index 底部进度栏（默认导出会 --hide-bar）",
    )
    parser.add_argument(
        "gen_args",
        nargs=argparse.REMAINDER,
        help="传给 generate_animated_text.py 的额外参数（前面请加 --）",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    if args.txt_path:
        txt_path = Path(args.txt_path).expanduser().resolve()
    else:
        txt_path = (repo_root / _DEFAULT_TXT_NAME).resolve()
    if not txt_path.is_file():
        raise SystemExit(f"找不到文案文件：{txt_path}")

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    gen_script = repo_root / "generate_animated_text.py"
    if not gen_script.is_file():
        raise SystemExit(f"找不到 {gen_script}")

    lines = load_non_empty_lines(txt_path)
    if not lines:
        raise SystemExit("文案里没有非空行")

    pages = lines_to_pages_two_per_page(lines)
    print(f"共 {len(lines)} 行非空文案 -> {len(pages)} 页")

    # 传给子进程的额外参数（argparse REMAINDER 可能包含开头的 '--'，去掉）
    extra = list(args.gen_args or [])
    if extra and extra[0] == "--":
        extra = extra[1:]

    page_files: list[str] = []
    durations: list[float] = []

    for idx, phrase in enumerate(pages, start=1):
        name = f"page_{idx:03d}.html"
        out_html = out_dir / name
        cmd = [
            sys.executable,
            str(gen_script),
            phrase,
            "--out",
            str(out_html),
            *extra,
        ]
        print(f"[{idx}/{len(pages)}] 生成 {name} …")
        subprocess.run(cmd, cwd=str(repo_root), check=True)

        html = out_html.read_text(encoding="utf-8")
        dur = estimate_page_duration_seconds(html) + args.page_padding
        durations.append(dur)
        page_files.append(name)

    total_content = sum(durations)
    # 页间淡出 n-1 次，最后一页播完直接结束，不再淡出
    fade_count = max(0, len(durations) - 1)
    total_with_fade = total_content + fade_count * _FADE_OUT_SECONDS
    meta = {
        "pages": page_files,
        "durations": durations,
        "total_content_sec": round(total_content, 3),
        "total_with_fade_sec": round(total_with_fade, 3),
    }
    (out_dir / "story_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # index.html：与 page_*.html 同目录，用相对路径
    index_html = out_dir / "index.html"
    index_html.write_text(
        _build_index_html(page_files, durations, fade_out_seconds=_FADE_OUT_SECONDS),
        encoding="utf-8",
    )

    print(
        f"完成：共 {len(pages)} 页，总时长 ≈{total_content:.1f}s"
        f"（含页间淡出 ≈{total_with_fade:.1f}s，最后一页无淡出）"
    )
    print(f"打开连续播放：{index_html}")

    if not args.no_export_mp4:
        export_js = repo_root / "export_mp4_from_html.js"
        if not export_js.is_file():
            raise SystemExit(f"自动导出需要存在 {export_js}")
        node = shutil.which("node")
        if not node:
            raise SystemExit("未在 PATH 中找到 node，无法导出 MP4（可加 --no-export-mp4 跳过）")
        mp4_path = (
            Path(args.mp4_out).expanduser().resolve()
            if args.mp4_out
            else (out_dir / "story.mp4")
        )
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            node,
            str(export_js),
            "--story",
            str(out_dir),
            "--out",
            str(mp4_path),
        ]
        if args.mp4_width:
            cmd.extend(["--width", str(args.mp4_width)])
        if args.mp4_height:
            cmd.extend(["--height", str(args.mp4_height)])
        if not args.mp4_show_bar:
            cmd.append("--hide-bar")
        print("正在导出 MP4 …")
        print(" ", " ".join(cmd))
        t0 = time.perf_counter()
        try:
            subprocess.run(cmd, cwd=str(repo_root), check=True)
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"导出 MP4 失败（退出码 {e.returncode}），可先 --no-export-mp4 只生成 HTML") from e
        elapsed = time.perf_counter() - t0
        size_mb = mp4_path.stat().st_size / (1024 * 1024)
        print(
            f"MP4 导出成功：{mp4_path}\n"
            f"  耗时 {elapsed:.1f}s  约 {size_mb:.2f} MiB"
        )


def _build_index_html(
    pages: list[str],
    durations: list[float],
    *,
    fade_out_seconds: float = _FADE_OUT_SECONDS,
) -> str:
    pages_json = json.dumps(pages, ensure_ascii=False)
    durs_json = json.dumps(durations)
    fade_ms = max(1, int(round(fade_out_seconds * 1000)))
    fade_css_js = json.dumps(f"{fade_out_seconds:g}s")
    return f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>连续手写动画</title>
  <style>
    html, body {{
      margin: 0;
      height: 100%;
      overflow: hidden;
      background: #fff;
    }}
    #stage {{
      position: relative;
      width: 100%;
      height: 100%;
      background: #fff;
    }}
    #view {{
      width: 100%;
      height: 100%;
      border: 0;
      display: block;
    }}
    /* 盖在 iframe 上：由透明渐变为不透明白，视觉上为浅蓝画布与字「溶入」白底，而非露出外框深色 */
    #whiteFade {{
      position: absolute;
      inset: 0;
      background: #fff;
      opacity: 0;
      pointer-events: none;
    }}
    #bar {{
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      padding: 10px 16px;
      background: rgba(0,0,0,0.75);
      color: #fff;
      font: 14px/1.4 system-ui, sans-serif;
      pointer-events: none;
    }}
  </style>
</head>
<body>
  <div id="stage">
    <iframe id="view" title="当前页"></iframe>
    <div id="whiteFade" aria-hidden="true"></div>
  </div>
  <div id="bar"></div>
  <script>
    const pages = {pages_json};
    const durs = {durs_json};
    const FADE_MS = {fade_ms};
    const FADE_CSS = {fade_css_js};
    const bar = document.getElementById('bar');
    const view = document.getElementById('view');
    const whiteFade = document.getElementById('whiteFade');
    let timer = null;
    let fadeTimer = null;

    function clearTimers() {{
      if (timer) {{
        clearTimeout(timer);
        timer = null;
      }}
      if (fadeTimer) {{
        clearTimeout(fadeTimer);
        fadeTimer = null;
      }}
    }}

    function setBar(text) {{
      bar.textContent = text;
    }}

    function show(i) {{
      clearTimers();
      if (i >= pages.length) {{
        setBar('全部播完（共 ' + pages.length + ' 页）');
        whiteFade.style.transition = 'none';
        whiteFade.style.opacity = '0';
        view.removeAttribute('src');
        return;
      }}
      setBar('第 ' + (i + 1) + ' / ' + pages.length + ' 页（约 ' + durs[i].toFixed(2) + 's）');
      view.onload = function () {{
        whiteFade.style.transition = 'none';
        whiteFade.style.opacity = '0';
        const ms = Math.max(100, Math.round(durs[i] * 1000));
        timer = setTimeout(function () {{
          timer = null;
          const isLastPage = i + 1 >= pages.length;
          if (isLastPage) {{
            show(i + 1);
            return;
          }}
          whiteFade.style.transition = 'opacity ' + FADE_CSS + ' ease-out';
          void whiteFade.offsetWidth;
          whiteFade.style.opacity = '1';
          fadeTimer = setTimeout(function () {{
            fadeTimer = null;
            show(i + 1);
          }}, FADE_MS);
        }}, ms);
      }};
      view.src = pages[i];
    }}

    show(0);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
