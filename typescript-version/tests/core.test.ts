import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, existsSync, linkSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { after, test } from 'node:test';
import { availablePort, Manager, TunnelCancelled, TunnelError, validateDestination, validatePort, type Settings, type TunnelRecord } from '#core';

const roots: string[] = [];
after(() => { if (roots.length) execFileSync('trash', roots); });

function fixture() {
  const root = mkdtempSync('/tmp/tt-');
  roots.push(root);
  const config = join(root, 'config');
  const state = join(root, 'state');
  writeFileSync(config, 'Host example\n HostName 127.0.0.1\n');
  const manager = new Manager(config, state);
  const settings: Settings = { alias: 'example', remote_port: 8790, local_port: 18790, destination: '127.0.0.1', mode: 'background' };
  const record = manager.prepare(settings);
  const persist = (records: TunnelRecord[]) => writeFileSync(join(state, 'registry.json'), JSON.stringify({ implementation: 'typescript', version: 1, tunnels: records }), { mode: 0o600 });
  return { root, config, state, manager, settings, record, persist };
}

async function listeningServer() {
  const server = createServer();
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert(address && typeof address !== 'string');
  return { server, port: address.port, close: () => new Promise<void>(resolve => server.close(() => resolve())) };
}

test('ports reject booleans, options, shell text and out-of-range values', () => {
  for (const value of [0, 65536, true, '-L', '22;id', ' 22', '22\n', '22\r', '22\u2028', '22.0', '', NaN, Infinity, null, {}]) assert.throws(() => validatePort(value), TunnelError);
  assert.equal(validatePort('08790'), 8790);
});

test('destinations accept hostnames and IPs and reject URLs or shell fragments', () => {
  for (const value of ['https://app', '127.0.0.1:80', 'x;id', '$(id)', '-o', '', 'x\nfoo', 'foo\n', 'foo\r', 'foo\u2028', {}, 123]) assert.throws(() => validateDestination(value), TunnelError);
  for (const value of ['localhost', 'my-host.internal', '127.0.0.1', '::1', '2001:db8::1']) assert.equal(validateDestination(value), value);
});

test('prepare performs no launch or registry write and uses clear-then-forward commands', async () => {
  const { manager, state, record } = fixture();
  assert.deepEqual(await manager.records(), []);
  assert(!existsSync(join(state, 'registry.json')));
  const [master, forward] = record.commands;
  assert(master!.includes('ClearAllForwardings=yes'));
  assert(!master!.includes('-L'));
  assert(master!.includes('PermitLocalCommand=no'));
  assert(master!.includes('ControlPersist=no'));
  assert(forward!.includes('ClearAllForwardings=no'));
  assert(forward!.includes('127.0.0.1:18790:127.0.0.1:8790'));
  assert.deepEqual(forward!.slice(1, 3), ['-F', '/dev/null']);
});

test('IPv6 forwards are bracketed and foreground never forks', () => {
  const { manager, settings } = fixture();
  const record = manager.prepare({ ...settings, destination: '::1', mode: 'foreground' });
  assert(record.commands[1]!.includes('127.0.0.1:18790:[::1]:8790'));
  assert(record.commands[0]!.includes('ForkAfterAuthentication=no'));
});

test('settings reject string ports and unsafe aliases', () => {
  const { manager, settings } = fixture();
  assert.throws(() => manager.prepare({ ...settings, remote_port: '22' } as unknown as Settings), /integers/);
  assert.throws(() => manager.prepare({ ...settings, alias: '-oProxyCommand=x' }), /literal/);
});

test('observed missing socket is Disconnected without rewriting successful history', async () => {
  const { manager, record, state, persist } = fixture();
  record.status = 'Running'; record.successful = true;
  persist([record]);
  assert.equal((await manager.records())[0]!.status, 'Disconnected');
  assert.equal(JSON.parse(readFileSync(join(state, 'registry.json'), 'utf8')).tunnels[0].status, 'Running');
  assert.equal(statSync(state).mode & 0o777, 0o700);
  assert.equal(statSync(join(state, 'registry.json')).mode & 0o777, 0o600);
});

