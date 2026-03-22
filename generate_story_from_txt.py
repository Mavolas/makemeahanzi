#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从纯文本文案生成「多页」手写动画，每页两行（跳过空行），每页调用 generate_animated_text.py，
最后生成 index.html 用 iframe 按时间轴连续播放全部页。
未在「--」后指定 --canvas-bg 时，整套 story 共用随机淡色背景（每页相同）；指定 --canvas-bg 则全 story 用该色。

不传文案路径时：扫描仓库根下「wenan」目录内全部 .txt，逐个打印摘要（文件名、行数、约多少页、前几行），
再只问一次「每个文稿统一生成多少套」。
默认目录结构（在 path_config.BASE_DIR/中间文本/ 下）：
  · HTML：{文案名}_草稿/（多套时其下再有 {文案名}_01、_02 …）
  · MP4：{文案名}_成稿/；默认按「橱窗」首次出现页在该页时长中点时的累计时刻命名（含页间淡出），
    如 文案13_惊天反击-03+40+13 (1).mp4；冲突自动 (2)(3)…；可用 --no-story-mp4-time-keyword 恢复旧命名。

用法示例：
  python3 generate_story_from_txt.py
  python3 generate_story_from_txt.py --wenan-all 20
  python3 generate_story_from_txt.py --no-export-mp4
  python3 generate_story_from_txt.py 其它文案.txt --out-dir out -- --speed 2 --char-size 140
  python3 generate_story_from_txt.py --wenan-dir 我的文案夹

「--」 后面的参数会原样传给 generate_animated_text.py。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from generate_animated_text import estimate_phrase_html_duration_seconds
from backgrounds import pick_random_canvas_background

_WENAN_DIR_NAME = "wenan"


def _gen_extra_has_canvas_bg(extra: list[str]) -> bool:
    """是否在传给 generate_animated_text.py 的参数里已指定画布背景。"""
    for tok in extra:
        if tok == "--canvas-bg":
            return True
        if tok.startswith("--canvas-bg=") and len(tok) > len("--canvas-bg="):
            return True
    return False


def _default_output_root() -> Path:
    """默认输出：path_config.BASE_DIR / STORY_OUTPUT_SUBDIR；失败则用仓库内 story_output。"""
    repo_root = Path(__file__).resolve().parent
    try:
        from path_config import BASE_DIR, STORY_OUTPUT_SUBDIR

        root = Path(BASE_DIR).expanduser() / STORY_OUTPUT_SUBDIR
        return root.resolve()
    except Exception:
        return (repo_root / "story_output").resolve()
_FADE_OUT_SECONDS = 1.0

# 成稿 MP4：默认用「关键词首次出现页」在整段播放到该页时长中点时的累计时刻（含页间淡出）命名
_DEFAULT_STORY_MP4_TIME_KEYWORD = "橱窗"
_DEFAULT_STORY_MP4_TIME_SUFFIX = "13"

_HOMEBREW_FFMPEG_CANDIDATES = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)


def resolve_ffmpeg_for_mp4_export() -> str | None:
    """供 Node 导出 MP4 使用：尊重已有 FFMPEG_PATH，其次 PATH，再试 Homebrew 常见路径。"""
    env_p = os.environ.get("FFMPEG_PATH")
    if env_p:
        p = Path(env_p).expanduser()
        if p.is_file():
            return str(p.resolve())
    w = shutil.which("ffmpeg")
    if w:
        return w
    for c in _HOMEBREW_FFMPEG_CANDIDATES:
        cp = Path(c)
        if cp.is_file():
            return str(cp.resolve())
    return None


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


def _safe_output_stem(txt_path: Path) -> str:
    stem = txt_path.stem
    for c in '<>:"/\\|?*':
        stem = stem.replace(c, "_")
    stem = stem.strip() or "story"
    stem = re.sub(r"\s+", "_", stem)
    return stem


def _story_suffixes() -> tuple[str, str]:
    try:
        from path_config import STORY_DRAFT_SUFFIX, STORY_FINAL_SUFFIX

        return str(STORY_DRAFT_SUFFIX), str(STORY_FINAL_SUFFIX)
    except Exception:
        return "_草稿", "_成稿"


def _allocate_mp4_path(final_dir: Path, stem: str, run_k: int, n_runs: int) -> Path:
    """成稿目录下分配不冲突的 mp4 路径（未启用关键词时间命名时）。"""
    final_dir.mkdir(parents=True, exist_ok=True)
    if n_runs > 1:
        root = final_dir / f"{stem}_{run_k:02d}.mp4"
    else:
        root = final_dir / f"{stem}.mp4"
    if not root.exists():
        return root
    for i in range(2, 10000):
        if n_runs > 1:
            p = final_dir / f"{stem}_{run_k:02d}_{i}.mp4"
        else:
            p = final_dir / f"{stem}_{i}.mp4"
        if not p.exists():
            return p
    raise SystemExit("无法生成不冲突的 MP4 文件名")


