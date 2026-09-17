// 清理本地构建产物、缓存、虚拟环境。
// 用法：npm run clean

import { rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url)) + '/..';

const targets = [
  'node_modules',
  'apps/api/__pycache__',
  'apps/api/src/__pycache__',
  'apps/api/src/api/__pycache__',
  'apps/api/src/core/__pycache__',
  'apps/api/src/db/__pycache__',
  'apps/api/src/db/models',
  'apps/api/src/services/__pycache__',
  'apps/api/src/utils/__pycache__',
  'apps/api/src/plugins/__pycache__',
  'apps/api/tests/__pycache__',
  'apps/cli/src/__pycache__',
  'apps/cli/src/ontolohub_cli/__pycache__',
  'packages/core/src/__pycache__',
  'apps/web/.vite',
  'apps/web/dist',
  'apps/web/.tsbuildinfo',
  'apps/web/node_modules',
  'data/logs/*.log',
  '.mypy_cache',
  '.pytest_cache',
  '.ruff_cache',
];

for (const t of targets) {
  const p = join(root, t);
  if (existsSync(p)) {
    await rm(p, { recursive: true, force: true });
    console.log(`  ✓ removed ${t}`);
  }
}

console.log('clean done');