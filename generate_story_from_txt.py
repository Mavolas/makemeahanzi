#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从纯文本文案生成「多页」手写动画，每页两行（跳过空行），每页调用 generate_animated_text.py，
最后生成 index.html 用 iframe 按时间轴连续播放全部页。
未在「--」后指定 --canvas-bg / --canvas-bg-image 时：若 path_config.BASE_DIR/中间文本背景 存在且含图片，
  则以 2:1 权重相对「仅纯色」随机选背景图（CSS cover 铺满，小图等比放大）并配随机淡色底；否则仅随机纯色（与 backgrounds.py 一致）。
  指定 --canvas-bg 或 --canvas-bg-image 则全 story 按参数，不再走上述逻辑。
index.html 页间切换可用 --story-page-transition 配置（默认 text=方式2）：
  · text（方式2，默认）：子页注入 --story-index-bridge + --transparent-canvas-backdrop；index 内 storyStaticBackdrop 与整 story 画布一致，子页背景透明，iframe 换 src 时底图不闪断。
  · default（方式1）：有 --canvas-bg-image 时为整页 iframe 淡入淡出；纯 --canvas-bg 时为同色遮罩后切页。

不传文案路径时：扫描仓库根下「wenan」目录内全部 .txt，逐个打印摘要（文件名、行数、约多少页、前几行），
再只问一次「每个文稿统一生成多少套」。
默认目录结构（在 path_config.BASE_DIR/中间文本/ 下）：
  · HTML：{文案名}_草稿/（多套时其下再有 {文案名}_01、_02 …）
  · MP4：{文案名}_成稿/（与草稿同级，仍在输出根目录如「中间文本」下）；默认按「橱窗」首次出现页在该页时长中点时的累计时刻命名（含页间淡出），
    启用自动导出 MP4 且全部成功后，仅删除 path_config 中 STORY_DRAFT_SUFFIX 对应的「{文案名}_草稿」目录，不移动、不删除 STORY_FINAL_SUFFIX 成稿目录。
    如 文案13_惊天反击-03+40+13 (1).mp4；冲突自动 (2)(3)…；可用 --no-story-mp4-time-keyword 恢复旧命名。
  wenan 批量且需导出多个 MP4 时，默认最多 2 路并行调用 Node/Playwright 导出（可用 --mp4-export-workers 调整；1 为顺序）。

用法示例：
  python3 generate_story_from_txt.py
  python3 generate_story_from_txt.py --wenan-all 20
  python3 generate_story_from_txt.py --no-export-mp4
  python3 generate_story_from_txt.py 其它文案.txt --out-dir out -- --speed 2 --char-size 140
  python3 generate_story_from_txt.py --wenan-dir 我的文案夹
  python3 generate_story_from_txt.py --story-page-transition default