def find_first_page_index_with_keyword(pages: list[str], keyword: str) -> int | None:
    """返回关键词首次出现的页下标（0-based）；未出现则 None。"""
    if not keyword:
        return None
    for i, text in enumerate(pages):
        if keyword in text:
            return i
    return None


def timeline_mid_of_page_sec(
    page_idx: int,
    durations: list[float],
    fade_out_seconds: float,
) -> float:
    """
    与 index.html 一致：每页播完后再经过 fade 才进入下一页。
    返回「第 page_idx 页开始后再过该页时长一半」时，从整段起点算起的秒数（含此前全部内容与过渡）。
    """
    if page_idx < 0 or page_idx >= len(durations):
        return 0.0
    elapsed = 0.0
    for i in range(page_idx):
        elapsed += durations[i] + fade_out_seconds
    return elapsed + durations[page_idx] / 2.0


def _seconds_to_mm_ss_pair(total_sec: float) -> tuple[str, str]:
    """用于文件名中的 分+秒，各两位，如 3分40秒 → 03, 40。"""
    t = max(0, int(round(total_sec)))
    mm = t // 60
    ss = t % 60
    return f"{mm:02d}", f"{ss:02d}"


def _allocate_keyword_time_mp4_path(
    final_dir: Path,
    stem: str,
    mm: str,
    ss: str,
    fixed_suffix: str,
) -> Path:
    """
    例：文案13_惊天反击-03+40+13 (1).mp4；同名冲突则 (2)、(3)…
    fixed_suffix 为用户要求的固定段（默认 13）。
    """
    final_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = fixed_suffix.strip() or _DEFAULT_STORY_MP4_TIME_SUFFIX
    for n in range(1, 10000):
        name = f"{stem}-{mm}+{ss}+{safe_suffix} ({n}).mp4"
        p = final_dir / name
        if not p.exists():
            return p
    raise SystemExit("无法生成不冲突的关键词时间 MP4 文件名")


def _preview_txt(path: Path) -> tuple[str, list[str], list[str]]:
    """返回 (展示用多行文本, 非空行, 分页列表)。"""
    lines = load_non_empty_lines(path)
    pages = lines_to_pages_two_per_page(lines)
    head_lines = lines[:5]
    head = "\n".join(head_lines) if head_lines else "(无内容)"
    if len(lines) > 5:
        head += f"\n…（共 {len(lines)} 行非空）"
    preview = (
        f"文件：{path.name}\n"
        f"非空行：{len(lines)} → 约 {len(pages)} 页\n"
        f"---\n{head}"
    )
    return preview, lines, pages


def _prompt_unified_repeat_count(
    *,
    interactive: bool,
    default_repeat: int,
    forced: int | None,
    file_count: int,
) -> int:
    if forced is not None:
        return max(0, forced)
    if not interactive:
        print(
            f"（非交互终端）每个文稿默认生成 {default_repeat} 套；"
            f"可用 --wenan-all N 指定，共 {file_count} 个 txt"
        )
        return max(0, default_repeat)
    while True:
        s = input(
            f"共 {file_count} 个文稿，每个统一生成多少套？（0=全部跳过，回车=1）: "
        ).strip()
        if s == "":
            return 1
        try:
            n = int(s)
            if n < 0:
                print("请输入 ≥0 的整数")
                continue
            return n
        except ValueError:
            print("请输入整数")


def _list_wenan_txt(wenan_dir: Path) -> list[Path]:
    if not wenan_dir.is_dir():
        return []
    return sorted(wenan_dir.glob("*.txt"), key=lambda p: p.name.lower())


