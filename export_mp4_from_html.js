#!/usr/bin/env node
/**
 * HTML(含CSS动画) -> MP4
 * 思路：用 Playwright 渲染页面 -> 每隔 1/fps 截一帧 PNG -> ffmpeg 合成 MP4
 *
 * 支持两种输入：
 *  1) 单页：--in phrase.html（与原先相同）
 *  2) 多页故事：generate_story_from_txt.py 生成的目录（含 index.html + story_meta.json）
 *     可用 --story <目录或index.html路径>，或 --in <目录/index.html> 且同目录有 story_meta.json 时自动识别
 *
 * 依赖：
 *  1) npm i playwright
 *  2) 系统需能找到 ffmpeg（可通过 brew/下载二进制安装）
 */

const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');
const { pathToFileURL } = require('url');

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

/**
 * @param {{ input?: string, story?: string }} args
 * @returns {string | null} 故事根目录（绝对路径），非故事模式返回 null
 */
function resolveStoryRoot(args) {
  if (args.story) {
    let p = path.resolve(args.story);
    if (fs.existsSync(p) && fs.statSync(p).isFile()) {
      p = path.dirname(p);
    }
    const indexPath = path.join(p, 'index.html');
    const metaPath = path.join(p, 'story_meta.json');
    if (!fs.existsSync(indexPath)) {
      throw new Error(`--story 路径下缺少 index.html：${indexPath}`);
    }
    if (!fs.existsSync(metaPath)) {
      throw new Error(`--story 路径下缺少 story_meta.json：${metaPath}`);
    }
    return p;
  }
  if (!args.input) return null;
  const abs = path.resolve(args.input);
  if (fs.existsSync(abs) && fs.statSync(abs).isDirectory()) {
    const indexPath = path.join(abs, 'index.html');
    const metaPath = path.join(abs, 'story_meta.json');
    if (fs.existsSync(indexPath) && fs.existsSync(metaPath)) {
      return abs;
    }
  }
  if (fs.existsSync(abs) && fs.statSync(abs).isFile() && path.basename(abs) === 'index.html') {
    const metaPath = path.join(path.dirname(abs), 'story_meta.json');
    if (fs.existsSync(metaPath)) {
      return path.dirname(abs);
    }
  }
  return null;
}

function parseFadeMsFromIndexHtml(htmlText) {
  const m = htmlText.match(/const\s+FADE_MS\s*=\s*(\d+)\s*;/);
  if (m) {
    const n = parseInt(m[1], 10);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return 1000;
}

function printUsage() {
  console.error(
    [
      '用法：',
      '  单页：  node export_mp4_from_html.js --in phrase.html --out out.mp4 [--fps 48] [--width 1000] [--height 600] [--duration 5]',
      '  故事：  node export_mp4_from_html.js --story story_output --out story.mp4 [--fps 48] [--hide-bar] [--tail 0.5] [--story-pad-sec 1]',
      '         或将 --in 指向含 index.html + story_meta.json 的目录（或该目录下的 index.html）',
    ].join('\n'),
  );
}

/**
 * 等待 #view 指向的 story 内页 frame 加载完成。
 * 使用 Playwright 的 frames()，避免 file:// 下 contentDocument 在页面函数里不可见、以及 waitForFunction 第二参被当成 fn 参数导致仍走 30s 默认超时。
 */
async function waitUntilIframeReady(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const remain = deadline - Date.now();
    const frames = page.frames();
    const child = frames.find((f) => {
      const u = f.url() || '';
      return /page_\d+\.html/i.test(u);
    });
    if (child) {
      await child.waitForLoadState('load', { timeout: Math.max(1000, remain) });
      return;
    }
    await page.waitForTimeout(50);
  }
  throw new Error('story iframe 内页未在时限内加载（检查 index 与 page_*.html 是否同目录、file:// 是否被浏览器策略拦截）');
}

/**
 * 用 Node 墙钟 Date.now() 对齐每帧时刻（与 Playwright wait 一致），避免仅用页内 performance.now()
 * 在高频截图负载下与 setTimeout 不同步、故事提前结束却继续录满 maxFrames → 后面全白屏。
 * 达到 contentSec 的约 82% 帧数后，若连续多帧检测到「全部播完」且 iframe 无有效 src，则只再录 tail 帧即停。
 */