test('corrupt registry and Python registry are preserved unchanged', async () => {
  const { manager, state } = fixture();
  const path = join(state, 'registry.json');
  for (const data of ['broken JSON', JSON.stringify({ version: 1, tunnels: [] })]) {
    writeFileSync(path, data, { mode: 0o600 });
    await assert.rejects(manager.records(), /preserved/);
    assert.equal(readFileSync(path, 'utf8'), data);
  }
});

test('registry rejects path traversal, duplicate IDs, and malformed settings', async () => {
  const { manager, record, persist } = fixture();
  persist([{ ...record, id: '../../other' }]);
  await assert.rejects(manager.records(), /preserved/);
  persist([{ ...record, id: `${record.id}\n` }]);
  await assert.rejects(manager.records(), /preserved/);
  persist([record, record]);
  await assert.rejects(manager.records(), /duplicate/);
  persist([{ ...record, settings: { ...record.settings, destination: {} } as unknown as Settings }]);
  await assert.rejects(manager.records(), /preserved/);
});

test('shared or symlinked directories and symlinked files are rejected', async () => {
  const { manager, root, config, state } = fixture();
  const shared = join(root, 'shared');
  mkdirSync(shared, { mode: 0o755 }); chmodSync(shared, 0o755);
  assert.throws(() => new Manager(config, shared), /private/);
  const linked = join(root, 'linked'); symlinkSync(state, linked);
  assert.throws(() => new Manager(config, linked), /symlink/);
  const victim = join(root, 'victim'); writeFileSync(victim, 'untouched', { mode: 0o600 });
  symlinkSync(victim, join(state, 'registry.json'));
  await assert.rejects(manager.records());
  assert.equal(readFileSync(victim, 'utf8'), 'untouched');
});

test('hard-linked state file is rejected', async () => {
  const { manager, root, state } = fixture();
  const victim = join(root, 'victim');
  writeFileSync(victim, '{}', { mode: 0o600 });
  linkSync(victim, join(state, 'registry.json'));
  await assert.rejects(manager.records(), /Unsafe state file/);
});

test('unsafe pending file preserves registry and its link target on a failed save', async () => {
  const { manager, record, root, state, persist } = fixture();
  persist([record]);
  const before = readFileSync(join(state, 'registry.json'), 'utf8');
  const victim = join(root, 'victim');
  writeFileSync(victim, 'untouched', { mode: 0o600 });
  symlinkSync(victim, join(state, 'registry.pending'));
  await assert.rejects(manager.stop(record.id));
  assert.equal(readFileSync(join(state, 'registry.json'), 'utf8'), before);
  assert.equal(readFileSync(victim, 'utf8'), 'untouched');
  assert(!existsSync(join(state, 'lock')));
});

test('stale exclusive lock fails immediately and is never stolen', async () => {
  const { manager, state, record } = fixture();
  const path = join(state, 'lock');
  writeFileSync(path, 'existing lock', { mode: 0o600 });
  await assert.rejects(manager.stop(record.id), /Another tunnel-ts command/);
  assert.equal(readFileSync(path, 'utf8'), 'existing lock');
});

test('unknown socket never becomes a PID-based kill or successful stop', async () => {
  const { manager, record, state, persist } = fixture();
  persist([record]);
  const socketPath = join(state, `${record.id}.sock`);
  writeFileSync(socketPath, 'not a socket');
  await assert.rejects(manager.stop(record.id), /No process was killed/);
  assert.equal(readFileSync(socketPath, 'utf8'), 'not a socket');
  assert(!existsSync(join(state, 'lock')));
});

test('missing alias never reserves or launches a tunnel', async () => {
  const { manager, settings, state } = fixture();
  await assert.rejects(manager.start(manager.prepare({ ...settings, alias: 'missing' })), /no longer/);
  assert(!existsSync(join(state, 'registry.json')));
  assert(!existsSync(join(state, 'lock')));
});

test('port availability detects a listener and releases its probe', async () => {
  const listener = await listeningServer();
  try { assert.equal(await availablePort(listener.port), false); }
  finally { await listener.close(); }
  assert.equal(await availablePort(listener.port), true);
});