def generate_one_story(
    *,
    txt_path: Path,
    out_dir: Path,
    page_padding: float,
    gen_extra: list[str],
    repo_root: Path,
    export_mp4: bool,
    mp4_out: Path | None,
    mp4_final_dir: Path | None = None,
    mp4_naming_stem: str | None = None,
    mp4_run_k: int = 1,
    mp4_n_runs: int = 1,
    story_mp4_time_keyword: str | None = None,
    story_mp4_time_suffix: str = _DEFAULT_STORY_MP4_TIME_SUFFIX,
    mp4_width: str | None,
    mp4_height: str | None,
    mp4_show_bar: bool,
) -> None:
    lines = load_non_empty_lines(txt_path)
    if not lines:
        raise SystemExit(f"文案里没有非空行：{txt_path}")

    pages = lines_to_pages_two_per_page(lines)
    print(f"  [{txt_path.name}] 共 {len(lines)} 行 → {len(pages)} 页 → 输出 {out_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    gen_script = repo_root / "generate_animated_text.py"
    if not gen_script.is_file():
        raise SystemExit(f"找不到 {gen_script}")

    page_files: list[str] = []
    durations: list[float] = []

    effective_extra = list(gen_extra)
    if not _gen_extra_has_canvas_bg(effective_extra):
        story_bg = pick_random_canvas_background()
        effective_extra = ["--canvas-bg", story_bg.hex, *effective_extra]
        print(f"    本套 story 统一画布：{story_bg.name} {story_bg.hex}")

    for idx, phrase in enumerate(pages, start=1):
        name = f"page_{idx:03d}.html"
        out_html = out_dir / name
        cmd = [
            sys.executable,
            str(gen_script),
            phrase,
            "--out",
            str(out_html),
            *effective_extra,
        ]
        print(f"    [{idx}/{len(pages)}] {name}")
        subprocess.run(cmd, cwd=str(repo_root), check=True)

        html = out_html.read_text(encoding="utf-8")
        dur = estimate_page_duration_seconds(html) + page_padding
        durations.append(dur)
        page_files.append(name)

    total_content = sum(durations)
    fade_count = max(0, len(durations) - 1)
    total_with_fade = total_content + fade_count * _FADE_OUT_SECONDS

    kw = (story_mp4_time_keyword or "").strip()
    hi_page0: int | None = None
    hi_mid_sec: float | None = None
    hi_mm = hi_ss = None
    if kw:
        hi_page0 = find_first_page_index_with_keyword(pages, kw)
        if hi_page0 is not None:
            hi_mid_sec = timeline_mid_of_page_sec(
                hi_page0, durations, _FADE_OUT_SECONDS
            )
            hi_mm, hi_ss = _seconds_to_mm_ss_pair(hi_mid_sec)

    meta = {
        "source_txt": txt_path.name,
        "pages": page_files,
        "durations": durations,
        "total_content_sec": round(total_content, 3),
        "total_with_fade_sec": round(total_with_fade, 3),
        "fade_out_seconds": _FADE_OUT_SECONDS,
        "highlight_keyword": kw or None,
        "highlight_first_page_1based": (hi_page0 + 1) if hi_page0 is not None else None,
        "highlight_mid_timeline_sec": round(hi_mid_sec, 3)
        if hi_mid_sec is not None
        else None,
        "highlight_mm_plus_ss": f"{hi_mm}+{hi_ss}" if hi_mm is not None else None,
    }
    (out_dir / "story_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    index_html = out_dir / "index.html"
    index_html.write_text(
        _build_index_html(page_files, durations, fade_out_seconds=_FADE_OUT_SECONDS),
        encoding="utf-8",
    )

    print(
        f"  完成：{len(pages)} 页，≈{total_content:.1f}s（含页间淡出 ≈{total_with_fade:.1f}s）"
    )

    if export_mp4:
        export_js = repo_root / "export_mp4_from_html.js"
        if not export_js.is_file():
            raise SystemExit(f"自动导出需要存在 {export_js}")
        node = shutil.which("node")
        if not node:
            raise SystemExit("未在 PATH 中找到 node（可加 --no-export-mp4 跳过）")
        if mp4_out is not None:
            mp4_path = Path(mp4_out).expanduser().resolve()
        elif mp4_final_dir is not None and mp4_naming_stem:
            stem_name = mp4_naming_stem
            suffix_fix = (story_mp4_time_suffix or _DEFAULT_STORY_MP4_TIME_SUFFIX).strip()
            if kw and hi_page0 is not None and hi_mm is not None:
                mp4_path = _allocate_keyword_time_mp4_path(
                    mp4_final_dir, stem_name, hi_mm, hi_ss, suffix_fix
                )
                print(
                    f"    成稿 MP4：关键词「{kw}」首次第 {hi_page0 + 1} 页，"
                    f"该页时长中点时刻 ≈ {hi_mid_sec:.2f}s（含页间淡出）→ {mp4_path.name}"
                )
            else:
                if kw and hi_page0 is None:
                    print(f"    未在文案分页中找到「{kw}」，成稿 MP4 使用默认命名")
                mp4_path = _allocate_mp4_path(
                    mp4_final_dir, stem_name, mp4_run_k, mp4_n_runs
                )
        else:
            mp4_path = out_dir / "story.mp4"
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            node,
            str(export_js),
            "--story",
            str(out_dir),
            "--out",
            str(mp4_path),
        ]
        if mp4_width:
            cmd.extend(["--width", str(mp4_width)])
        if mp4_height:
            cmd.extend(["--height", str(mp4_height)])
        if not mp4_show_bar:
            cmd.append("--hide-bar")
        ff = resolve_ffmpeg_for_mp4_export()
        if not ff:
            raise SystemExit(
                "导出 MP4 需要带 libx264 的 ffmpeg。可执行：brew install ffmpeg，"
                "或设置环境变量 FFMPEG_PATH 指向 ffmpeg 可执行文件。"
            )
        env = os.environ.copy()
        env["FFMPEG_PATH"] = ff
        print("    导出 MP4 …")
        t0 = time.perf_counter()
        subprocess.run(cmd, cwd=str(repo_root), check=True, env=env)
        elapsed = time.perf_counter() - t0
        size_mb = mp4_path.stat().st_size / (1024 * 1024)
        print(f"    MP4：{mp4_path}  ({elapsed:.1f}s, {size_mb:.2f} MiB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="文案按两页一行分页，连续生成手写动画并汇总 index.html")
    parser.add_argument(
        "txt_path",
        nargs="?",
        default=None,
        help="单个文案 txt（省略则扫描 --wenan-dir 下全部 .txt）",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help="输出根目录（省略则用 path_config：BASE_DIR/中间文本；失败则用仓库 story_output）",
    )
    parser.add_argument(
        "--wenan-dir",
        default=_WENAN_DIR_NAME,
        help=f"批量文案目录，相对仓库根（默认 {_WENAN_DIR_NAME}）",
    )
    parser.add_argument(
        "--wenan-all",
        type=int,
        default=None,
        metavar="N",
        help="不提问：每个 txt 统一生成 N 套（N≥0）",
    )
    parser.add_argument(
        "--wenan-default-repeat",
        type=int,
        default=1,
        metavar="N",
        help="非交互终端时每个 txt 默认套数（默认 1）",
    )
    parser.add_argument(
        "--page-padding",
        type=float,
        default=0.35,
        help="每页时长估算附加秒数",
    )
    parser.add_argument(
        "--no-export-mp4",
        action="store_true",
        help="不自动导出 MP4",
    )
    parser.add_argument(
        "--export-mp4",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--mp4-out",
        default=None,
        help="单文件模式：MP4 路径；wenan 批量时忽略",
    )
    parser.add_argument(
        "--mp4-fps",
        default=None,
        help="已废弃",
    )
    parser.add_argument("--mp4-width", default=None, help="传给 export 的 --width")
    parser.add_argument("--mp4-height", default=None, help="传给 export 的 --height")
    parser.add_argument(
        "--mp4-show-bar",
        action="store_true",
        help="导出时保留进度栏",
    )
    parser.add_argument(
        "--story-mp4-time-keyword",
        default=_DEFAULT_STORY_MP4_TIME_KEYWORD,
        metavar="KW",
        help="成稿 MP4 文件名中的时刻：关键词在分页后首次出现的页，取该页播放到时长中点时的累计秒数"
        "（含页间淡出），格式为 分+秒+固定段+(编号)。默认：橱窗",
    )
    parser.add_argument(
        "--no-story-mp4-time-keyword",
        action="store_true",
        help="关闭关键词时刻命名，仍用 文案名.mp4 或 文案名_01.mp4",
    )
    parser.add_argument(
        "--story-mp4-time-suffix",
        default=_DEFAULT_STORY_MP4_TIME_SUFFIX,
        metavar="SUF",
        help="文件名中「分+秒」之后的固定段，默认 13",
    )
    parser.add_argument(
        "gen_args",
        nargs=argparse.REMAINDER,
        help="传给 generate_animated_text.py（前请加 --）",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    gen_extra = list(args.gen_args or [])
    if gen_extra and gen_extra[0] == "--":
        gen_extra = gen_extra[1:]

    export_mp4 = not args.no_export_mp4
    base_out = (
        Path(args.out_dir).expanduser().resolve()
        if args.out_dir
        else _default_output_root()
    )
    base_out.mkdir(parents=True, exist_ok=True)

    if args.txt_path:
        txt_path = Path(args.txt_path).expanduser().resolve()
        if not txt_path.is_file():
            raise SystemExit(f"找不到文案文件：{txt_path}")
        stem = _safe_output_stem(txt_path)
        draft_sfx, final_sfx = _story_suffixes()
        draft_root = base_out / f"{stem}{draft_sfx}"
        final_root = base_out / f"{stem}{final_sfx}"
        html_out = draft_root
        time_kw = (
            None
            if args.no_story_mp4_time_keyword
            else (args.story_mp4_time_keyword.strip() or None)
        )
        if export_mp4:
            if args.mp4_out:
                mp4_single = Path(args.mp4_out).expanduser().resolve()
                mp4_final = None
                naming_stem = None
            else:
                mp4_single = None
                mp4_final = final_root
                naming_stem = stem
        else:
            mp4_single = None
            mp4_final = None
            naming_stem = None
        generate_one_story(
            txt_path=txt_path,
            out_dir=html_out,
            page_padding=args.page_padding,
            gen_extra=gen_extra,
            repo_root=repo_root,
            export_mp4=export_mp4,
            mp4_out=mp4_single,
            mp4_final_dir=mp4_final,
            mp4_naming_stem=naming_stem,
            mp4_run_k=1,
            mp4_n_runs=1,
            story_mp4_time_keyword=time_kw,
            story_mp4_time_suffix=args.story_mp4_time_suffix,
            mp4_width=args.mp4_width,
            mp4_height=args.mp4_height,
            mp4_show_bar=args.mp4_show_bar,
        )
        print(f"打开连续播放：{html_out / 'index.html'}")
        return

    wenan_dir = (repo_root / args.wenan_dir).resolve()
    txt_files = _list_wenan_txt(wenan_dir)
    if not txt_files:
        raise SystemExit(
            f"未找到可用文案：目录「{wenan_dir}」不存在或其中没有 .txt。\n"
            f"请创建并放入文案，或：python {Path(__file__).name} 你的文案.txt"
        )

    if args.mp4_out:
        print(
            "提示：wenan 批量忽略 --mp4-out，MP4 写入各文案的「_成稿」目录",
            file=sys.stderr,
        )

    interactive = sys.stdin.isatty()
    print(f"wenan 批量：{len(txt_files)} 个 txt，输出根目录 {base_out}")
    print(f"文案目录：{wenan_dir}\n")

    print("— 文稿列表（每个将使用下面输入的同一套数）—")
    nonempty: list[Path] = []
    for txt_path in txt_files:
        preview, lines, _pages = _preview_txt(txt_path)
        print("\n" + preview)
        if lines:
            nonempty.append(txt_path)
        else:
            print("  → 将跳过（无有效行）")

    if not nonempty:
        raise SystemExit("没有可生成的文稿（全部为空）。")

    n = _prompt_unified_repeat_count(
        interactive=interactive,
        default_repeat=max(0, args.wenan_default_repeat),
        forced=args.wenan_all,
        file_count=len(nonempty),
    )
    if n == 0:
        print("套数为 0，不生成。")
        return

    print(f"\n每个文稿 {n} 套，{len(nonempty)} 个文稿 → 最多 {len(nonempty) * n} 套。\n")

    draft_sfx, final_sfx = _story_suffixes()
    total_jobs = 0
    for txt_path in nonempty:
        stem = _safe_output_stem(txt_path)
        draft_root = base_out / f"{stem}{draft_sfx}"
        final_root = base_out / f"{stem}{final_sfx}"
        for k in range(1, n + 1):
            if n > 1:
                out_dir = draft_root / f"{stem}_{k:02d}"
            else:
                out_dir = draft_root
            total_jobs += 1
            time_kw = (
                None
                if args.no_story_mp4_time_keyword
                else (args.story_mp4_time_keyword.strip() or None)
            )
            print(f"\n>>> [{total_jobs}] {txt_path.name} 第 {k}/{n} 套 → {out_dir}")
            if export_mp4:
                print(f"    成稿目录：{final_root}")
            try:
                generate_one_story(
                    txt_path=txt_path,
                    out_dir=out_dir,
                    page_padding=args.page_padding,
                    gen_extra=gen_extra,
                    repo_root=repo_root,
                    export_mp4=export_mp4,
                    mp4_out=None,
                    mp4_final_dir=final_root if export_mp4 else None,
                    mp4_naming_stem=stem if export_mp4 else None,
                    mp4_run_k=k,
                    mp4_n_runs=n,
                    story_mp4_time_keyword=time_kw,
                    story_mp4_time_suffix=args.story_mp4_time_suffix,
                    mp4_width=args.mp4_width,
                    mp4_height=args.mp4_height,
                    mp4_show_bar=args.mp4_show_bar,
                )
            except subprocess.CalledProcessError as e:
                raise SystemExit(f"子进程失败（退出码 {e.returncode}）") from e

    print(f"\n全部完成：共 {total_jobs} 套，根目录 {base_out}")


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
