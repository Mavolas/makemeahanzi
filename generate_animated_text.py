#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 Make Me a Hanzi 的单字手写 SVG 拼成“动画文字”页面。

用法示例：
  python3 generate_animated_text.py "我是最棒的" --out phrase.html --char-size 160
然后用浏览器打开 phrase.html。
"""

import argparse
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote
from typing import Optional

from backgrounds import pick_random_canvas_background

# 与 generate_story_from_txt index 方式2 约定一致；父页 postMessage 须同此 id
STORY_INDEX_BRIDGE_ID = "makemeahanzi-story-index-v1"


def _story_index_bridge_script() -> str:
    """子页监听 postMessage，在页内改 .phrase / #handOverlay 的 opacity（无需父页读 contentDocument）。"""
    br = json.dumps(STORY_INDEX_BRIDGE_ID)
    return f"""<script>
(function () {{
  var BRIDGE = {br};
  function phrase() {{
    return document.querySelector(".canvas .phrase") || document.querySelector(".phrase");
  }}
  function hand() {{
    return document.getElementById("handOverlay");
  }}
  function handOp(h) {{
    if (!h) return 1;
    var m = (h.getAttribute("style") || "").match(/opacity\\s*:\\s*([\\d.]+)/i);
    return m ? parseFloat(m[1]) : 1;
  }}
  window.addEventListener("message", function (ev) {{
    var d = ev.data;
    if (!d || d.storyBridge !== BRIDGE) return;
    var css = d.fadeCss || "1s";
    var ms = Math.max(1, parseInt(d.fadeMs, 10) || 1000);
    if (d.cmd === "fadeOut") {{
      var ph = phrase(), h = hand();
      if (!ph) {{
        window.parent.postMessage({{ storyBridge: BRIDGE, cmd: "fadeOutDone" }}, "*");
        return;
      }}
      ph.style.transition = "opacity " + css + " ease-in-out";
      void ph.offsetWidth;
      ph.style.opacity = "0";
      if (h) {{
        h.style.transition = "opacity " + css + " ease-in-out";
        h.style.opacity = "0";
      }}
      setTimeout(function () {{
        window.parent.postMessage({{ storyBridge: BRIDGE, cmd: "fadeOutDone" }}, "*");
      }}, ms);
    }}
    if (d.cmd === "fadeIn") {{
      var ph = phrase(), h = hand(), pi = d.pageIndex | 0;
      if (!ph) {{
        window.parent.postMessage({{ storyBridge: BRIDGE, cmd: "fadeInDone" }}, "*");
        return;
      }}
      if (pi === 0) {{
        ph.style.transition = "none";
        ph.style.opacity = "1";
        window.parent.postMessage({{ storyBridge: BRIDGE, cmd: "fadeInDone" }}, "*");
        return;
      }}
      ph.style.transition = "none";
      ph.style.opacity = "0";
      void ph.offsetWidth;
      ph.style.transition = "opacity " + css + " ease-in-out";
      ph.style.opacity = "1";
      if (h) {{
        var hb = handOp(h);
        h.style.transition = "none";
        h.style.opacity = "0";
        void h.offsetWidth;
        h.style.transition = "opacity " + css + " ease-in-out";
        h.style.opacity = String(hb);
      }}
      setTimeout(function () {{
        window.parent.postMessage({{ storyBridge: BRIDGE, cmd: "fadeInDone" }}, "*");
      }}, ms);
    }}
  }});
}})();
</script>"""


def resolve_hand_image_path(hand_image: str, repo_root: Path) -> Optional[Path]:
    """解析手形 PNG：先试当前工作目录，再试仓库根（与默认「手形1.png」一致）。"""
    p = Path(hand_image)
    if p.is_file():
        return p.resolve()
    q = (repo_root / hand_image).resolve()
    if q.is_file():
        return q
    return None