「--」 后面的参数会原样传给 generate_animated_text.py。
未写 --stroke-draw-ratio 时，story 会默认注入 1.0（笔画全程等粗）；若需细→粗变化可在「--」后自行传该参数覆盖。
未在「--」后指定 --speed 时，每套 story 在 7.5～8.5 间随机一个速率，该套内各页共用同一数值。
仓库根下 shouxing/ 内放多个手形 PNG 时，每套 story 随机选一张，全套页共用；「--」里已传 --hand-image 则不随机。
"""

from __future__ import annotations

import argparse
import html
import json
import os
from urllib.parse import quote
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from generate_animated_text import (
    STORY_INDEX_BRIDGE_ID,
    estimate_phrase_html_duration_seconds,
)
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


def _gen_extra_has_canvas_bg_image(extra: list[str]) -> bool:
    """是否已指定画布背景图（--canvas-bg-image）。"""
    for tok in extra:
        if tok == "--canvas-bg-image":
            return True
        if tok.startswith("--canvas-bg-image="):
            return True
    return False


_STORY_BG_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
)


def _resolve_story_bg_images_dir() -> Path | None:
    """path_config.BASE_DIR / STORY_BG_IMAGES_SUBDIR；不可用则 None。"""
    try:
        import path_config as pc

        sub = getattr(pc, "STORY_BG_IMAGES_SUBDIR", "中间文本背景")
        p = (Path(pc.BASE_DIR).expanduser() / sub).resolve()
        return p if p.is_dir() else None
    except Exception:
        return None


def _list_story_bg_images(dir_path: Path) -> list[Path]:
    if not dir_path.is_dir():
        return []
    out = [
        p
        for p in dir_path.iterdir()
        if p.is_file() and p.suffix.lower() in _STORY_BG_IMAGE_SUFFIXES
    ]
    out.sort(key=lambda p: p.name.lower())
    return out


def _copy_story_bg_image_to_out_dir(src: Path, out_dir: Path) -> str:
    """复制到 out_dir，若重名则加后缀，返回写入后的文件名。"""
    dest = out_dir / src.name
    if not dest.exists():
        shutil.copy2(src, dest)
        return dest.name
    stem, suf = src.stem, src.suffix
    n = 1
    while True:
        cand = out_dir / f"{stem}_bg{n}{suf}"
        if not cand.exists():
            shutil.copy2(src, cand)
            return cand.name
        n += 1
        if n > 9999:
            raise SystemExit("无法为 story 背景图生成不冲突的文件名")


def _gen_extra_has_stroke_draw_ratio(extra: list[str]) -> bool:
    """是否已指定笔画粗细动画比例（--stroke-draw-ratio）。"""
    for tok in extra:
        if tok == "--stroke-draw-ratio":
            return True
        if tok.startswith("--stroke-draw-ratio="):
            return True
    return False


def _gen_extra_has_speed(extra: list[str]) -> bool:
    """是否已指定 --speed（传给 generate_animated_text）。"""
    for tok in extra:
        if tok == "--speed":
            return True
        if tok.startswith("--speed="):
            return True
    return False


def _gen_extra_has_story_index_bridge(extra: list[str]) -> bool:
    return "--story-index-bridge" in extra


def _gen_extra_has_transparent_canvas_backdrop(extra: list[str]) -> bool:
    return "--transparent-canvas-backdrop" in extra


def _gen_extra_has_hand_image(extra: list[str]) -> bool:
    """是否已指定手形图（--hand-image）。"""
    for tok in extra:
        if tok == "--hand-image":
            return True
        if tok.startswith("--hand-image="):
            return True
    return False


def _list_shouxing_pngs(dir_path: Path) -> list[Path]:
    """列出目录内全部 .png（不递归），按文件名排序后供随机抽取。"""
    if not dir_path.is_dir():
        return []
    out = [p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
    out.sort(key=lambda p: p.name.lower())
    return out


def _looks_like_hex_color(s: str) -> bool:
    t = s.strip()
    if t.startswith("#"):
        t = t[1:]
    return bool(t) and len(t) in (3, 6) and all(
        c in "0123456789abcdefABCDEF" for c in t
    )


def parse_canvas_bg_hex_from_gen_extra(extra: list[str]) -> str | None:
    """从 gen_extra / effective_extra 解析 --canvas-bg，返回如 #RRGGBB。"""
    i = 0
    while i < len(extra):
        tok = extra[i]
        if tok == "--canvas-bg" and i + 1 < len(extra):
            raw = extra[i + 1].strip()
            if _looks_like_hex_color(raw):
                return raw if raw.startswith("#") else f"#{raw}"
            return None
        if tok.startswith("--canvas-bg="):
            raw = tok.split("=", 1)[1].strip()
            if _looks_like_hex_color(raw):
                return raw if raw.startswith("#") else f"#{raw}"
            return None
        i += 1
    return None


def _parse_canvas_bg_image_basename_from_extra(extra: list[str]) -> str | None:
    """从 gen_extra 解析 --canvas-bg-image 的文件名（与输出目录下文件名一致）。"""
    i = 0
    while i < len(extra):
        tok = extra[i]
        if tok == "--canvas-bg-image" and i + 1 < len(extra):
            raw = extra[i + 1].strip()
            if raw and not raw.startswith("-"):
                return Path(raw).name
            return None
        if tok.startswith("--canvas-bg-image="):
            raw = tok.split("=", 1)[1].strip()
            if raw:
                return Path(raw).name
            return None
        i += 1
    return None


def _default_output_root() -> Path:
    """默认输出：path_config.BASE_DIR / STORY_OUTPUT_SUBDIR；失败则用仓库内 story_output。"""
    repo_root = Path(__file__).resolve().parent
    try:
        from path_config import BASE_DIR, STORY_OUTPUT_SUBDIR

        root = Path(BASE_DIR).expanduser() / STORY_OUTPUT_SUBDIR
        return root.resolve()
    except Exception:
        return (repo_root / "story_output").resolve()
_FADE_OUT_SECONDS = 0.5

# index 页间切换：命令行 --story-page-transition
_STORY_PAGE_TRANSITION_DEFAULT = "default"
_STORY_PAGE_TRANSITION_TEXT = "text"