async function recordStoryIndex(
  page,
  frameDir,
  { fps, width, height, hideBar, maxFrames, contentSec, tailSeconds },
) {
  const frameIntervalMs = 1000 / fps;
  const tailFrames = Math.max(0, Math.ceil((Number(tailSeconds) || 0) * fps));
  // 低于约 52% contentSec 的帧不采纳「播完」，避免 ~60s 误判；也不宜用 80%+，否则真在 60s 播完时会一直录到该阈值 → 中间全白屏。
  const minEarlyExitFrame = Math.max(
    Math.ceil(fps * 5),
    Math.floor(contentSec * fps * 0.52),
  );

  if (hideBar) {
    await page.addStyleTag({ content: '#bar{display:none!important}' });
  }

  await waitUntilIframeReady(page, 120000);
  await page.waitForTimeout(200);

  const t0 = Date.now();
  let endedStreak = 0;
  let tailLeft = null;
  let totalFrames = 0;

  for (let frameIndex = 0; frameIndex < maxFrames; frameIndex++) {
    const deadline = t0 + frameIndex * frameIntervalMs;
    while (Date.now() < deadline) {
      const left = deadline - Date.now();
      await page.waitForTimeout(left > 50 ? 50 : Math.max(left, 1));
    }

    const framePath = path.join(frameDir, `frame_${pad5(totalFrames)}.png`);
    await page.screenshot({
      path: framePath,
      clip: { x: 0, y: 0, width, height },
    });
    totalFrames++;

    const ended = await page.evaluate(() => {
      const bar = document.getElementById('bar');
      const v = document.getElementById('view');
      const t = bar && bar.textContent ? bar.textContent : '';
      if (!t.includes('全部播完')) return false;
      const s = v.getAttribute('src');
      if (s != null && String(s).trim() !== '') return false;
      return true;
    });

    if (tailLeft === null) {
      if (ended) {
        endedStreak++;
      } else {
        endedStreak = 0;
      }
      if (frameIndex >= minEarlyExitFrame && endedStreak >= 3) {
        if (tailFrames === 0) {
          break;
        }
        tailLeft = tailFrames;
      }
    }

    if (tailLeft !== null) {
      tailLeft--;
      if (tailLeft <= 0) {
        break;
      }
    }
  }

  return totalFrames;
}

async function recordSingleHtmlPage(page, frameDir, { absIn, durationSeconds, fps, width, height }) {
  await page.goto(pathToFileURL(absIn).href, { waitUntil: 'load' });
  await page.waitForTimeout(200);

  const frameIntervalMs = 1000 / fps;
  const frameCount = Math.ceil(durationSeconds * fps);
  const startPerf = await page.evaluate(() => performance.now());

  for (let i = 0; i < frameCount; i++) {
    const deadline = startPerf + i * frameIntervalMs;
    for (;;) {
      const now = await page.evaluate(() => performance.now());
      const left = deadline - now;
      if (left <= 0) break;
      await page.waitForTimeout(left > 50 ? 50 : Math.max(left, 1));
    }

    const framePath = path.join(frameDir, `frame_${pad5(i)}.png`);
    await page.screenshot({
      path: framePath,
      clip: { x: 0, y: 0, width, height },
    });
  }

  return frameCount;
}

function runFfmpeg(frameDir, fps, absOut) {
  const pattern = path.join(frameDir, 'frame_%05d.png');
  const ffArgs = [
    '-y',
    '-r',
    String(fps),
    '-i',
    pattern,
    '-vf',
    'scale=trunc(iw/2)*2:trunc(ih/2)*2',
    '-c:v',
    'libx264',
    '-pix_fmt',
    'yuv420p',
    absOut,
  ];
  console.log(`[export] running ffmpeg -> ${absOut}`);
  childProcess.execFileSync('ffmpeg', ffArgs, { stdio: 'inherit' });
}