def parse_hand_tip_xy_from_filename(path: Path) -> tuple[Optional[float], Optional[float]]:
    """
    从文件名解析笔尖在图内的像素坐标（左上角为 0,0）。
    匹配片段形如「-3+61」→ x=3, y=61（例：手形-3+61 (1).png）。
    """
    m = re.search(r"-(\d+)\+(\d+)", path.stem)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def read_png_pixel_size(path: Path) -> Optional[tuple[int, int]]:
    """读取 PNG 的 IHDR 宽高（不依赖 Pillow）。"""
    try:
        with path.open("rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            length = int.from_bytes(f.read(4), "big")
            ctype = f.read(4)
            if ctype != b"IHDR" or length < 8:
                return None
            data = f.read(length)
            if len(data) < 8:
                return None
            w = int.from_bytes(data[0:4], "big")
            h = int.from_bytes(data[4:8], "big")
            if w <= 0 or h <= 0:
                return None
            return (w, h)
    except OSError:
        return None


ANIM_DELAY_RE = re.compile(r"animation-delay:\s*([0-9]+(?:\.[0-9]+)?)s;")
ANIM_DURATION_RE = re.compile(
    # SVG 合成时 keyframes 名称可能被加前缀：例如 c0_keyframes0
    r"(animation:\s*[^ \t]+keyframes\d+\s+)([0-9]+(?:\.[0-9]+)?)s(\s+both;)",
)
KEYFRAMES_DEF_RE = re.compile(r"@keyframes\s+(keyframes\d+)")
KEYFRAMES_REF_RE = re.compile(r"(animation:\s*)(keyframes\d+)(\s+[0-9.]+s)")

# 解析“一个字”SVG 里所有笔画动画块的总结束时间
# 例如：
#   #make-me-a-hanzi-animation-0 { animation: keyframes0 0.5s both; animation-delay: 0s; }
CHAR_ANIM_BLOCK_RE = re.compile(
    r"#([^{}]*?)make-me-a-hanzi-animation-\d+\s*\{[^}]*?"
    r"animation:\s*[^ \t]+?\s*([0-9.]+)s\s+both;[^}]*?"
    r"animation-delay:\s*([0-9.]+)s;",
    re.DOTALL,
)

# 去掉 Make Me a Hanzi 单字 SVG 里的：米字格虚线 + 浅色整字轮廓（动画开始前不要显示）
# 米字格：属性顺序不固定，用两个 lookahead
_SVG_GRID_GROUP_RE = re.compile(
    r'<g(?=[^>]*stroke="lightgray")(?=[^>]*stroke-dasharray="1,\s*1")[^>]*>[\s\S]*?</g>\s*',
    re.IGNORECASE,
)
_SVG_LIGHTGRAY_PATH_RE = re.compile(
    r'\s*<path\b[^>]*\bfill\s*=\s*["\']lightgray["\'][^>]*(?:/>|>\s*</path>)\s*',
    re.IGNORECASE,
)


def strip_svgs_preview_guides(svg_text: str) -> str:
    """移除米字格/对角线虚线组，以及 fill=lightgray 的预览字形。"""
    svg_text = _SVG_GRID_GROUP_RE.sub("", svg_text, count=1)
    svg_text = _SVG_LIGHTGRAY_PATH_RE.sub("", svg_text)
    return svg_text


def normalize_stroke_color_black(svg_text: str) -> str:
    """原 SVG 动画 keyframes 里用蓝色描边书写过程，统一改为黑色。"""
    return re.sub(r"stroke:\s*blue\s*;", "stroke: black;", svg_text, flags=re.IGNORECASE)


_STYLE_STROKE_WIDTH_DECL_RE = re.compile(
    r"stroke-width\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*;",
    re.IGNORECASE,
)


def _fmt_stroke_width_num(n: float) -> str:
    s = f"{n:.4f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s


def _rescale_stroke_widths_in_css_chunk(
    chunk: str, *, target_peak_user: float, draw_ratio: float
) -> str:
    """
    将 chunk 内所有 stroke-width 从 [lo, hi] 线性映射到
    [target_peak_user * draw_ratio, target_peak_user]。
    draw_ratio=1 时全程等粗（仅 stroke-dashoffset 表现书写），解决同屏「有的笔画细灰、有的粗黑」。
    draw_ratio 越小书写过程越细；0.125 接近 MMH 原始 128/1024。
    """
    nums = [float(mm.group(1)) for mm in _STYLE_STROKE_WIDTH_DECL_RE.finditer(chunk)]
    if not nums:
        return chunk
    lo = min(nums)
    hi = max(nums)
    if hi <= 0:
        return chunk
    dr = max(0.0, min(1.0, draw_ratio))
    new_lo = target_peak_user * dr
    if abs(hi - lo) < 1e-9:
        def repl_flat(m: re.Match[str]) -> str:
            return f"stroke-width: {_fmt_stroke_width_num(target_peak_user)};"

        return _STYLE_STROKE_WIDTH_DECL_RE.sub(repl_flat, chunk)
    scale = (target_peak_user - new_lo) / (hi - lo)

    def repl(m: re.Match[str]) -> str:
        v = float(m.group(1))
        nv = new_lo + (v - lo) * scale
        return f"stroke-width: {_fmt_stroke_width_num(nv)};"

    return _STYLE_STROKE_WIDTH_DECL_RE.sub(repl, chunk)


def _rewrite_style_body_keyframes(
    style_body: str, *, target_peak_user: float, draw_ratio: float
) -> str:
    """按 @keyframes 块分段，分别统一块内线宽范围。"""
    i = 0
    parts: list[str] = []
    while i < len(style_body):
        m = re.search(r"@keyframes\b", style_body[i:], re.IGNORECASE)
        if not m:
            parts.append(style_body[i:])
            break
        parts.append(style_body[i : i + m.start()])
        j = i + m.start()
        brace_open = style_body.find("{", j)
        if brace_open == -1:
            parts.append(style_body[j:])
            break
        depth = 0
        k = brace_open
        while k < len(style_body):
            ch = style_body[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    inner = style_body[brace_open + 1 : k]
                    inner2 = _rescale_stroke_widths_in_css_chunk(
                        inner,
                        target_peak_user=target_peak_user,
                        draw_ratio=draw_ratio,
                    )
                    parts.append(style_body[j : brace_open + 1] + inner2 + "}")
                    i = k + 1
                    break
            k += 1
        else:
            parts.append(style_body[j:])
            break
    return "".join(parts)


def _flatten_all_stroke_widths_in_style_body(
    body: str, target_peak_user: float
) -> str:
    """无视 keyframes 结构，把 style 内每一处 stroke-width 都改成同一值（最彻底等粗）。"""

    def repl(m: re.Match[str]) -> str:
        return f"stroke-width: {_fmt_stroke_width_num(target_peak_user)};"

    return _STYLE_STROKE_WIDTH_DECL_RE.sub(repl, body)


def scale_stylesheet_stroke_width_to_screen_px(
    svg_text: str,
    *,
    stroke_width_px: float,
    char_size: int,
    stroke_draw_ratio: float = 1.0,
) -> str:
    """
    MMH 的 stroke-width 写在 @keyframes 里（常见书写 128、收笔 1024）。
    target_peak_user = stroke_width_px * 1024 / char_size，使线宽约等于 stroke_width_px（px）。
    每个 @keyframes 内映射 stroke-width；默认 stroke_draw_ratio=1 为全程等粗。
    stroke_width_px <= 0 时不修改。
    """
    if stroke_width_px <= 0 or char_size <= 0:
        return svg_text
    target_peak_user = stroke_width_px * 1024.0 / char_size

    def repl_style_block(m: re.Match[str]) -> str:
        attrs, body = m.group(1), m.group(2)
        # draw_ratio≈1：整段 style 内所有 stroke-width 强制同一数值，避免分段解析遗漏导致仍有细线
        if stroke_draw_ratio >= 0.999:
            new_body = _flatten_all_stroke_widths_in_style_body(body, target_peak_user)
        elif re.search(r"@keyframes\b", body, re.IGNORECASE):
            new_body = _rewrite_style_body_keyframes(
                body,
                target_peak_user=target_peak_user,
                draw_ratio=stroke_draw_ratio,
            )
        else:
            new_body = _rescale_stroke_widths_in_css_chunk(
                body,
                target_peak_user=target_peak_user,
                draw_ratio=stroke_draw_ratio,
            )
        return f"<style{attrs}>{new_body}</style>"

    return re.sub(
        r"<style([^>]*)>([\s\S]*?)</style>",
        repl_style_block,
        svg_text,
        flags=re.IGNORECASE,
    )


def load_char_svg(svg_dir: str, ch: str) -> Optional[str]:
    codepoint = ord(ch)
    path = os.path.join(svg_dir, f"{codepoint}.svg")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def namespace_svg(svg_text: str, prefix: str) -> str:
    # 1) clip path & animation element IDs：简单做字符串替换即可，覆盖：
    #    - <clipPath id="make-me-a-hanzi-clip-0">
    #    - clip-path="url(#make-me-a-hanzi-clip-0)"
    #    - id="make-me-a-hanzi-animation-0"
    #    - CSS 里的 #make-me-a-hanzi-animation-0
    svg_text = svg_text.replace("make-me-a-hanzi-animation-", f"{prefix}make-me-a-hanzi-animation-")
    svg_text = svg_text.replace("make-me-a-hanzi-clip-", f"{prefix}make-me-a-hanzi-clip-")

    # 2) keyframes 名称必须同步改：只替换 @keyframes 定义 和 animation: 引用
    svg_text = KEYFRAMES_DEF_RE.sub(r"@keyframes " + prefix + r"\1", svg_text)
    svg_text = KEYFRAMES_REF_RE.sub(r"\1" + prefix + r"\2\3", svg_text)

    return svg_text


def apply_stroke_linejoin_to_animation_paths(svg_text: str, linejoin: str) -> str:
    """
    为 id 含 make-me-a-hanzi-animation- 的 path 设置 stroke-linejoin（多段折线拐角样式）。
    须在 namespace_svg 之后调用，以匹配带前缀的 id。
    """
    lj = linejoin.strip().lower()
    if lj not in ("round", "miter", "bevel"):
        return svg_text
    attr = f'stroke-linejoin="{lj}"'

    def repl(m: re.Match[str]) -> str:
        open_, mid, close = m.group(1), m.group(2), m.group(3)
        if re.search(r"\bstroke-linejoin\s*=", mid, re.I):
            mid2 = re.sub(
                r'\bstroke-linejoin\s*=\s*["\'][^"\']*["\']',
                attr,
                mid,
                count=1,
                flags=re.I,
            )
        else:
            mid2 = mid.rstrip() + " " + attr
        return open_ + mid2 + close

    return re.sub(
        r'(<path\b)([^>]*\bid\s*=\s*["\'][^"\']*make-me-a-hanzi-animation-\d+[^"\']*["\'][^>]*)(>)',
        repl,
        svg_text,
        flags=re.IGNORECASE,
    )


def shift_animation_delays(svg_text: str, offset_seconds: float) -> str:
    def repl(m: re.Match) -> str:
        original = float(m.group(1))
        shifted = original + offset_seconds
        # 截断到 6 位小数，避免 HTML 里数字太长
        return f"animation-delay: {shifted:.6f}s;"

    return ANIM_DELAY_RE.sub(repl, svg_text)


def scale_animation_times(svg_text: str, speed: float) -> str:
    """
    speed > 1：更快（duration/delay 都除以 speed）
    speed < 1：更慢（duration/delay 都除以 speed）
    """
    if speed <= 0:
        raise ValueError("--speed must be > 0")

    def scale_num(m: re.Match) -> str:
        v = float(m.group(1))
        return f"{v / speed:.6f}"

    svg_text = ANIM_DELAY_RE.sub(lambda m: "animation-delay: " + f"{float(m.group(1)) / speed:.6f}" + "s;", svg_text)
    svg_text = ANIM_DURATION_RE.sub(
        lambda m: m.group(1) + f"{float(m.group(2)) / speed:.6f}" + "s" + m.group(3),
        svg_text,
    )
    return svg_text


def get_char_total_duration_seconds(svg_text: str) -> float:
    """
    返回一个字 SVG 动画的总时长（从 0 开始计秒）。
    通过找到每个笔画动画块的 (delay + duration) 的最大值计算。
    """
    ends: list[float] = []
    for m in CHAR_ANIM_BLOCK_RE.finditer(svg_text):
        duration_s = float(m.group(2))
        delay_s = float(m.group(3))
        ends.append(delay_s + duration_s)
    return max(ends) if ends else 0.0


def estimate_phrase_html_duration_seconds(html: str, *, fallback_seconds: float = 3.0) -> float:
    """
    整页 HTML（含多个字、带 c0_ 等前缀的 #...make-me-a-hanzi-animation-N）上，
    取所有笔画动画块 (animation-delay + duration) 的最大值，与 get_char_total_duration_seconds 同源。
    """
    best = 0.0
    for m in CHAR_ANIM_BLOCK_RE.finditer(html):
        duration_s = float(m.group(2))
        delay_s = float(m.group(3))
        best = max(best, delay_s + duration_s)
    return best if best > 0 else fallback_seconds


def svg_to_html(svg_text: str, char_size: int) -> str:
    # 仅靠 CSS 缩放即可，所以不额外改 svg 标签属性
    # 注意：svg_text 本身已经有 <svg ...>...</svg>
    return f'<div class="char">{svg_text}</div>'


def inject_hand_image(
    svg_text: str,
    prefix: str,
    hand_image_href: str,
    hand_width: int,
    hand_height: int,
    opacity: float,
    debug_show: bool = False,
) -> str:
    """把手形图注入到单字 SVG 内（动画样式之后），并初始隐藏）。"""
    init_visibility = "visible" if debug_show else "hidden"
    insert = (
        f'\n    <image '
        f'id="{prefix}hand" '
        f'href="{hand_image_href}" '
        f'x="0" y="0" '
        f'width="{hand_width}" height="{hand_height}" '
        f'style="visibility:{init_visibility}; opacity:{opacity}; pointer-events:none;" />\n'
    )
    # 为了让手形永远在“最上层”，把它插到变换后的主绘制 <g> 的最后面（最后一个 </g> 之前）
    # 否则手形可能被后续 path 绘制覆盖，看起来“手不见”。
    svg_end = svg_text.rfind("</svg>")
    if svg_end == -1:
        return svg_text
    g_end_pos = svg_text.rfind("</g>", 0, svg_end)
    if g_end_pos == -1:
        return svg_text
    return svg_text[:g_end_pos] + insert + svg_text[g_end_pos:]


def extract_stroke_timeline(svg_text: str, prefix: str) -> list[dict]:
    """从单字 SVG 的 CSS 中提取每一笔的 delay/duration，供 JS 跟随。"""
    rule_re = re.compile(
        rf"#({re.escape(prefix)}make-me-a-hanzi-animation-(\d+))\s*\{{(.*?)\}}\s*",
        re.DOTALL,
    )

    blocks: list[dict] = []
    for m in rule_re.finditer(svg_text):
        full_id = m.group(1)  # e.g. c0_make-me-a-hanzi-animation-0
        stroke_idx = int(m.group(2))
        block = m.group(3)

        anim_m = re.search(
            r"animation:\s*([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)s\s+both;",
            block,
        )
        delay_m = re.search(r"animation-delay:\s*([0-9]+(?:\.[0-9]+)?)s;", block)
        if not anim_m or not delay_m:
            continue
        keyframes_name = anim_m.group(1)
        duration_s = float(anim_m.group(2))
        delay_s = float(delay_m.group(1))

        # 从 keyframes 中找：stroke-dashoffset 变为 0 的 step-end 百分比
        # 为了避免用正则误截断（keyframes 内部还有多个 `{}`），这里用“定位块起止”的方式切片。
        step_at = 1.0
        kstart_m = re.search(rf"@keyframes\s+{re.escape(keyframes_name)}\s*\{{", svg_text)
        if kstart_m:
            kstart = kstart_m.start()
            # 下一个 keyframes 定义的位置（作为结束边界）
            knext = svg_text.find("@keyframes ", kstart + 1)
            kbody = svg_text[kstart: knext if knext != -1 else len(svg_text)]

            step_m = re.search(
                r"([0-9]+(?:\.[0-9]+)?)%\s*\{[^}]*animation-timing-function\s*:\s*step-end;[^}]*stroke-dashoffset\s*:\s*0",
                kbody,
            )
            if step_m:
                step_at = float(step_m.group(1)) / 100.0
        if step_at <= 0:
            step_at = 1.0

        blocks.append(
            {
                "id": full_id,
                "strokeIndex": stroke_idx,
                "duration": duration_s,
                "delay": delay_s,
                "stepAt": step_at,
            }
        )

    blocks.sort(key=lambda x: x["strokeIndex"])
    return blocks


def build_hand_js(
    tracks: list[dict],
    rotate_hand: bool,
    hotspot_x_ratio: float,
    hotspot_y_ratio: float,
    flip_x: bool,
    flip_y: bool,
    rotate_extra_deg: float,
) -> str:
    """生成一段 JS：让手形沿着当前笔画 path 移动。"""
    import json

    tracks_json = json.dumps(tracks, ensure_ascii=False)
    flipX = -1 if flip_x else 1
    flipY = -1 if flip_y else 1
    return f"""
<script>
  const handTracks = {tracks_json};
  const rotateHand = {str(bool(rotate_hand)).lower()};
  const hotspotXRatio = {hotspot_x_ratio};
  const hotspotYRatio = {hotspot_y_ratio};
  const flipX = {flipX};
  const flipY = {flipY};
  const rotateExtraDeg = {rotate_extra_deg};

  function updateHandForTrack(track, t) {{
    const img = document.getElementById(track.handId);
    if (!img) return;

    // 写完一个字就隐藏：避免上一字最后笔画结束后手还一直停在那儿
    const strokes = track.strokes || [];
    if (strokes.length > 0) {{
      const last = strokes[strokes.length - 1];
      const end = (last.delay || 0) + (last.duration || 0);
      if (t > end) {{
        img.style.visibility = 'hidden';
        return;
      }}
    }}

    let active = null;
    for (const s of track.strokes) {{
      if (!s.pathLen) continue;
      if (t >= s.delay) {{
        active = s;
      }} else {{
        break;
      }}
    }}
    if (!active) return;

    img.style.visibility = 'visible';

    const dur = active.duration || 0.000001;
    const stepAt = (active.stepAt === undefined ? 1.0 : active.stepAt);
    // SVG 里大概率使用 step-end 在某个百分比瞬间完成该笔，因此笔尖轨迹也做同样的分段：
    //   0..stepAt: 线性移动
    //   >stepAt: 保持在末端
    const tLocal = t - active.delay;
    let p = 0;
    if (tLocal <= 0) {{
      p = 0;
    }} else if (stepAt >= 1.0) {{
      p = tLocal / dur;
    }} else {{
      const linearDur = dur * stepAt;
      p = tLocal >= linearDur ? 1 : (tLocal / linearDur);
    }}
    p = Math.min(1, Math.max(0, p));

    const L = active.pathLen * p;
    const pt = active.path.getPointAtLength(L);

    const p2 = Math.min(1, p + 0.01);
    const pt2 = active.path.getPointAtLength(active.pathLen * p2);
    const angle = Math.atan2(pt2.y - pt.y, pt2.x - pt.x) * 180 / Math.PI;

    const hotspotX = track.width * hotspotXRatio;
    const hotspotY = track.height * hotspotYRatio;

    if (rotateHand) {{
      img.setAttribute(
        'transform',
        'translate(' + pt.x + ' ' + pt.y + ') rotate(' + angle + ') translate(' + (-hotspotX) + ' ' + (-hotspotY) + ') rotate(' + rotateExtraDeg + ') scale(' + flipX + ' ' + flipY + ')'
      );
    }} else {{
      img.setAttribute(
        'transform',
        'translate(' + pt.x + ' ' + pt.y + ') translate(' + (-hotspotX) + ' ' + (-hotspotY) + ') rotate(' + rotateExtraDeg + ') scale(' + flipX + ' ' + flipY + ')'
      );
    }}
  }}

  function initTracks() {{
    for (const track of handTracks) {{
      for (const s of track.strokes) {{
        const path = document.getElementById(s.id);
        if (!path) continue;
        s.path = path;
        s.pathLen = path.getTotalLength();
      }}
      track.strokes.sort((a,b) => a.delay - b.delay);
    }}
  }}

  initTracks();

  const start = performance.now();
  function tick(now) {{
    const t = (now - start) / 1000.0;
    for (const track of handTracks) {{
      const first = track.strokes[0];
      if (first && t < first.delay) {{
        const img = document.getElementById(track.handId);
        if (img) img.style.visibility = 'hidden';
        continue;
      }}
      updateHandForTrack(track, t);
    }}
    requestAnimationFrame(tick);
  }}
  requestAnimationFrame(tick);
</script>
"""


def build_hand_overlay_js(
    stroke_items: list[dict],
    hand_image_href: str,
    hand_width: int,
    hand_height: int,
    hand_opacity: float,
    hotspot_x_ratio: float,
    hotspot_y_ratio: float,
    rotate_hand: bool,
    flip_x: bool,
    flip_y: bool,
    rotate_extra_deg: float,
    debug_show: bool,
) -> str:
    import json

    # 确保按时间升序，JS 用于查找 active
    stroke_items = sorted(stroke_items, key=lambda x: x["delay"])

    flipX = -1 if flip_x else 1
    flipY = -1 if flip_y else 1
    hotspotXpx = hand_width * hotspot_x_ratio
    hotspotYpx = hand_height * hotspot_y_ratio

    items_json = json.dumps(stroke_items, ensure_ascii=False)
    # 固定用 viewBox 0..1024 的坐标 -> 转屏坐标用 getScreenCTM，避免手工映射缩放
    return f"""
<img
  id="handOverlay"
  src="{html.escape(hand_image_href)}"
  width="{hand_width}"
  height="{hand_height}"
  style="
    position: fixed;
    left: 0;
    top: 0;
    width: {hand_width}px;
    height: {hand_height}px;
    opacity: {hand_opacity};
    visibility: {'visible' if debug_show else 'hidden'};
    pointer-events: none;
    z-index: 9999;
    transform-origin: 0 0;
    transform: translate({-hotspotXpx}px, {-hotspotYpx}px) rotate(0deg) scale({flipX}, {flipY});
  "
/>
<script>
  const handStrokes = {items_json};
  const rotateHand = {str(bool(rotate_hand)).lower()};
  const flipX = {flipX};
  const flipY = {flipY};
  const rotateExtraDeg = {rotate_extra_deg};
  const hotspotXpx = {hotspotXpx};
  const hotspotYpx = {hotspotYpx};

  function hide() {{
    const img = document.getElementById('handOverlay');
    if (img) img.style.visibility = 'hidden';
  }}

  function setVisible() {{
    const img = document.getElementById('handOverlay');
    if (img) img.style.visibility = 'visible';
  }}

  function svgPointToScreen(path, pt) {{
    // pt 是 path 的 user space 坐标，转到屏幕像素坐标
    const svg = path.ownerSVGElement;
    const ptSvg = svg.createSVGPoint();
    ptSvg.x = pt.x;
    ptSvg.y = pt.y;
    const ctm = path.getScreenCTM();
    if (!ctm) return null;
    const screen = ptSvg.matrixTransform(ctm);
    return {{x: screen.x, y: screen.y}};
  }}

  function updateForTime(t) {{
    const img = document.getElementById('handOverlay');
    if (!img) return;
    if (!handStrokes.length) return;

    let active = null;
    for (const s of handStrokes) {{
      if (t >= s.delay) {{
        active = s;
      }} else {{
        break;
      }}
    }}

    if (!active) {{
      img.style.visibility = 'hidden';
      return;
    }}
    img.style.visibility = 'visible';

    const path = document.getElementById(active.id);
    if (!path) return;
    const dur = active.duration || 0.000001;
    const stepAt = (active.stepAt === undefined ? 1.0 : active.stepAt);

    // step-end 对齐：0..stepAt 线性，超过 stepAt 保持在末端
    const tLocal = t - active.delay;
    let p = 0;
    if (tLocal <= 0) {{
      p = 0;
    }} else if (stepAt >= 1.0) {{
      p = tLocal / dur;
    }} else {{
      const linearDur = dur * stepAt;
      p = tLocal >= linearDur ? 1 : (tLocal / linearDur);
    }}
    p = Math.min(1, Math.max(0, p));

    const pathLen = path.getTotalLength();
    const L = pathLen * p;
    const pt = path.getPointAtLength(L);
    const p2 = Math.min(1, p + 0.01);
    const pt2 = path.getPointAtLength(pathLen * p2);

    const angle = Math.atan2(pt2.y - pt.y, pt2.x - pt.x) * 180 / Math.PI;
    const finalRot = rotateHand ? (angle + rotateExtraDeg) : rotateExtraDeg;

    const screenPt = svgPointToScreen(path, pt);
    if (!screenPt) return;

    img.style.left = screenPt.x + 'px';
    img.style.top = screenPt.y + 'px';
    img.style.transform =
      'translate(' + (-hotspotXpx) + 'px, ' + (-hotspotYpx) + 'px)'
      + ' rotate(' + finalRot + 'deg)'
      + ' scale(' + flipX + ',' + flipY + ')';
  }}

  const start = performance.now();
  function tick(now) {{
    const t = (now - start) / 1000.0;
    updateForTime(t);
    requestAnimationFrame(tick);
  }}
  requestAnimationFrame(tick);
</script>
"""


def build_html(
    phrase: str,
    pieces_html: list[str],
    char_size: int,
    gap_px: int,
    out_title: str,
    hand_js: str = "",
    canvas_width: int = 1054,
    canvas_height: int = 588,
    canvas_bg: str = "#d6e9f8",
    line_gap_px: int = 24,
    canvas_bg_image: Optional[str] = None,
    story_index_bridge: bool = False,
    transparent_canvas_backdrop: bool = False,
) -> str:
    # 固定画布：底色/背景图在 .canvas-backdrop，.phrase 叠在上层（story index 方式2 仅淡出 .phrase 时背景不动）
    safe_title = html.escape(out_title)
    safe_bg = html.escape(canvas_bg)
    if transparent_canvas_backdrop:
        canvas_bg_css = "background: transparent;"
        body_page_bg = "transparent"
    elif canvas_bg_image:
        # url() 内对中文/空格等编码；文件与 HTML 同目录时用文件名即可
        safe_img_url = quote(canvas_bg_image, safe="")
        canvas_bg_css = f"""background-color: {safe_bg};
      background-image: url(\"{safe_img_url}\");
      background-size: cover;
      background-position: center;
      background-repeat: no-repeat;"""
        body_page_bg = "#e8e8e8"
    else:
        canvas_bg_css = f"background: {safe_bg};"
        body_page_bg = "#e8e8e8"
    bridge_html = _story_index_bridge_script() if story_index_bridge else ""
    return f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: {body_page_bg};
      font-family: system-ui, -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .canvas {{
      position: relative;
      width: {canvas_width}px;
      height: {canvas_height}px;
      box-sizing: border-box;
      padding: 16px;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
      background: transparent;
    }}
    .canvas-backdrop {{
      position: absolute;
      inset: 0;
      z-index: 0;
      pointer-events: none;
      {canvas_bg_css}
    }}
    .phrase {{
      position: relative;
      z-index: 1;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      align-content: center;
      justify-content: center;
      column-gap: {gap_px}px;
      row-gap: {line_gap_px}px;
      max-width: 100%;
    }}
    .char svg {{
      width: {char_size}px;
      height: {char_size}px;
      display: block;
    }}
    .missing {{
      width: {char_size}px;
      height: {char_size}px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: {char_size}px;
      line-height: 1;
      color: #000;
      background: #f6f6f6;
      border: 1px solid #e6e6e6;
      box-sizing: border-box;
    }}
  </style>
</head>
<body>
  <div class="canvas">
    <div class="canvas-backdrop" aria-hidden="true"></div>
    <div class="phrase">
      {''.join(pieces_html)}
    </div>
  </div>
  {hand_js}
  {bridge_html}
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="拼接字的动画 SVG，生成“动画文字”HTML。")
    # phrase 支持可选输入：不传则交互式输入（方便测试/临时生成）
    parser.add_argument("phrase", nargs="?", default=None, help="要生成的文字，例如：我是最棒的")
    parser.add_argument("--out", default="phrase.html", help="输出 HTML 文件路径")
    parser.add_argument("--svg-dir", default="svgs", help="动画 SVG 目录（默认：svgs）")
    parser.add_argument("--char-size", type=int, default=146, help="每个字显示尺寸（px）")
    parser.add_argument(
        "--stroke-width-px",
        type=float,
        default=28.0,
        metavar="PX",
        help="笔画线宽（约等于屏幕像素），按 viewBox 与 --char-size 换算后写入 SVG 动画；≤0 保留源 SVG 线宽",
    )
    parser.add_argument(
        "--stroke-draw-ratio",
        type=float,
        default=1.0,
        metavar="R",
        help="每个笔画动画内：书写最细相对写完最粗的比例（0～1）。1=全程等粗（推荐，避免细灰/粗黑混杂）；0.5～0.65 略有粗细变化；0.125≈MMH 原始 128/1024",
    )
    parser.add_argument(
        "--stroke-linejoin",
        choices=["round", "miter", "bevel", "none"],
        default="round",
        help="动画 path 的 stroke-linejoin（折角）：round 圆角、miter 尖角、bevel 斜切；none 不插入/不改该属性",
    )
    parser.add_argument("--gap-px", type=int, default=8, help="字与字之间间距（px）")
    parser.add_argument("--line-gap-px", type=int, default=46, help="两行之间的垂直间距（px）")
    parser.add_argument("--canvas-width", type=int, default=1054, help="固定背景画布宽度（px）")
    parser.add_argument("--canvas-height", type=int, default=588, help="固定背景画布高度（px）")
    parser.add_argument(
        "--canvas-bg",
        default=None,
        metavar="HEX",
        help="画布背景色（如 #d6e9f8）；省略则每次从 backgrounds 随机淡色；与背景图并存时为底色",
    )
    parser.add_argument(
        "--canvas-bg-image",
        default=None,
        metavar="FILE",
        help="与输出 HTML 同目录下的背景图文件名；CSS background-size:cover 铺满画布（过小会等比放大）；通常由 story 脚本复制素材后传入",
    )
    parser.add_argument(
        "--story-index-bridge",
        action="store_true",
        help="注入 postMessage 桥接脚本，供 generate_story index 方式2 在子页内淡出文字（file:// 下父页无法读 iframe 文档时必需）",
    )
    parser.add_argument(
        "--transparent-canvas-backdrop",
        action="store_true",
        help="子页 .canvas-backdrop 透明、body 背景透明；由 index 的 storyStaticBackdrop 铺底，避免 iframe 换页时背景闪断",
    )
    parser.add_argument("--start-delay", type=float, default=0.0, help="第一个字的起始延迟（秒）")
    parser.add_argument(
        "--sequence-mode",
        choices=["sequential", "fixed-delay"],
        default="sequential",
        help="sequential：按每个字动画结束时间排队；fixed-delay：使用 --gap-delay 固定间隔",
    )
    parser.add_argument("--gap-delay", type=float, default=0.8, help="fixed-delay 模式下：字与字之间的起始延迟（秒）")
    parser.add_argument("--char-gap", type=float, default=0.15, help="sequential 模式下：字与字之间额外间隔（秒）")
    parser.add_argument(
        "--speed",
        type=float,
        default=8.0,
        help="速度倍数（默认 6）：数值越大越快；delay/duration 均除以该值。1≈未加速；与 story_meta 时长估算一致。",
    )
    # 默认直接跟随手形：不需要用户再传参数
    # 假设输出 HTML 和图片都在当前脚本运行目录（仓库根目录）
    parser.add_argument(
        "--hand-image",
        default="手形-3+61 (1).png",
        help="手形 PNG 路径（默认：手形-3+61 (1).png；文件名中含 -Mx+Ny 时可自动识别笔尖像素坐标）；"
        "会在当前目录与仓库根查找；生成时复制到输出 HTML 同目录",
    )
    parser.add_argument(
        "--hand-width",
        type=int,
        default=None,
        metavar="PX",
        help="手形显示宽度（px）；省略则用 --hand-image 的 PNG 原始宽度",
    )
    parser.add_argument(
        "--hand-height",
        type=int,
        default=None,
        metavar="PX",
        help="手形显示高度（px）；省略则用 --hand-image 的 PNG 原始高度",
    )
    parser.add_argument("--hand-opacity", type=float, default=1.0, help="手形透明度（0-1）")
    parser.add_argument(
        "--hand-tip-x",
        type=float,
        default=None,
        metavar="PX",
        help="笔尖在 PNG 内的 x 像素（左上角为 0）；与 --hand-tip-y 成对使用则优先于热点比例",
    )
    parser.add_argument(
        "--hand-tip-y",
        type=float,
        default=None,
        metavar="PX",
        help="笔尖在 PNG 内的 y 像素；若均未指定且文件名含 -Mx+Ny 则自动解析",
    )
    parser.add_argument(
        "--hand-hotspot-x",
        type=float,
        default=0.02,
        help="未使用笔尖像素时：热点在显示宽度上的比例(0-1)",
    )
    parser.add_argument(
        "--hand-hotspot-y",
        type=float,
        default=0.2,
        help="未使用笔尖像素时：热点在显示高度上的比例(0-1)",
    )
    parser.add_argument("--hand-rotate", action="store_true", help="是否让手形沿切线旋转")
    # 默认水平/垂直翻转以贴合笔尖方向；手形尺寸默认取 PNG 原始像素 × --hand-scale
    parser.add_argument("--hand-flip-x", action="store_true", default=True, help="水平翻转手形（解决反着）")
    parser.add_argument("--hand-flip-y", action="store_true", default=True, help="垂直翻转手形（默认不翻）")
    parser.add_argument("--hand-rotate-extra", type=float, default=180.0, help="额外旋转角度（度），用于微调手的方向")
    parser.add_argument(
        "--hand-scale",
        type=float,
        default=1.0,
        help="在（指定或从图片读出的）宽高上再乘的倍数，默认 1 即不额外缩放",
    )
    parser.add_argument("--hand-debug-show", action="store_true", help="调试：不等 JS 就先把手形显示出来（用于排查“不见了”）")
    parser.add_argument(
        "--hand-mode",
        choices=["overlay", "per-char"],
        default="overlay",
        help="overlay：手形作为页面顶层覆盖层（不会被其它字遮挡）；per-char：注入到每个字 SVG 内",
    )
    args = parser.parse_args()

    phrase = args.phrase
    if phrase is None:
        # 不手动输入时：默认输出两行文本
        phrase = "测试测试测试\n看起来不错"
    svg_dir = args.svg_dir
    out_path = args.out
    repo_root = Path(__file__).resolve().parent

    # 手形图：解析源文件，并复制到输出 HTML 同目录（避免 story_output/page.html 相对路径找不到根目录的 PNG）
    hand_image_url: Optional[str] = None
    hand_src: Optional[Path] = None
    if args.hand_image:
        hand_src = resolve_hand_image_path(args.hand_image, repo_root)
        if hand_src:
            out_parent = Path(out_path).expanduser().resolve().parent
            out_parent.mkdir(parents=True, exist_ok=True)
            dest_hand = out_parent / hand_src.name
            if dest_hand.resolve() != hand_src.resolve():
                shutil.copy2(hand_src, dest_hand)
            # HTML 内只用文件名，与输出文件同目录即可加载
            hand_image_url = hand_src.name
        else:
            print(
                f"警告：找不到手形图片「{args.hand_image}」（已试过当前目录与 {repo_root}），将不显示手形。",
                file=sys.stderr,
            )

    # 手形占位尺寸：默认取 PNG 原始像素；可单独覆盖宽/高；再乘 --hand-scale
    _hand_fallback_w, _hand_fallback_h = 120, 105
    hand_intrinsic_w, hand_intrinsic_h = _hand_fallback_w, _hand_fallback_h
    if hand_src:
        sz = read_png_pixel_size(hand_src)
        if sz:
            hand_intrinsic_w, hand_intrinsic_h = sz
        else:
            print(
                f"警告：无法从「{hand_src.name}」读取 PNG 宽高，手形尺寸暂用 {_hand_fallback_w}×{_hand_fallback_h}",
                file=sys.stderr,
            )
    base_hand_w = args.hand_width if args.hand_width is not None else hand_intrinsic_w
    base_hand_h = args.hand_height if args.hand_height is not None else hand_intrinsic_h
    if args.hand_scale <= 0:
        raise SystemExit("--hand-scale 须为大于 0 的数")
    hand_w = max(1, int(round(base_hand_w * args.hand_scale)))
    hand_h = max(1, int(round(base_hand_h * args.hand_scale)))

    tip_x = args.hand_tip_x
    tip_y = args.hand_tip_y
    if (tip_x is not None) ^ (tip_y is not None):
        raise SystemExit("--hand-tip-x 与 --hand-tip-y 须同时指定，或均省略以使用文件名/比例")
    if tip_x is None and tip_y is None and hand_src:
        ax, ay = parse_hand_tip_xy_from_filename(hand_src)
        if ax is not None and ay is not None:
            tip_x, tip_y = ax, ay
            print(
                f"手形笔尖（自文件名，相对 PNG 左上角）: x={tip_x:g}, y={tip_y:g} → "
                f"比例 {tip_x / hand_intrinsic_w:.4f}, {tip_y / hand_intrinsic_h:.4f}"
            )
    if tip_x is not None and tip_y is not None:
        hotspot_x_ratio = tip_x / hand_intrinsic_w
        hotspot_y_ratio = tip_y / hand_intrinsic_h
    else:
        hotspot_x_ratio = args.hand_hotspot_x
        hotspot_y_ratio = args.hand_hotspot_y

    if not os.path.isdir(svg_dir):
        raise SystemExit(f"找不到 svg 目录：{svg_dir}（请检查 --svg-dir）")

    pieces_html: list[str] = []
    hand_tracks: list[dict] = []
    stroke_items: list[dict] = []
    current_offset = args.start_delay
    for i, ch in enumerate(phrase):
        if ch == "\n":
            # flex 换行：占满一整行
            pieces_html.append('<div style="flex-basis:100%; height:0;"></div>')
            continue
        if ch.isspace():
            # 空格：直接用一个占位块控制换行/间距
            pieces_html.append(f'<div style="width:{args.char_size // 3}px"></div>')
            continue

        svg_text = load_char_svg(svg_dir, ch)
        if svg_text is None:
            pieces_html.append(f'<div class="missing">{html.escape(ch)}</div>')
            continue

        svg_text = strip_svgs_preview_guides(svg_text)
        svg_text = normalize_stroke_color_black(svg_text)
        svg_text = scale_stylesheet_stroke_width_to_screen_px(
            svg_text,
            stroke_width_px=args.stroke_width_px,
            char_size=args.char_size,
            stroke_draw_ratio=args.stroke_draw_ratio,
        )

        prefix = f"c{i}_"
        svg_text = namespace_svg(svg_text, prefix=prefix)
        if args.stroke_linejoin != "none":
            svg_text = apply_stroke_linejoin_to_animation_paths(
                svg_text, args.stroke_linejoin
            )

        if args.sequence_mode == "fixed-delay":
            offset = args.start_delay + i * args.gap_delay
            svg_text = shift_animation_delays(svg_text, offset_seconds=offset)
        else:
            # sequential：一个字的起始时间 = current_offset
            # 注意：get_char_total_duration_seconds() 必须基于“未加 offset 的原始字”，
            # 否则会把 offset 重复加到 current_offset 里，导致后面的字等待被指数放大。
            char_duration_local = get_char_total_duration_seconds(svg_text)
            offset = current_offset
            svg_text = shift_animation_delays(svg_text, offset_seconds=offset)
            current_offset = offset + char_duration_local + args.char_gap

        svg_text = scale_animation_times(svg_text, speed=args.speed)

        if hand_image_url:
            timeline = extract_stroke_timeline(svg_text, prefix=prefix)
            if timeline:
                if args.hand_mode == "per-char":
                    svg_text = inject_hand_image(
                        svg_text=svg_text,
                        prefix=prefix,
                        hand_image_href=hand_image_url,
                        hand_width=hand_w,
                        hand_height=hand_h,
                        opacity=args.hand_opacity,
                        debug_show=args.hand_debug_show,
                    )
                    hand_tracks.append(
                        {
                            "handId": f"{prefix}hand",
                            "width": hand_w,
                            "height": hand_h,
                            "strokes": [
                                {
                                    "id": s["id"],
                                    "delay": s["delay"],
                                    "duration": s["duration"],
                                    "stepAt": s.get("stepAt", 1.0),
                                }
                                for s in timeline
                            ],
                        }
                    )
                else:
                    # overlay：不要注入到每个字 SVG，而是收集所有笔画时间线，交给全局顶层手形覆盖层
                    stroke_items.extend(
                        [
                            {
                                "id": s["id"],
                                "delay": s["delay"],
                                "duration": s["duration"],
                                "stepAt": s.get("stepAt", 1.0),
                            }
                            for s in timeline
                        ]
                    )

        pieces_html.append(svg_to_html(svg_text, char_size=args.char_size))

    hand_js = ""
    if hand_image_url:
        if args.hand_mode == "per-char" and hand_tracks:
            hand_js = build_hand_js(
                tracks=hand_tracks,
                rotate_hand=args.hand_rotate,
                hotspot_x_ratio=hotspot_x_ratio,
                hotspot_y_ratio=hotspot_y_ratio,
                flip_x=args.hand_flip_x,
                flip_y=args.hand_flip_y,
                rotate_extra_deg=args.hand_rotate_extra,
            )
        elif args.hand_mode == "overlay" and stroke_items:
            hand_js = build_hand_overlay_js(
                stroke_items=stroke_items,
                hand_image_href=hand_image_url,
                hand_width=hand_w,
                hand_height=hand_h,
                hand_opacity=args.hand_opacity,
                hotspot_x_ratio=hotspot_x_ratio,
                hotspot_y_ratio=hotspot_y_ratio,
                rotate_hand=args.hand_rotate,
                flip_x=args.hand_flip_x,
                flip_y=args.hand_flip_y,
                rotate_extra_deg=args.hand_rotate_extra,
                debug_show=args.hand_debug_show,
            )

    if args.canvas_bg:
        canvas_bg = args.canvas_bg
    else:
        _picked = pick_random_canvas_background()
        canvas_bg = _picked.hex
        if args.canvas_bg_image:
            print(
                f"画布底色素（随机）：{_picked.name} {_picked.hex}（背景图 {args.canvas_bg_image}）"
            )
        else:
            print(f"画布背景（随机）：{_picked.name} {_picked.hex}")

    html_out = build_html(
        phrase=phrase,
        pieces_html=pieces_html,
        char_size=args.char_size,
        gap_px=args.gap_px,
        out_title=f"Animated Text: {phrase}",
        hand_js=hand_js,
        canvas_width=args.canvas_width,
        canvas_height=args.canvas_height,
        canvas_bg=canvas_bg,
        line_gap_px=args.line_gap_px,
        canvas_bg_image=args.canvas_bg_image,
        story_index_bridge=bool(args.story_index_bridge),
        transparent_canvas_backdrop=bool(args.transparent_canvas_backdrop),
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"生成完成：{out_path}")


if __name__ == "__main__":
    main()