def _index_transition_kind(story_page_transition: str, has_background_image: bool) -> str:
    """
    解析为写入 index.html 的 JS 分支：
    iframe_crossfade / color_overlay（方式1 的两种子策略）/ inner_text_fade（方式2）。
    """
    if story_page_transition == _STORY_PAGE_TRANSITION_TEXT:
        return "inner_text_fade"
    if has_background_image:
        return "iframe_crossfade"
    return "color_overlay"


# 未传 --speed 时，每套 story 随机速率（该套内各页一致）
_STORY_SPEED_RANDOM_MIN = 7.5
_STORY_SPEED_RANDOM_MAX = 8.5

# 成稿 MP4：默认用「关键词首次出现页」在整段播放到该页时长中点时的累计时刻（含页间淡出）命名
_DEFAULT_STORY_MP4_TIME_KEYWORD = "橱窗"
_DEFAULT_STORY_MP4_TIME_SUFFIX = "13"

_HOMEBREW_FFMPEG_CANDIDATES = (
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)


def _windows_merged_path_for_which() -> str | None:
    """GUI/IDE 子进程常未继承安装后刷新的 PATH；合并注册表与当前环境以便找到 winget 等安装的 ffmpeg。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    chunks: list[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as k:
            chunks.append(winreg.QueryValueEx(k, "Path")[0])
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            chunks.append(winreg.QueryValueEx(k, "Path")[0])
    except OSError:
        pass
    cur = os.environ.get("PATH", "")
    if cur:
        chunks.append(cur)
    merged = os.pathsep.join(c for c in chunks if c)
    return merged or None


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
    wp = _windows_merged_path_for_which()
    if wp:
        w = shutil.which("ffmpeg", path=wp)
        if w:
            return w
    for c in _HOMEBREW_FFMPEG_CANDIDATES:
        cp = Path(c)
        if cp.is_file():
            return str(cp.resolve())
    return None


def _preflight_mp4_export_environment(repo_root: Path) -> None:
    """批量并行导出前一次性检查 export 脚本、node、ffmpeg。"""
    export_js = repo_root / "export_mp4_from_html.js"
    if not export_js.is_file():
        raise SystemExit(f"自动导出需要存在 {export_js}")
    if not shutil.which("node"):
        raise SystemExit("未在 PATH 中找到 node（可加 --no-export-mp4 跳过）")
    ff = resolve_ffmpeg_for_mp4_export()
    if not ff:
        raise SystemExit(
            "导出 MP4 需要带 libx264 的 ffmpeg。Windows：winget install Gyan.FFmpeg；"
            "macOS：brew install ffmpeg；或设置环境变量 FFMPEG_PATH 指向 ffmpeg 可执行文件。"
        )


def run_story_mp4_export(
    *,
    repo_root: Path,
    story_dir: Path,
    mp4_path: Path,
    mp4_width: str | None,
    mp4_height: str | None,
    mp4_show_bar: bool,
    label: str = "",
    print_lock: threading.Lock | None = None,
) -> None:
    """调用 export_mp4_from_html.js 导出单个 story 目录为 MP4（可供线程池并发调用）。"""
    export_js = repo_root / "export_mp4_from_html.js"
    node = shutil.which("node")
    if not node or not export_js.is_file():
        raise RuntimeError("缺少 node 或 export_mp4_from_html.js")
    ff = resolve_ffmpeg_for_mp4_export()
    if not ff:
        raise RuntimeError("未找到 ffmpeg（含 FFMPEG_PATH / PATH）")
    mp4_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        node,
        str(export_js),
        "--story",
        str(story_dir),
        "--out",
        str(mp4_path),
    ]
    if mp4_width:
        cmd.extend(["--width", str(mp4_width)])
    if mp4_height:
        cmd.extend(["--height", str(mp4_height)])
    if not mp4_show_bar:
        cmd.append("--hide-bar")
    env = os.environ.copy()
    env["FFMPEG_PATH"] = ff
    prefix = f"{label} " if label else ""

    def _emit(msg: str) -> None:
        if print_lock is not None:
            with print_lock:
                print(msg)
        else:
            print(msg)

    _emit(f"    {prefix}导出 MP4 …")
    t0 = time.perf_counter()
    subprocess.run(cmd, cwd=str(repo_root), check=True, env=env)
    elapsed = time.perf_counter() - t0
    size_mb = mp4_path.stat().st_size / (1024 * 1024)
    _emit(f"    {prefix}MP4：{mp4_path}  ({elapsed:.1f}s, {size_mb:.2f} MiB)")


def _run_one_mp4_export_job(job: dict) -> None:
    run_story_mp4_export(
        repo_root=job["repo_root"],
        story_dir=job["story_dir"],
        mp4_path=job["mp4_path"],
        mp4_width=job["mp4_width"],
        mp4_height=job["mp4_height"],
        mp4_show_bar=job["mp4_show_bar"],
        label=job["label"],
        print_lock=job["print_lock"],
    )


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


def _remove_story_draft_dir(draft_root: Path) -> None:
    """导出 MP4 成功后仅删除 STORY_DRAFT_SUFFIX 对应的草稿目录（HTML、story_meta 等）；成稿目录不动。"""
    if not draft_root.is_dir():
        return
    try:
        shutil.rmtree(draft_root)
    except OSError as e:
        raise SystemExit(f"无法删除草稿目录：{draft_root}\n{e}") from e
    print(f"已删除草稿目录：{draft_root}")


def _allocate_mp4_path(
    final_dir: Path,
    stem: str,
    run_k: int,
    n_runs: int,
    *,
    reserved_paths: set[str] | None = None,
) -> Path:
    """成稿目录下分配不冲突的 mp4 路径（未启用关键词时间命名时）。"""
    final_dir.mkdir(parents=True, exist_ok=True)

    def _free(p: Path) -> bool:
        key = str(p.resolve())
        return not p.exists() and (reserved_paths is None or key not in reserved_paths)

    if n_runs > 1:
        root = final_dir / f"{stem}_{run_k:02d}.mp4"
    else:
        root = final_dir / f"{stem}.mp4"
    if _free(root):
        if reserved_paths is not None:
            reserved_paths.add(str(root.resolve()))
        return root
    for i in range(2, 10000):
        if n_runs > 1:
            p = final_dir / f"{stem}_{run_k:02d}_{i}.mp4"
        else:
            p = final_dir / f"{stem}_{i}.mp4"
        if _free(p):
            if reserved_paths is not None:
                reserved_paths.add(str(p.resolve()))
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
    *,
    reserved_paths: set[str] | None = None,
) -> Path:
    """
    例：文案13_惊天反击-03+40+13 (1).mp4；同名冲突则 (2)、(3)…
    fixed_suffix 为用户要求的固定段（默认 13）。
    reserved_paths：延迟并行导出时，已分配给本批其它任务的绝对路径（字符串），避免仅靠 exists() 误判未占用。
    """
    final_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = fixed_suffix.strip() or _DEFAULT_STORY_MP4_TIME_SUFFIX
    for n in range(1, 10000):
        name = f"{stem}-{mm}+{ss}+{safe_suffix} ({n}).mp4"
        p = final_dir / name
        key = str(p.resolve())
        if not p.exists() and (reserved_paths is None or key not in reserved_paths):
            if reserved_paths is not None:
                reserved_paths.add(key)
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
    shouxing_dir: str = "shouxing",
    mp4_export_jobs: list[dict] | None = None,
    mp4_job_label: str = "",
    mp4_export_print_lock: threading.Lock | None = None,
    mp4_reserved_paths: set[str] | None = None,
    story_page_transition: str = _STORY_PAGE_TRANSITION_TEXT,
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
    if not _gen_extra_has_stroke_draw_ratio(effective_extra):
        # 与 generate_animated_text 默认一致：全程等粗，避免 story 批量时仍见细线/粗线混杂
        effective_extra = ["--stroke-draw-ratio", "1.0", *effective_extra]
    if not _gen_extra_has_speed(effective_extra):
        story_speed = random.uniform(_STORY_SPEED_RANDOM_MIN, _STORY_SPEED_RANDOM_MAX)
        effective_extra = ["--speed", f"{story_speed:.4f}", *effective_extra]
        print(f"    本套 story 随机 --speed：{story_speed:.4f}（{len(pages)} 页共用）")
    if not _gen_extra_has_hand_image(effective_extra):
        sx_root = (repo_root / shouxing_dir).resolve()
        sx_pngs = _list_shouxing_pngs(sx_root)
        if sx_pngs:
            picked = random.choice(sx_pngs)
            try:
                hand_rel = picked.relative_to(repo_root).as_posix()
            except ValueError:
                hand_rel = str(picked)
            effective_extra = ["--hand-image", hand_rel, *effective_extra]
            print(f"    本套 story 随机手形：{hand_rel}")
    story_bg_image_basename: str | None = None
    if not _gen_extra_has_canvas_bg(effective_extra) and not _gen_extra_has_canvas_bg_image(
        effective_extra
    ):
        story_bg = pick_random_canvas_background()
        bg_dir = _resolve_story_bg_images_dir()
        bg_imgs = _list_story_bg_images(bg_dir) if bg_dir else []
        # 有图时：背景图权重 2，纯色权重 1
        pick_image = bool(bg_imgs) and random.randint(1, 3) <= 2
        if pick_image:
            src = random.choice(bg_imgs)
            try:
                story_bg_image_basename = _copy_story_bg_image_to_out_dir(src, out_dir)
            except OSError as e:
                print(f"    警告：复制背景图失败（{e}），改用纯色画布", file=sys.stderr)
                story_bg_image_basename = None
        if story_bg_image_basename:
            effective_extra = [
                "--canvas-bg",
                story_bg.hex,
                "--canvas-bg-image",
                story_bg_image_basename,
                *effective_extra,
            ]
            print(
                f"    本套 story 画布：背景图 {story_bg_image_basename}（相对纯色权重 2:1）"
                f" + 底色 {story_bg.name} {story_bg.hex}"
            )
        else:
            effective_extra = ["--canvas-bg", story_bg.hex, *effective_extra]
            print(f"    本套 story 统一画布：{story_bg.name} {story_bg.hex}")

    story_canvas_bg = parse_canvas_bg_hex_from_gen_extra(effective_extra)
    if not story_canvas_bg:
        story_canvas_bg = "#e8e8e8"

    if story_page_transition == _STORY_PAGE_TRANSITION_TEXT:
        if not _gen_extra_has_story_index_bridge(effective_extra):
            effective_extra = ["--story-index-bridge", *effective_extra]
    if not _gen_extra_has_transparent_canvas_backdrop(effective_extra):
        effective_extra = ["--transparent-canvas-backdrop", *effective_extra]

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
        "story_canvas_bg": story_canvas_bg,
        "story_background_image": story_bg_image_basename,
        "highlight_keyword": kw or None,
        "highlight_first_page_1based": (hi_page0 + 1) if hi_page0 is not None else None,
        "highlight_mid_timeline_sec": round(hi_mid_sec, 3)
        if hi_mid_sec is not None
        else None,
        "highlight_mm_plus_ss": f"{hi_mm}+{hi_ss}" if hi_mm is not None else None,
    }

    bg_img_bn = story_bg_image_basename or _parse_canvas_bg_image_basename_from_extra(
        effective_extra
    )
    transition_kind = _index_transition_kind(story_page_transition, bool(bg_img_bn))
    _tp_desc = {
        "iframe_crossfade": (
            f"方式1·有背景图：整页 iframe 淡入淡出（{_FADE_OUT_SECONDS:g}s）"
        ),
        "color_overlay": f"方式1·纯色画布：同色遮罩切页（{_FADE_OUT_SECONDS:g}s）",
        "inner_text_fade": (
            f"方式2：仅短语与手形淡出，画布背景不动（{_FADE_OUT_SECONDS:g}s）"
        ),
    }
    print(f"    页间过渡：{_tp_desc[transition_kind]}")
    meta["story_page_transition"] = story_page_transition
    meta["transition_kind"] = transition_kind
    meta["fade_layer_background"] = story_canvas_bg
    meta["fade_overlay_max_opacity"] = 1.0

    (out_dir / "story_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    index_html = out_dir / "index.html"
    story_bridge_id_js = json.dumps(
        STORY_INDEX_BRIDGE_ID if transition_kind == "inner_text_fade" else ""
    )
    index_html.write_text(
        _build_index_html(
            page_files,
            durations,
            fade_out_seconds=_FADE_OUT_SECONDS,
            stage_background=story_canvas_bg,
            transition_kind=transition_kind,
            story_bridge_id_js=story_bridge_id_js,
            index_static_backdrop_image=bg_img_bn,
        ),
        encoding="utf-8",
    )

    print(
        f"  完成：{len(pages)} 页，≈{total_content:.1f}s（含页间淡出 ≈{total_with_fade:.1f}s）"
    )

    if export_mp4:
        export_js = repo_root / "export_mp4_from_html.js"
        if not export_js.is_file():
            raise SystemExit(f"自动导出需要存在 {export_js}")
        defer_mp4 = mp4_export_jobs is not None
        if not defer_mp4:
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
                    mp4_final_dir,
                    stem_name,
                    hi_mm,
                    hi_ss,
                    suffix_fix,
                    reserved_paths=mp4_reserved_paths,
                )
                print(
                    f"    成稿 MP4：关键词「{kw}」首次第 {hi_page0 + 1} 页，"
                    f"该页时长中点时刻 ≈ {hi_mid_sec:.2f}s（含页间淡出）→ {mp4_path.name}"
                )
            else:
                if kw and hi_page0 is None:
                    print(f"    未在文案分页中找到「{kw}」，成稿 MP4 使用默认命名")
                mp4_path = _allocate_mp4_path(
                    mp4_final_dir,
                    stem_name,
                    mp4_run_k,
                    mp4_n_runs,
                    reserved_paths=mp4_reserved_paths,
                )
        else:
            mp4_path = out_dir / "story.mp4"
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        if defer_mp4:
            mp4_export_jobs.append(
                {
                    "repo_root": repo_root,
                    "story_dir": out_dir,
                    "mp4_path": mp4_path,
                    "mp4_width": mp4_width,
                    "mp4_height": mp4_height,
                    "mp4_show_bar": mp4_show_bar,
                    "label": mp4_job_label,
                    "print_lock": mp4_export_print_lock,
                }
            )
        else:
            ff = resolve_ffmpeg_for_mp4_export()
            if not ff:
                raise SystemExit(
                    "导出 MP4 需要带 libx264 的 ffmpeg。Windows：winget install Gyan.FFmpeg；"
                    "macOS：brew install ffmpeg；或设置环境变量 FFMPEG_PATH 指向 ffmpeg 可执行文件。"
                )
            run_story_mp4_export(
                repo_root=repo_root,
                story_dir=out_dir,
                mp4_path=mp4_path,
                mp4_width=mp4_width,
                mp4_height=mp4_height,
                mp4_show_bar=mp4_show_bar,
                label="",
                print_lock=None,
            )


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
        "--story-page-transition",
        choices=(_STORY_PAGE_TRANSITION_DEFAULT, _STORY_PAGE_TRANSITION_TEXT),
        default=_STORY_PAGE_TRANSITION_TEXT,
        metavar="MODE",
        help="index 页间切换（默认 text）：text=方式2 仅文字与手形淡出、背景不动；"
        "default=方式1（有背景图则整页淡入淡出，否则同色遮罩）",
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
    parser.add_argument("--mp4-width", default=1054, help="传给 export 的 --width")
    parser.add_argument("--mp4-height", default=588, help="传给 export 的 --height")
    parser.add_argument(
        "--mp4-export-workers",
        type=int,
        default=2,
        metavar="N",
        help="wenan 批量导出多个 MP4 时的最大并发数（默认 2；1 表示顺序导出）",
    )
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
        "--shouxing-dir",
        default="shouxing",
        metavar="DIR",
        help="手形 PNG 目录（相对仓库根）；每套 story 随机选一张，全套页共用；目录不存在或无 png 则用子脚本默认手形；「--」里传 --hand-image 时不随机",
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
            shouxing_dir=args.shouxing_dir,
            story_page_transition=args.story_page_transition,
        )
        if export_mp4:
            _remove_story_draft_dir(draft_root)
        else:
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
    batch_job_count = len(nonempty) * n
    mp4_jobs: list[dict] | None = (
        [] if (export_mp4 and batch_job_count > 1) else None
    )
    mp4_print_lock = threading.Lock() if mp4_jobs is not None else None
    mp4_reserved_paths: set[str] | None = set() if mp4_jobs is not None else None
    if mp4_jobs is not None:
        _preflight_mp4_export_environment(repo_root)
        w_cap = max(1, args.mp4_export_workers)
        print(
            f"本批 HTML 顺序生成；{batch_job_count} 个 MP4 将在完成后并行导出"
            f"（最大并发 {min(w_cap, batch_job_count)}）。\n"
        )

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
                    shouxing_dir=args.shouxing_dir,
                    mp4_export_jobs=mp4_jobs,
                    mp4_job_label=f"[{total_jobs}]",
                    mp4_export_print_lock=mp4_print_lock,
                    mp4_reserved_paths=mp4_reserved_paths,
                    story_page_transition=args.story_page_transition,
                )
            except subprocess.CalledProcessError as e:
                raise SystemExit(f"子进程失败（退出码 {e.returncode}）") from e

    if mp4_jobs:
        w = min(max(1, args.mp4_export_workers), len(mp4_jobs))
        print(f"\n并行导出 MP4：共 {len(mp4_jobs)} 个，并发 {w} …")
        errors: list[tuple[str, BaseException]] = []
        with ThreadPoolExecutor(max_workers=w) as ex:
            future_to_label = {
                ex.submit(_run_one_mp4_export_job, job): str(job.get("label", ""))
                for job in mp4_jobs
            }
            for fut in as_completed(future_to_label):
                label = future_to_label[fut]
                try:
                    fut.result()
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    errors.append((label, exc))
        if errors:
            for lbl, exc in errors:
                print(f"MP4 导出失败 {lbl}: {exc}", file=sys.stderr)
            raise SystemExit(f"{len(errors)} 个 MP4 导出失败")

    if export_mp4:
        for txt_path in nonempty:
            stem = _safe_output_stem(txt_path)
            _remove_story_draft_dir(base_out / f"{stem}{draft_sfx}")

    print(f"\n全部完成：共 {total_jobs} 套，根目录 {base_out}")


def _build_index_html(
    pages: list[str],
    durations: list[float],
    *,
    fade_out_seconds: float = _FADE_OUT_SECONDS,
    stage_background: str = "#ffffff",
    transition_kind: str = "color_overlay",
    story_bridge_id_js: str = '""',
    index_static_backdrop_image: str | None = None,
) -> str:
    pages_json = json.dumps(pages, ensure_ascii=False)
    durs_json = json.dumps(durations)
    fade_ms = max(1, int(round(fade_out_seconds * 1000)))
    fade_css_js = json.dumps(f"{fade_out_seconds:g}s")
    safe_stage_bg = html.escape(stage_background)
    safe_fade_layer_bg = safe_stage_bg
    kind_js = json.dumps(transition_kind)
    if index_static_backdrop_image:
        safe_img_url = quote(index_static_backdrop_image, safe="")
        static_backdrop_css = f"""background-color: {safe_stage_bg};
      background-image: url(\"{safe_img_url}\");
      background-size: cover;
      background-position: center;
      background-repeat: no-repeat;"""
    else:
        static_backdrop_css = f"background: {safe_stage_bg};"
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
      background: {safe_stage_bg};
    }}
    #stage {{
      position: relative;
      width: 100%;
      height: 100%;
      background: transparent;
    }}
    #storyStaticBackdrop {{
      position: absolute;
      inset: 0;
      z-index: 0;
      pointer-events: none;
      {static_backdrop_css}
    }}
    #view {{
      position: relative;
      z-index: 1;
      width: 100%;
      height: 100%;
      border: 0;
      display: block;
      background: transparent;
    }}
    #whiteFade {{
      position: absolute;
      inset: 0;
      z-index: 2;
      background: {safe_fade_layer_bg};
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
    <div id="storyStaticBackdrop" aria-hidden="true"></div>
    <iframe id="view" title="当前页"></iframe>
    <div id="whiteFade" aria-hidden="true"></div>
  </div>
  <div id="bar"></div>
  <script>
    const pages = {pages_json};
    const durs = {durs_json};
    const FADE_MS = {fade_ms};
    const FADE_CSS = {fade_css_js};
    const TRANSITION_KIND = {kind_js};
    const STORY_BRIDGE_ID = {story_bridge_id_js};
    const bar = document.getElementById('bar');
    const view = document.getElementById('view');
    const whiteFade = document.getElementById('whiteFade');
    let timer = null;
    let fadeTimer = null;
    let pendingAfterFadeOut = null;
    let pendingAfterFadeIn = null;
    let bridgeFailSafe = null;

    function clearBridgeFailSafe() {{
      if (bridgeFailSafe) {{
        clearTimeout(bridgeFailSafe);
        bridgeFailSafe = null;
      }}
    }}

    function clearTimers() {{
      if (timer) {{
        clearTimeout(timer);
        timer = null;
      }}
      if (fadeTimer) {{
        clearTimeout(fadeTimer);
        fadeTimer = null;
      }}
      pendingAfterFadeOut = null;
      pendingAfterFadeIn = null;
      clearBridgeFailSafe();
    }}

    function setBar(text) {{
      bar.textContent = text;
    }}

    window.addEventListener('message', function (ev) {{
      if (!STORY_BRIDGE_ID) return;
      var d = ev.data;
      if (!d || d.storyBridge !== STORY_BRIDGE_ID) return;
      if (d.cmd === 'fadeOutDone') {{
        clearBridgeFailSafe();
        if (pendingAfterFadeOut) {{
          var f = pendingAfterFadeOut;
          pendingAfterFadeOut = null;
          f();
        }}
        return;
      }}
      if (d.cmd === 'fadeInDone') {{
        clearBridgeFailSafe();
        if (pendingAfterFadeIn) {{
          var g = pendingAfterFadeIn;
          pendingAfterFadeIn = null;
          g();
        }}
        return;
      }}
    }});

    function postStoryBridge(cmd, extra) {{
      var w = view.contentWindow;
      if (!w || !STORY_BRIDGE_ID) return false;
      var msg = {{
        storyBridge: STORY_BRIDGE_ID,
        cmd: cmd,
        fadeCss: FADE_CSS,
        fadeMs: FADE_MS
      }};
      if (extra) {{
        for (var k in extra) {{
          if (Object.prototype.hasOwnProperty.call(extra, k)) msg[k] = extra[k];
        }}
      }}
      w.postMessage(msg, '*');
      return true;
    }}

    function show(i) {{
      clearTimers();
      if (i >= pages.length) {{
        setBar('全部播完（共 ' + pages.length + ' 页）');
        whiteFade.style.transition = 'none';
        whiteFade.style.opacity = '0';
        view.style.transition = 'none';
        view.style.opacity = '1';
        view.removeAttribute('src');
        return;
      }}
      setBar('第 ' + (i + 1) + ' / ' + pages.length + ' 页（约 ' + durs[i].toFixed(2) + 's）');
      view.onload = function () {{
        if (TRANSITION_KIND === 'inner_text_fade') {{
          whiteFade.style.display = 'none';
          view.style.transition = 'none';
          view.style.opacity = '1';
          pendingAfterFadeIn = function () {{
            var ms = Math.max(100, Math.round(durs[i] * 1000));
            timer = setTimeout(function () {{
              timer = null;
              if (i + 1 >= pages.length) {{
                show(i + 1);
                return;
              }}
              pendingAfterFadeOut = function () {{ show(i + 1); }};
              clearBridgeFailSafe();
              bridgeFailSafe = setTimeout(function () {{
                bridgeFailSafe = null;
                if (pendingAfterFadeOut) {{
                  var fn = pendingAfterFadeOut;
                  pendingAfterFadeOut = null;
                  fn();
                }}
              }}, FADE_MS + 300);
              if (!postStoryBridge('fadeOut')) {{
                clearBridgeFailSafe();
                pendingAfterFadeOut = null;
                show(i + 1);
              }}
            }}, ms);
          }};
          clearBridgeFailSafe();
          bridgeFailSafe = setTimeout(function () {{
            bridgeFailSafe = null;
            if (pendingAfterFadeIn) {{
              var g = pendingAfterFadeIn;
              pendingAfterFadeIn = null;
              g();
            }}
          }}, FADE_MS + 300);
          if (!postStoryBridge('fadeIn', {{ pageIndex: i }})) {{
            clearBridgeFailSafe();
            if (pendingAfterFadeIn) {{
              var g2 = pendingAfterFadeIn;
              pendingAfterFadeIn = null;
              g2();
            }}
          }}
          return;
        }} else if (TRANSITION_KIND === 'iframe_crossfade') {{
          whiteFade.style.display = 'none';
          if (i === 0) {{
            view.style.transition = 'none';
            view.style.opacity = '1';
          }} else {{
            view.style.transition = 'opacity ' + FADE_CSS + ' ease-in-out';
            void view.offsetWidth;
            view.style.opacity = '1';
          }}
        }} else {{
          whiteFade.style.display = '';
          whiteFade.style.transition = 'none';
          whiteFade.style.opacity = '0';
        }}
        const ms = Math.max(100, Math.round(durs[i] * 1000));
        timer = setTimeout(function () {{
          timer = null;
          const isLastPage = i + 1 >= pages.length;
          if (isLastPage) {{
            show(i + 1);
            return;
          }}
          if (TRANSITION_KIND === 'iframe_crossfade') {{
            view.style.transition = 'opacity ' + FADE_CSS + ' ease-in-out';
            void view.offsetWidth;
            view.style.opacity = '0';
          }} else {{
            whiteFade.style.transition = 'opacity ' + FADE_CSS + ' ease-out';
            void whiteFade.offsetWidth;
            whiteFade.style.opacity = '1';
          }}
          fadeTimer = setTimeout(function () {{
            fadeTimer = null;
            show(i + 1);
          }}, FADE_MS);
        }}, ms);
      }};
      view.src = pages[i];
    }}

    if (TRANSITION_KIND === 'iframe_crossfade' || TRANSITION_KIND === 'inner_text_fade') {{
      whiteFade.style.display = 'none';
      view.style.opacity = '1';
    }}
    show(0);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
