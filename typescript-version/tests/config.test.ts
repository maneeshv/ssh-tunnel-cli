import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { afterEach, beforeEach, test } from 'node:test';
import { discoverHosts, isAlias } from '#config';

let home: string;
let root: string;
let config: string;
let originalHome: string | undefined;

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), 'tunnel-ts-config-'));
  root = join(home, '.ssh');
  mkdirSync(root);
  config = join(root, 'config');
  originalHome = process.env.HOME;
  process.env.HOME = home;
});
afterEach(() => {
  if (originalHome === undefined) delete process.env.HOME;
  else process.env.HOME = originalHome;
  execFileSync('trash', [home]);
});
function write(name: string, contents: string | Buffer): void {
  const path = join(root, name);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, contents);
}

test('literal aliases, comments, quoting, equals, order and deduplication', () => {
  write('config', `# Host ignored
    hOsT = "first" second *.example !excluded first
    Host third # comment
    Host=second fourth
    Host -option bad;command "two words" user@host`);
  const result = discoverHosts(config);
  assert.deepEqual(result.aliases, ['first', 'second', 'third', 'fourth']);
  assert.equal(result.warnings.length, 4);
});

test('literal alias validation rejects options, terminal controls and shell syntax', () => {
  for (const value of ['host', 'mac-2', 'dev.example', '_alias', '123']) assert.ok(isAlias(value));
  for (const value of ['', '-oProxyCommand=exec', 'a\nb', 'host\n', 'host\r', 'host\u2028', 'host\u2029', 'a b', 'a/b', 'a:b', 'a;b', 'a$()', '*', '!host', '[host]', 'höst', null, 4]) {
    assert.equal(isAlias(value), false, String(value));
  }
});

test('nested globbed and quoted relative Includes use sorted traversal', () => {
  write('config', 'Include "parts with spaces/*.conf"\nHost end\n');
  write('parts with spaces/02.conf', 'Host second first\n');
  write('parts with spaces/01.conf', 'Include nested/one\nHost first\nHost *\n');
  write('nested/one', 'Host nested\nHost *\n');
  assert.deepEqual(discoverHosts(config), { aliases: ['nested', 'first', 'second', 'end'], warnings: [] });
});

test('default config and tilde-expanded Includes', () => {
  write('config', 'Include "~/extra config"\n');
  writeFileSync(join(home, 'extra config'), 'Host home-host\n');
  assert.deepEqual(discoverHosts().aliases, ['home-host']);
});

test('custom config still resolves relative Includes from user SSH directory', () => {
  write('included.conf', 'Host from-user-ssh\n');
  mkdirSync(join(home, 'project'));
  writeFileSync(join(home, 'project', 'config'), 'Include included.conf\n');
  writeFileSync(join(home, 'project', 'included.conf'), 'Host wrong-directory\n');
  assert.deepEqual(discoverHosts(join(home, 'project', 'config')), { aliases: ['from-user-ssh'], warnings: [] });
});

test('cycles, missing files and depth limits become warnings', () => {
  write('config', 'Include config missing*.conf\nInclude chain/0\nHost usable\n');
  for (let index = 0; index < 18; index++) write(`chain/${index}`, `Include chain/${index + 1}\n`);
  const result = discoverHosts(config);
  assert.deepEqual(result.aliases, ['usable']);
  for (const reason of ['cycle', 'matched no files', 'depth limit']) assert.ok(result.warnings.some((value) => value.includes(reason)));
});

test('conditional Includes and Match exec are not evaluated', () => {
  write('config', `Host selected
    Include hidden
    Match exec "touch NEVER_EXECUTE"
    Include hidden
    Host * !excluded
    Include hidden
    Match all
    Include visible`);
  write('hidden', 'Host hidden-host\n');
  write('visible', 'Host visible-host\n');
  const result = discoverHosts(config);
  assert.deepEqual(result.aliases, ['selected', 'visible-host']);
  assert.equal(result.warnings.length, 3);
  assert.ok(result.warnings.every((value) => value.includes('Conditional Include')));
});

test('included Host context propagates to parent', () => {
  write('config', 'Include child\nInclude hidden\nHost *\nInclude visible\n');
  write('child', 'Host child-host\n');
  write('hidden', 'Host hidden-host\n');
  write('visible', 'Host visible-host\n');
  const result = discoverHosts(config);
  assert.deepEqual(result.aliases, ['child-host', 'visible-host']);
  assert.equal(result.warnings.length, 1);
  assert.match(result.warnings[0]!, /Conditional Include/);
});

test('included unconditional context also propagates to parent', () => {
  write('config', 'Include child\nInclude visible\n');
  write('child', 'Host child-host\nHost *\n');
  write('visible', 'Host visible-host\n');
  assert.deepEqual(discoverHosts(config), { aliases: ['child-host', 'visible-host'], warnings: [] });
});

test('bad syntax and dynamic Includes are reported', () => {
  write('config', 'Include ${CONFIG} %h.conf\nHost "unterminated\nInclude hidden\nHost valid\n');
  const result = discoverHosts(config);
  assert.deepEqual(result.aliases, ['valid']);
  assert.equal(result.warnings.length, 4);
});

test('missing config and invalid UTF-8 are reported without losing diagnostics', () => {
  assert.match(discoverHosts(config).warnings[0]!, /Cannot read SSH config/);
  write('config', Buffer.from([255]));
  const result = discoverHosts(config);
  assert.deepEqual(result.aliases, []);
  assert.match(result.warnings[0]!, /Cannot read SSH config/);
});

test('shell escapes and inline comments preserve literal aliases', () => {
  write('config', String.raw`Host dev\-one 'dev-two' "dev-three" dev-four#ignored`);
  assert.deepEqual(discoverHosts(config), { aliases: ['dev-one', 'dev-two', 'dev-three', 'dev-four'], warnings: [] });
});

test('brace filenames are literal and double stars do not recurse', () => {
  write('config', 'Include "literal{a,b}"\nHost *\nInclude "parts/**/host"\n');
  write('literal{a,b}', 'Host literal\n');
  write('parts/one/host', 'Host shallow\n');
  write('parts/one/two/host', 'Host deep\n');
  assert.deepEqual(discoverHosts(config), { aliases: ['literal', 'shallow'], warnings: [] });
});

test('wildcard classes and question marks exclude hidden and nonmatching files', () => {
  write('config', 'Include parts/[!x]?.conf\n');
  write('parts/ab.conf', 'Host accepted\nHost *\n');
  write('parts/xb.conf', 'Host excluded\n');
  write('parts/.b.conf', 'Host hidden\n');
  write('parts/abc.conf', 'Host too-long\n');
  assert.deepEqual(discoverHosts(config), { aliases: ['accepted'], warnings: [] });
});

test('named-user home paths warn without silently searching another directory', () => {
  write('config', 'Include ~other/config\nHost usable\n');
  const result = discoverHosts(config);
  assert.deepEqual(result.aliases, ['usable']);
  assert.match(result.warnings[0]!, /named-user home expansion is unsupported/);
});
