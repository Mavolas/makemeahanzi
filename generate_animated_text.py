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
import os
import re
from typing import Optional


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
) -> str:
    # 为了简单可靠：用 flex 排版 + gap 控制字间距
    safe_title = html.escape(out_title)
    return f"""<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    body {{
      margin: 0;
      padding: 24px;
      background: white;
      font-family: system-ui, -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .phrase {{
      display: flex;
      align-items: center;
      gap: {gap_px}px;
      flex-wrap: wrap;
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
  <div class="phrase">
    {''.join(pieces_html)}
  </div>
  {hand_js}
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="拼接字的动画 SVG，生成“动画文字”HTML。")
    # phrase 支持可选输入：不传则交互式输入（方便测试/临时生成）
    parser.add_argument("phrase", nargs="?", default=None, help="要生成的文字，例如：我是最棒的")
    parser.add_argument("--out", default="phrase.html", help="输出 HTML 文件路径")
    parser.add_argument("--svg-dir", default="svgs", help="动画 SVG 目录（默认：svgs）")
    parser.add_argument("--char-size", type=int, default=160, help="每个字显示尺寸（px）")
    parser.add_argument("--gap-px", type=int, default=20, help="字与字之间间距（px）")
    parser.add_argument("--start-delay", type=float, default=0.0, help="第一个字的起始延迟（秒）")
    parser.add_argument(
        "--sequence-mode",
        choices=["sequential", "fixed-delay"],
        default="sequential",
        help="sequential：按每个字动画结束时间排队；fixed-delay：使用 --gap-delay 固定间隔",
    )
    parser.add_argument("--gap-delay", type=float, default=0.8, help="fixed-delay 模式下：字与字之间的起始延迟（秒）")
    parser.add_argument("--char-gap", type=float, default=0.15, help="sequential 模式下：字与字之间额外间隔（秒）")
    parser.add_argument("--speed", type=float, default=4.0, help="速度倍数：1=正常，2=更快，0.5=更慢")
    # 默认直接跟随手形：不需要用户再传参数
    # 假设输出 HTML 和图片都在当前脚本运行目录（仓库根目录）
    parser.add_argument(
        "--hand-image",
        default="手形1.png",
        help="手形图片路径（默认：手形1.png）；如果文件不存在则不会注入手形",
    )
    parser.add_argument("--hand-width", type=int, default=120, help="手形显示宽度（px）")
    parser.add_argument("--hand-height", type=int, default=105, help="手形显示高度（px）")
    parser.add_argument("--hand-opacity", type=float, default=1.0, help="手形透明度（0-1）")
    parser.add_argument("--hand-hotspot-x", type=float, default=0.02, help="手形热点在宽度的比例(0-1)，用于贴合笔尖")
    parser.add_argument("--hand-hotspot-y", type=float, default=0.2, help="手形热点在高度的比例(0-1)，用于贴合笔尖")
    parser.add_argument("--hand-rotate", action="store_true", help="是否让手形沿切线旋转")
    # 默认就反转/放大，符合“直接跑就是手形正确方向和大小”的需求
    parser.add_argument("--hand-flip-x", action="store_true", default=True, help="水平翻转手形（解决反着）")
    parser.add_argument("--hand-flip-y", action="store_true", default=True, help="垂直翻转手形（默认不翻）")
    parser.add_argument("--hand-rotate-extra", type=float, default=180.0, help="额外旋转角度（度），用于微调手的方向")
    parser.add_argument("--hand-scale", type=float, default=1.8, help="手形整体缩放倍数（比直接改宽高更方便）")
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
        phrase = input("请输入要生成的文字（例如：我是最棒的）：").strip()
    svg_dir = args.svg_dir
    out_path = args.out

    # 如果默认 hand 图片不存在，就自动降级为不跟随手
    if args.hand_image and not os.path.exists(args.hand_image):
        args.hand_image = None

    if not os.path.isdir(svg_dir):
        raise SystemExit(f"找不到 svg 目录：{svg_dir}（请检查 --svg-dir）")

    pieces_html: list[str] = []
    hand_tracks: list[dict] = []
    stroke_items: list[dict] = []
    current_offset = args.start_delay
    for i, ch in enumerate(phrase):
        if ch.isspace():
            # 空格：直接用一个占位块控制换行/间距
            pieces_html.append(f'<div style="width:{args.char_size // 3}px"></div>')
            continue

        svg_text = load_char_svg(svg_dir, ch)
        if svg_text is None:
            pieces_html.append(f'<div class="missing">{html.escape(ch)}</div>')
            continue

        prefix = f"c{i}_"
        svg_text = namespace_svg(svg_text, prefix=prefix)

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

        if args.hand_image:
            timeline = extract_stroke_timeline(svg_text, prefix=prefix)
            if timeline:
                if args.hand_mode == "per-char":
                    hand_w = int(args.hand_width * args.hand_scale)
                    hand_h = int(args.hand_height * args.hand_scale)
                    svg_text = inject_hand_image(
                        svg_text=svg_text,
                        prefix=prefix,
                        hand_image_href=args.hand_image,
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
    if args.hand_image:
        if args.hand_mode == "per-char" and hand_tracks:
            hand_js = build_hand_js(
                tracks=hand_tracks,
                rotate_hand=args.hand_rotate,
                hotspot_x_ratio=args.hand_hotspot_x,
                hotspot_y_ratio=args.hand_hotspot_y,
                flip_x=args.hand_flip_x,
                flip_y=args.hand_flip_y,
                rotate_extra_deg=args.hand_rotate_extra,
            )
        elif args.hand_mode == "overlay" and stroke_items:
            hand_w = int(args.hand_width * args.hand_scale)
            hand_h = int(args.hand_height * args.hand_scale)
            hand_js = build_hand_overlay_js(
                stroke_items=stroke_items,
                hand_image_href=args.hand_image,
                hand_width=hand_w,
                hand_height=hand_h,
                hand_opacity=args.hand_opacity,
                hotspot_x_ratio=args.hand_hotspot_x,
                hotspot_y_ratio=args.hand_hotspot_y,
                rotate_hand=args.hand_rotate,
                flip_x=args.hand_flip_x,
                flip_y=args.hand_flip_y,
                rotate_extra_deg=args.hand_rotate_extra,
                debug_show=args.hand_debug_show,
            )

    html_out = build_html(
        phrase=phrase,
        pieces_html=pieces_html,
        char_size=args.char_size,
        gap_px=args.gap_px,
        out_title=f"Animated Text: {phrase}",
        hand_js=hand_js,
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"生成完成：{out_path}")


if __name__ == "__main__":
    main()