test('edited command preview is rejected before reserving a launch', async () => {
  const { manager, settings, state } = fixture();
  const listener = await listeningServer(); await listener.close();
  const record = manager.prepare({ ...settings, local_port: listener.port });
  record.commands[0]!.push('unexpected');
  await assert.rejects(manager.start(record), /changed after review/);
  assert(!existsSync(join(state, 'registry.json')));
});

test('log creation failure preserves a Failed record without launching', async () => {
  const { manager, settings, state } = fixture();
  const listener = await listeningServer(); await listener.close();
  const record = manager.prepare({ ...settings, local_port: listener.port });
  const log = join(state, `${record.id}.log`);
  writeFileSync(log, 'existing diagnostics', { mode: 0o600 });
  await assert.rejects(manager.start(record), /EEXIST/);
  const stored = (await manager.records())[0]!;
  assert.equal(stored.status, 'Failed'); assert.equal(stored.successful, false);
  assert.equal(readFileSync(log, 'utf8'), 'existing diagnostics');
  assert.equal(lstatSync(join(state, 'registry.json')).mode & 0o777, 0o600);
});

test('stopping a gone owned tunnel saves Stopped and logs trim to their last 64 KiB', async () => {
  const { manager, record, state, persist } = fixture();
  persist([record]);
  const log = join(state, `${record.id}.log`);
  writeFileSync(log, 'a'.repeat(100_000) + 'last line', { mode: 0o600 });
  assert((await manager.logs(record.id)).endsWith('last line'));
  assert.equal(statSync(log).size, 65_536);
  assert.equal((await manager.stop(record.id)).status, 'Stopped');
  assert.equal((await manager.records())[0]!.status, 'Stopped');
});

test('foreground and background command options isolate existing user masters', () => {
  const { manager, settings } = fixture();
  const a = manager.prepare(settings), b = manager.prepare(settings);
  assert.notEqual(a.id, b.id);
  assert.notDeepEqual(a.commands[0], b.commands[0]);
  assert(a.commands[0]!.includes('ForwardAgent=no'));
  assert(a.commands[0]!.includes('ForwardX11=no'));
  assert(a.commands[0]!.includes('Tunnel=no'));
  assert(a.commands[0]!.includes('RemoteCommand=none'));
});

test('startup cancellation keeps its type after rollback and releases the exclusive lock', async context => {
  for (const signal of ['SIGINT', 'SIGHUP', 'SIGTERM'] as const) {
    const { manager, settings, state } = fixture();
    const listener = await listeningServer(); await listener.close();
    const record = manager.prepare({ ...settings, local_port: listener.port });
    const initialListeners = process.listenerCount(signal);
    const internal = manager as unknown as { save: (records: TunnelRecord[]) => void };
    const save = internal.save.bind(manager);
    let saves = 0;
    context.mock.method(internal, 'save', (records: TunnelRecord[]) => {
      save(records);
      if (++saves === 1) process.emit(signal);
    });
    await assert.rejects(manager.start(record), TunnelCancelled);
    const stored = (await manager.records())[0]!;
    assert.equal(stored.status, 'Failed');
    assert.equal(stored.successful, false);
    assert.match(stored.last_error!, /Cancelled/);
    assert(!existsSync(join(state, 'lock')));
    assert(!existsSync(join(state, `${record.id}.sock`)));
    assert(!existsSync(join(state, `${record.id}.log`)));
    assert.equal(process.listenerCount(signal), initialListeners);
  }
});

test('stop defers signals until state is saved and lock is removed', async context => {
  for (const signal of ['SIGINT', 'SIGHUP', 'SIGTERM'] as const) {
    const { manager, record, persist, state } = fixture();
    persist([record]);
    const initialListeners = process.listenerCount(signal);
    const internal = manager as unknown as { exit: (record: TunnelRecord) => Promise<void> };
    context.mock.method(internal, 'exit', async () => {
      assert(existsSync(join(state, 'lock')));
      process.emit(signal);
      await new Promise<void>(resolve => setImmediate(resolve));
    });
    await assert.rejects(manager.stop(record.id), TunnelCancelled);
    assert.equal((await manager.records())[0]!.status, 'Stopped');
    assert(!existsSync(join(state, 'lock')));
    assert.equal(process.listenerCount(signal), initialListeners);
  }
});
