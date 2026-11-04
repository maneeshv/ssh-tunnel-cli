import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { quote } from '#cli';
const cli = fileURLToPath(new URL('../src/cli.js', import.meta.url));
const run = (...args: string[]) => spawnSync(process.execPath, [cli, ...args], { encoding: 'utf8' });
test('help/version are usable without a terminal', () => {
  assert.equal(run('--version').stdout.trim(), 'tunnel-ts 0.1.0');
  assert.match(run('--help').stdout, /Usage: tunnel-ts/);
});
test('invalid and interactive invocations do not initialize state', () => {
  const root = mkdtempSync('/tmp/ts-cli-');
  try {
    const state = join(root, 'state');
    for (const args of [[], ['stop'], ['restart']]) {
      const result = run('--state-dir', state, ...args);
      assert.equal(result.status, 2);
      assert.match(result.stdout, /need a terminal/);
      assert.equal(existsSync(state), false);
    }
    assert.equal(run('garbage').status, 2);
    assert.equal(run('list', 'extra').status, 2);
  } finally { spawnSync('trash', [root]); }
});
test('command preview quotes shell metacharacters and empty arguments', () => {
  assert.equal(quote('127.0.0.1:8790'), '127.0.0.1:8790');
  assert.equal(quote(''), "''");
  assert.equal(quote("it's a path"), "'it'\"'\"'s a path'");
  assert.equal(quote('$(touch unsafe)'), "'$(touch unsafe)'");
  assert.equal(quote('path\n'), "'path\n'");
});
