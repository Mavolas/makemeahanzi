#!/usr/bin/env node
/**
 * HTML(含CSS动画) -> MP4
 *
 * 使用 Playwright 内置 recordVideo 实时录制页面播放，浏览器原生捕获帧，
 * 录完后用 ffmpeg 将 webm 转为 mp4。比逐帧截图快 3-5 倍，且时间轴与浏览器播放完全一致。
 *
 * 支持两种输入：
 *  1) 单页：--in phrase.html
 *  2) 多页故事：--story <目录或index.html>（含 index.html + story_meta.json）
 *
 * 依赖：npm i playwright；WebM→MP4（H.264）需要带 libx264 的完整 ffmpeg（如 brew install ffmpeg），
 * Playwright 自带的 ffmpeg 仅用于录屏管线，不含 libx264 / mov 高级选项，不能单独用来转 MP4。
 * 可通过 FFMPEG_PATH 指定可执行文件；未设置时会尝试 PATH，以及 Homebrew 常见路径
 * （/opt/homebrew/bin/ffmpeg、/usr/local/bin/ffmpeg）。
 */

const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');
const { pathToFileURL } = require('url');

/** macOS Homebrew 典型安装位置（Apple Silicon / Intel） */
const HOMEBREW_FFMPEG_CANDIDATES = ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'];

function ffmpegSupportsLibx264(ffmpegBin) {
  try {
    const out = childProcess.execFileSync(ffmpegBin, ['-hide_banner', '-encoders'], {
      encoding: 'utf8',
      maxBuffer: 2 * 1024 * 1024,
    });
    return /\blibx264\b/.test(out);
  } catch {
    return false;
  }
}

function resolveFfmpegExecutable() {
  const fromEnv = process.env.FFMPEG_PATH;
  if (fromEnv) {
    if (!fs.existsSync(fromEnv)) {
      throw new Error(`FFMPEG_PATH 不存在：${fromEnv}`);
    }
    if (!ffmpegSupportsLibx264(fromEnv)) {
      throw new Error(`FFMPEG_PATH 指向的 ffmpeg 不含 libx264，无法编码 MP4：${fromEnv}`);
    }
    return fromEnv;
  }

  if (ffmpegSupportsLibx264('ffmpeg')) return 'ffmpeg';

  for (const p of HOMEBREW_FFMPEG_CANDIDATES) {
    if (fs.existsSync(p) && ffmpegSupportsLibx264(p)) return p;
  }

  throw new Error(
    '未找到带 libx264 的 ffmpeg，无法把录制的 WebM 转成 MP4。Playwright 自带的 ffmpeg 仅为精简版（无 H.264）。请安装完整版，例如：brew install ffmpeg',
  );
}

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

function computeDurationSecondsFromHtml(htmlText) {
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
      '  单页：  node export_mp4_from_html.js --in phrase.html --out out.mp4 [--width 1000] [--height 600] [--duration 5]',
      '  故事：  node export_mp4_from_html.js --story story_output --out story.mp4 [--hide-bar] [--tail 1] [--story-pad-sec 1]',
      '         或将 --in 指向含 index.html + story_meta.json 的目录',
    ].join('\n'),
  );
}

async function main() {
  const args = parseArgs(process.argv);

  const input = args.in || args.input;
  const outMp4 = args.out || args.output || 'out.mp4';
  const width = parseInt(args.width || '1000', 10);
  const height = parseInt(args.height || '600', 10);
  const durationSecondsArg = args.duration ? parseFloat(args.duration) : null;
  const extraSeconds = parseFloat(args.extra || '0.5');
  const tailSeconds = parseFloat(args.tail != null ? args.tail : '0');
  const padSec = parseFloat(args['story-pad-sec'] != null ? args['story-pad-sec'] : '0');
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

  let playwright;
  try {
    playwright = require('playwright');
  } catch (e) {
    throw new Error('缺少 playwright：请先执行 npm i playwright');
  }

  const tmpDir = path.join(path.dirname(absOut), `.video_tmp_${Date.now()}`);
  fs.mkdirSync(tmpDir, { recursive: true });

  const { chromium } = playwright;
  const browser = await chromium.launch({
    args: [
      '--disable-background-timer-throttling',
      '--disable-renderer-backgrounding',
      '--allow-file-access-from-files',
    ],
  });

  const page = await browser.newPage({
    viewport: { width, height },
    deviceScaleFactor: 1,
    recordVideo: {
      dir: tmpDir,
      size: { width, height },
    },
  });
  page.setDefaultTimeout(300000);

  if (storyRoot) {
    const indexPath = path.join(storyRoot, 'index.html');
    const metaPath = path.join(storyRoot, 'story_meta.json');
    const indexHtml = fs.readFileSync(indexPath, 'utf8');
    const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
    const fadeMs = parseFadeMsFromIndexHtml(indexHtml);
    const sumDurs = (meta.durations || []).reduce((a, b) => a + Number(b), 0);
    const n = (meta.pages || []).length;
    const fadeSec = fadeMs / 1000;
    // 与 index.html 一致：最后一页后不淡出，仅 n-1 次页间淡出
    const contentSec = sumDurs + Math.max(0, n - 1) * fadeSec;
    const safePad = Number.isFinite(padSec) && padSec >= 0 ? padSec : 1;

    console.log(`[export] mode=story  pages=${n}  content≈${contentSec.toFixed(1)}s`);
    console.log(`[export] 实时录制中，预计等待 ≈${(contentSec + tailSeconds + safePad).toFixed(0)}s …`);

    await page.goto(pathToFileURL(indexPath).href, { waitUntil: 'load' });

    if (hideBar) {
      await page.addStyleTag({ content: '#bar{display:none!important}' });
    }

    const timeoutMs = (contentSec + 120) * 1000;
    try {
      await page.waitForFunction(
        () => {
          const bar = document.getElementById('bar');
          return bar && bar.textContent && bar.textContent.includes('全部播完');
        },
        { timeout: timeoutMs, polling: 500 },
      );
      console.log('[export] 故事播完，录制 tail …');
    } catch (e) {
      console.warn('[export] 警告：未检测到「全部播完」，按超时结束录制');
    }

    const tailMs = ((Number.isFinite(tailSeconds) ? tailSeconds : 1) + safePad) * 1000;
    if (tailMs > 0) {
      await page.waitForTimeout(tailMs);
    }
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
    console.log(`[export] mode=single  duration=${durationSeconds.toFixed(1)}s`);
    console.log(`[export] 实时录制中，等待 ${durationSeconds.toFixed(0)}s …`);

    await page.goto(pathToFileURL(absIn).href, { waitUntil: 'load' });
    await page.waitForTimeout(durationSeconds * 1000);
  }

  // 关闭 page 才能获取最终视频文件路径
  await page.close();
  const videoPath = await page.video().path();
  console.log(`[export] 录制完成：${videoPath}`);

  await browser.close();

  // webm → mp4
  const ffmpegBin = resolveFfmpegExecutable();
  console.log(`[export] ffmpeg 转码（${ffmpegBin}）→ ${absOut}`);
  const ffArgs = [
    '-y',
    '-i',
    videoPath,
    '-c:v',
    'libx264',
    '-pix_fmt',
    'yuv420p',
    '-movflags',
    '+faststart',
    absOut,
  ];
  childProcess.execFileSync(ffmpegBin, ffArgs, { stdio: 'inherit' });

  // 清理临时目录
  try {
    fs.rmSync(tmpDir, { recursive: true });
  } catch (_) {}

  console.log('[export] done.');
}

main().catch((err) => {
  console.error('[export] failed:', err && err.message ? err.message : err);
  process.exit(1);
});
