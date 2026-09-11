#!/usr/bin/env node
const { spawnSync } = require('node:child_process');
const { resolve } = require('node:path');

const result = spawnSync('uv', [
  'run', '--project', resolve(__dirname, '..'), '--locked', '--no-dev',
  '--no-editable', '--no-config', '--no-env-file',
  '--', 'python', '-I', resolve(__dirname, '..', 'slopcheck.py'), ...process.argv.slice(2),
], { stdio: 'inherit' });

if (result.error) {
  console.error(result.error.code === 'ENOENT'
    ? 'slopcheck requires uv: https://docs.astral.sh/uv/getting-started/installation/'
    : `slopcheck: ${result.error.message}`);
  process.exit(2);
}
if (result.signal) process.kill(process.pid, result.signal);
process.exit(result.status ?? 2);
