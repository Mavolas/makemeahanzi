#!/usr/bin/env node
/**
 * HTML(含CSS动画) -> MP4
 * 思路：用 Playwright 渲染页面 -> 每隔 1/fps 截一帧 PNG -> ffmpeg 合成 MP4
 *
 * 依赖：
 *  1) npm i playwright
 *  2) 系统需能找到 ffmpeg（可通过 brew/下载二进制安装）
 */

const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) continue;
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next && !next.startsWith('--')) {
      out[key] = next;
      i++;
    } else {
      out[key] = true;
    }
  }
  return out;
}

function pad5(n) {
  return String(n).padStart(5, '0');
}

function computeDurationSecondsFromHtml(htmlText) {
  // CSS 规则块形如：
  // #id { animation: keyframes0 0.5s both; ... animation-delay: 0s; ... }
  const re = /#[A-Za-z0-9_\-:]+[\s\S]*?animation:\s*[^\s]+\s+([0-9]+(?:\.[0-9]+)?)s\s+both;[\s\S]*?animation-delay:\s*([0-9]+(?:\.[0-9]+)?)s;/g;
  let m;
  let best = 0;
  while ((m = re.exec(htmlText))) {
    const dur = parseFloat(m[1]);
    const delay = parseFloat(m[2]);
    if (Number.isFinite(dur) && Number.isFinite(delay)) {
      best = Math.max(best, dur + delay);
    }
  }
  return best;
}

async function main() {
  const args = parseArgs(process.argv);

  const input = args.in || args.input;
  const outMp4 = args.out || args.output || 'out.mp4';
  const fps = parseFloat(args.fps || '10');
  const width = parseInt(args.width || '1000', 10);
  const height = parseInt(args.height || '600', 10);
  const durationSecondsArg = args.duration ? parseFloat(args.duration) : null;
  const extraSeconds = parseFloat(args.extra || '0.2');

  if (!input) {
    console.error('用法：node export_mp4_from_html.js --in phrase.html --out out.mp4 [--fps 10] [--width 1000] [--height 600] [--duration 5]');
    process.exit(1);
  }

  const absIn = path.resolve(input);
  const absOut = path.resolve(outMp4);

  const htmlText = fs.readFileSync(absIn, 'utf8');
  const computed = computeDurationSecondsFromHtml(htmlText);
  const durationSeconds = (durationSecondsArg && Number.isFinite(durationSecondsArg) ? durationSecondsArg : computed) + extraSeconds;
  if (!durationSeconds || !Number.isFinite(durationSeconds)) {
    throw new Error('无法从 HTML 计算动画时长，请手动加 --duration');
  }
  console.log(`[export] input=${absIn}`);
  console.log(`[export] computedDuration=${computed}s, usingDuration=${durationSeconds}s, fps=${fps}`);

  let playwright;
  try {
    playwright = require('playwright');
  } catch (e) {
    throw new Error('缺少 playwright：请先执行 npm i playwright');
  }

  const { chromium } = playwright;
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });

  // file:// 打开本地 html
  await page.goto('file://' + absIn, { waitUntil: 'load' });

  // 给浏览器一点时间启动 CSS 动画
  await page.waitForTimeout(200);

  const frameDir = path.join(path.dirname(absOut), `.frames_${path.basename(absOut)}_${Date.now()}`);
  fs.mkdirSync(frameDir, { recursive: true });

  const frameIntervalMs = 1000 / fps;
  const frameCount = Math.ceil(durationSeconds * fps);
  console.log(`[export] frameCount=${frameCount}, frameDir=${frameDir}`);

  const startPerf = await page.evaluate(() => performance.now());

  for (let i = 0; i < frameCount; i++) {
    const target = startPerf + i * frameIntervalMs;
    // 简单等待到接近目标时间（避免每次 waitForTimeout 漂移过大）
    for (;;) {
      const now = await page.evaluate(() => performance.now());
      const remain = target - now;
      if (remain <= 0) break;
      if (remain > 16) await page.waitForTimeout(Math.min(remain, 50));
      else break;
    }

    const framePath = path.join(frameDir, `frame_${pad5(i)}.png`);
    await page.screenshot({
      path: framePath,
      clip: { x: 0, y: 0, width, height },
    });
  }

  await browser.close();

  // 组装 mp4
  // 注意：ffmpeg 必须在 PATH 里
  const pattern = path.join(frameDir, 'frame_%05d.png');
  const ffArgs = [
    '-y',
    '-r',
    String(fps),
    '-i',
    pattern,
    '-vf',
    // 确保偶数宽高，兼容编码器
    'scale=trunc(iw/2)*2:trunc(ih/2)*2',
    '-c:v',
    'libx264',
    '-pix_fmt',
    'yuv420p',
    absOut,
  ];

  console.log(`[export] running ffmpeg -> ${absOut}`);
  childProcess.execFileSync('ffmpeg', ffArgs, { stdio: 'inherit' });

  console.log('[export] done.');
}

main().catch((err) => {
  console.error('[export] failed:', err && err.message ? err.message : err);
  process.exit(1);
});