async function main() {
  const args = parseArgs(process.argv);

  const input = args.in || args.input;
  const outMp4 = args.out || args.output || 'out.mp4';
  const fps = parseFloat(args.fps || '48');
  const width = parseInt(args.width || '1000', 10);
  const height = parseInt(args.height || '600', 10);
  const durationSecondsArg = args.duration ? parseFloat(args.duration) : null;
  const extraSeconds = parseFloat(args.extra || '0.2');
  const tailSeconds = parseFloat(args.tail != null ? args.tail : '0.5');
  const hideBar = !!(args['hide-bar'] || args.hideBar);

  if (!input && !args.story) {
    printUsage();
    process.exit(1);
  }

  let storyRoot;
  try {
    storyRoot = resolveStoryRoot({ input, story: args.story });
  } catch (e) {
    console.error('[export] failed:', e.message || e);
    process.exit(1);
  }

  const absOut = path.resolve(outMp4);
  const frameDir = path.join(path.dirname(absOut), `.frames_${path.basename(absOut)}_${Date.now()}`);
  fs.mkdirSync(frameDir, { recursive: true });

  let playwright;
  try {
    playwright = require('playwright');
  } catch (e) {
    throw new Error('缺少 playwright：请先执行 npm i playwright');
  }

  const { chromium } = playwright;
  const browser = await chromium.launch({
    args: ['--disable-background-timer-throttling', '--disable-renderer-backgrounding'],
  });
  const page = await browser.newPage({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });
  page.setDefaultTimeout(120000);

  let frameCount;

  if (storyRoot) {
    const indexPath = path.join(storyRoot, 'index.html');
    const metaPath = path.join(storyRoot, 'story_meta.json');
    const indexHtml = fs.readFileSync(indexPath, 'utf8');
    const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
    const fadeMs = parseFadeMsFromIndexHtml(indexHtml);
    const sumDurs = (meta.durations || []).reduce((a, b) => a + Number(b), 0);
    const n = (meta.pages || []).length;
    const fadeSec = fadeMs / 1000;
    const contentSec = sumDurs + n * fadeSec;
    const tail = Number.isFinite(tailSeconds) ? tailSeconds : 0.5;
    const padSec = parseFloat(args['story-pad-sec'] != null ? args['story-pad-sec'] : '1');
    const safePad = Number.isFinite(padSec) && padSec >= 0 ? padSec : 1;
    const plannedSec = contentSec + tail + safePad;
    const maxFrames = Math.ceil(plannedSec * fps);

    console.log(`[export] mode=story index=${indexPath}`);
    console.log(
      `[export] pages=${n}, content≈${contentSec.toFixed(2)}s, +tail=${tail}s +pad=${safePad}s → record≈${plannedSec.toFixed(2)}s, maxFrames=${maxFrames} @ ${fps}fps`,
    );

    await page.goto(pathToFileURL(indexPath).href, { waitUntil: 'load' });

    frameCount = await recordStoryIndex(page, frameDir, {
      fps,
      width,
      height,
      hideBar,
      maxFrames,
      contentSec,
      tailSeconds: tail,
    });

    console.log(`[export] frameCount=${frameCount}, frameDir=${frameDir}`);
  } else {
    if (!input) {
      printUsage();
      process.exit(1);
    }
    const absIn = path.resolve(input);
    const htmlText = fs.readFileSync(absIn, 'utf8');
    const computed = computeDurationSecondsFromHtml(htmlText);
    const durationSeconds =
      (durationSecondsArg && Number.isFinite(durationSecondsArg) ? durationSecondsArg : computed) + extraSeconds;
    if (!durationSeconds || !Number.isFinite(durationSeconds)) {
      throw new Error('无法从 HTML 计算动画时长，请手动加 --duration');
    }
    console.log(`[export] mode=single input=${absIn}`);
    console.log(`[export] computedDuration=${computed}s, usingDuration=${durationSeconds}s, fps=${fps}`);

    frameCount = await recordSingleHtmlPage(page, frameDir, {
      absIn,
      durationSeconds,
      fps,
      width,
      height,
    });
    console.log(`[export] frameCount=${frameCount}, frameDir=${frameDir}`);
  }

  await browser.close();

  runFfmpeg(frameDir, fps, absOut);
  console.log('[export] done.');
}

main().catch((err) => {
  console.error('[export] failed:', err && err.message ? err.message : err);
  process.exit(1);
});