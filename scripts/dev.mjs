// 同时启动 FastAPI (:8000) 与 Vite (:3000)。
// 用法：npm run dev   或者   node scripts/dev.mjs
//
// 在 Windows 上避免使用 shell:true 嵌套引号（dev.mjs 早期版本在 Node 18+ 上会
// 触发 ENOENT）；改用直接 execve 路径。

import { spawn } from 'node:child_process';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { existsSync } from 'node:fs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const venvPy = process.platform === 'win32'
  ? path.join(root, 'venv', 'Scripts', 'python.exe')
  : path.join(root, 'venv', 'bin', 'python');

if (!existsSync(venvPy)) {
  console.error(`[dev] venv python not found: ${venvPy}`);
  console.error('[dev] run `npm run install` (or `./install.ps1` / `./install.sh`) first.');
  process.exit(1);
}

function start(name, command, args, cwd, color, useShell) {
  const child = spawn(command, args, {
    cwd,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: process.env,
    windowsHide: true,
    shell: !!useShell,
  });
  const tag = `\x1b[${color}m[${name}]\x1b[0m`;
  for (const stream of ['stdout', 'stderr']) {
    child[stream].on('data', (b) => {
      const text = b.toString();
      for (const line of text.split(/\r?\n/)) {
        if (line.trim()) console.log(`${tag} ${line}`);
      }
    });
  }
  child.on('exit', (code) => {
    console.log(`${tag} exited with code ${code}`);
    process.exit(code ?? 0);
  });
  return child;
}

// 在 npm workspaces 模式下，依赖会被 hoist 到仓库根。用 npm run dev 触发
// 各自 workspace 内的脚本，让 npm 处理 PATH。
//
// Windows 上 npm.cmd 是 batch script，Node 的 spawn 不能直接 exec；
// 统一用 shell: true 启动 npm，但保持 venvPy 直接 exec（更稳）。
const api = start(
  'api',
  venvPy,
  ['-m', 'uvicorn', 'src.api.main:app', '--reload', '--host', '0.0.0.0', '--port', '8000'],
  path.join(root, 'apps', 'api'),
  '36',
  false,
);

const web = start(
  'web',
  process.platform === 'win32' ? 'npm.cmd' : 'npm',
  ['run', 'dev'],
  path.join(root, 'apps', 'web'),
  '35',
  true,
);

const shutdown = () => {
  api.kill();
  web.kill();
  process.exit(0);
};
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);