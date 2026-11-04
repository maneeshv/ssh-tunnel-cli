import { spawn, type ChildProcess } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import {
  closeSync, constants, existsSync, fstatSync, fsyncSync, ftruncateSync,
  lstatSync, mkdirSync, openSync, readFileSync, readSync, renameSync,
  unlinkSync, writeFileSync, writeSync,
} from 'node:fs';
import { createConnection, createServer, isIP } from 'node:net';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { discoverHosts, isAlias } from '#config';

export class TunnelError extends Error {}
export class TunnelCancelled extends TunnelError {}

export interface Settings {
  alias: string;
  remote_port: number;
  local_port: number;
  destination: string;
  mode: 'background' | 'foreground';
}

export type Status = 'Connecting' | 'Running' | 'Stopped' | 'Disconnected' | 'Failed' | 'Unknown';
export interface TunnelRecord {
  id: string;
  settings: Settings;
  created_at: string;
  started_at?: string;
  stopped_at?: string;
  status: Status;
  successful: boolean;
  last_error: string | null;
  commands: string[][];
}

export function validatePort(value: unknown): number {
  const token = String(value);
  if ((typeof value !== 'string' && typeof value !== 'number') || !token.length || token.length > 5 || /[^0-9]/.test(token)) {
    throw new TunnelError('Enter a decimal port from 1 to 65535.');
  }
  const port = Number(value);
  if (port < 1 || port > 65535) throw new TunnelError('Enter a decimal port from 1 to 65535.');
  return port;
}

export function validateDestination(value: unknown): string {
  if (typeof value !== 'string') throw new TunnelError('Destination must be a hostname or IP address string.');
  if (isIP(value)) return value;
  if (value.length > 253 || /\s/.test(value) || !value.replace(/\.$/, '').split('.').every(part => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(part))) {
    throw new TunnelError('Enter a hostname or IP address, without a port or URL.');
  }
  return value;
}

export function validateSettings(settings: Settings): Settings {
  if (!settings || typeof settings.alias !== 'string' || !isAlias(settings.alias)) throw new TunnelError('Choose a literal SSH-config alias.');
  validatePort(settings.remote_port);
  validatePort(settings.local_port);
  if (!Number.isInteger(settings.remote_port) || !Number.isInteger(settings.local_port)) throw new TunnelError('Stored ports must be integers.');
  validateDestination(settings.destination);
  if (!['background', 'foreground'].includes(settings.mode)) throw new TunnelError('Run mode must be background or foreground.');
  return settings;
}

export async function availablePort(port: number): Promise<boolean> {
  validatePort(port);
  return new Promise(resolvePort => {
    const server = createServer();
    server.once('error', () => resolvePort(false));
    server.listen(port, '127.0.0.1', () => server.close(() => resolvePort(true)));
  });
}

const now = () => new Date().toISOString();
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const errorCode = (error: unknown) => (error as NodeJS.ErrnoException)?.code;
const expand = (path: string) => resolve(path === '~' ? homedir() : path.startsWith('~/') ? join(homedir(), path.slice(2)) : path);
const emptyLog = 'No SSH diagnostics recorded. Application logs are not collected.';

function run(command: string[], timeout = 5_000): Promise<{ code: number; stderr: string }> {
  return new Promise((resolveRun, reject) => {
    // Mux commands need no terminal; isolate them so Ctrl+C cannot interrupt owned-session cleanup.
    const child = spawn(command[0]!, command.slice(1), { detached: true, stdio: ['ignore', 'ignore', 'pipe'] });
    const chunks: Buffer[] = [];
    let size = 0;
    let failure: Error | undefined;
    const timer = setTimeout(() => {
      failure = new TunnelError('SSH control command timed out. Inspect the tunnel status.');
      child.kill('SIGKILL');
    }, timeout);
    timer.unref();
    child.stderr.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > 65_536) {
        failure = new TunnelError('SSH control diagnostics exceeded 64 KiB. Inspect the tunnel status.');
        child.kill('SIGKILL');
      } else chunks.push(chunk);
    });
    child.once('error', error => { clearTimeout(timer); reject(new TunnelError(message(error))); });
    child.once('close', (code, signal) => {
      clearTimeout(timer);
      if (failure) reject(failure);
      else if (code === null) reject(new TunnelError(`SSH control command was interrupted (${signal}).`));
      else resolveRun({ code, stderr: Buffer.concat(chunks).toString('utf8') });
    });
  });
}

interface ChildOutcome { code: number | null; signal?: string | null; error?: Error }
interface OwnedChild { child: ChildProcess; done: Promise<ChildOutcome>; outcome?: ChildOutcome }
function launch(command: string[]): OwnedChild {
  const child = spawn(command[0]!, command.slice(1), { stdio: 'inherit' });
  const owned: OwnedChild = { child, done: Promise.resolve({ code: null }) };
  owned.done = new Promise(resolveDone => {
    child.once('error', error => { owned.outcome = { code: null, error }; resolveDone(owned.outcome); });
    child.once('exit', (code, signal) => { owned.outcome = { code, signal }; resolveDone(owned.outcome); });
  });
  return owned;
}

export class Manager {
  readonly configPath: string | undefined;
  readonly stateDir: string;
  readonly ssh = '/usr/bin/ssh';

  constructor(configPath?: string, stateDir?: string) {
    this.configPath = configPath ? expand(configPath) : undefined;
    this.stateDir = expand(stateDir || join(homedir(), '.tunnel-ts'));
    if (!process.getuid || !existsSync(this.ssh)) throw new TunnelError('This CLI requires macOS or Linux with /usr/bin/ssh.');
    mkdirSync(this.stateDir, { recursive: true, mode: 0o700 });
    const info = lstatSync(this.stateDir);
    if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077)) {
      throw new TunnelError(`State directory must be owned by you, private (0700), and not a symlink: ${this.stateDir}`);
    }
    if (Buffer.byteLength(join(this.stateDir, '000000000000.sock')) >= 100) throw new TunnelError('State path is too long for SSH control sockets. Choose a shorter --state-dir.');
  }

  private path(id: string, suffix: string): string {
    if (typeof id !== 'string' || id.length !== 12 || !/^[0-9a-f]{12}$/.test(id)) throw new TunnelError('Invalid tunnel ID.');
    return join(this.stateDir, `${id}.${suffix}`);
  }

  private open(path: string, flags: number): number {
    const fd = openSync(path, flags | constants.O_NOFOLLOW | constants.O_NONBLOCK, 0o600);
    const info = fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid!() || (info.mode & 0o077) || info.nlink !== 1) {
      closeSync(fd);
      throw new TunnelError(`Unsafe state file (expected a private regular file): ${path}`);
    }
    return fd;
  }

  private async locked<T>(operation: () => Promise<T>): Promise<T> {
    const path = join(this.stateDir, 'lock');
    let fd: number;
    try { fd = this.open(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL); }
    catch (error) {
      if (errorCode(error) !== 'EEXIST') throw error;
      // shortcut: exclusive files survive crashes; inspect and remove stale locks manually, never trust a reused PID.
      throw new TunnelError(`Another tunnel-ts command is changing state. If a command crashed, verify no tunnel-ts command is running before using trash on ${path}.`);
    }
    const identity = fstatSync(fd);
    try {
      writeFileSync(fd, JSON.stringify({ pid: process.pid, created_at: now() }));
      return await operation();
    } finally {
      closeSync(fd);
      const current = lstatSync(path);
      if (current.ino === identity.ino && current.dev === identity.dev) unlinkSync(path);
    }
  }

  private read(): TunnelRecord[] {
    let fd: number;
    try { fd = this.open(join(this.stateDir, 'registry.json'), constants.O_RDONLY); }
    catch (error) { if (errorCode(error) === 'ENOENT') return []; throw error; }
    try {
      const data = JSON.parse(readFileSync(fd, 'utf8'));
      if (data.implementation !== 'typescript' || data.version !== 1 || !Array.isArray(data.tunnels)) throw new Error('unsupported registry; Python state cannot be managed here');
      const ids = new Set<string>();
      for (const record of data.tunnels) {
        this.path(record.id, 'sock');
        if (ids.has(record.id)) throw new Error('duplicate tunnel ID');
        ids.add(record.id);
        validateSettings(record.settings);
        if (typeof record.created_at !== 'string' || (record.last_error !== null && typeof record.last_error !== 'string') ||
            !['Connecting', 'Running', 'Stopped', 'Disconnected', 'Failed', 'Unknown'].includes(record.status) ||
            typeof record.successful !== 'boolean' || !Array.isArray(record.commands) || !record.commands.length ||
            record.commands.some((command: unknown) => !Array.isArray(command) || !command.length || command.some(arg => typeof arg !== 'string'))) throw new Error('invalid record');
      }
      return data.tunnels;
    } catch (error) { throw new TunnelError(`Cannot read registry; preserved it unchanged: ${message(error)}`); }
    finally { closeSync(fd); }
  }

  private save(records: TunnelRecord[]): void {
    const pending = join(this.stateDir, 'registry.pending');
    const fd = this.open(pending, constants.O_WRONLY | constants.O_CREAT);
    try {
      ftruncateSync(fd, 0);
      writeFileSync(fd, JSON.stringify({ implementation: 'typescript', version: 1, tunnels: records }, null, 2));
      fsyncSync(fd);
    } finally { closeSync(fd); }
    renameSync(pending, join(this.stateDir, 'registry.json'));
    const directory = openSync(this.stateDir, constants.O_RDONLY);
    try { fsyncSync(directory); } finally { closeSync(directory); }
  }

  private controlArgs(record: TunnelRecord, operation: string): string[] {
    // Management must never re-evaluate user configuration or open a new connection.
    return [this.ssh, '-F', '/dev/null', '-S', this.path(record.id, 'sock'), '-O', operation, record.settings.alias];
  }

  private async probe(record: TunnelRecord): Promise<'Alive' | 'Gone' | 'Unknown'> {
    const path = this.path(record.id, 'sock');
    try {
      const info = lstatSync(path);
      if (!info.isSocket() || info.uid !== process.getuid!()) return 'Unknown';
      if ((await run(this.controlArgs(record, 'check'))).code === 0) return 'Alive';
      return await new Promise(resolveProbe => {
        const client = createConnection(path);
        client.setTimeout(1_000);
        client.once('connect', () => { client.destroy(); resolveProbe('Unknown'); });
        client.once('timeout', () => { client.destroy(); resolveProbe('Unknown'); });
        client.once('error', error => { client.destroy(); resolveProbe(['ENOENT', 'ECONNREFUSED'].includes(errorCode(error) || '') ? 'Gone' : 'Unknown'); });
      });
    } catch (error) { return errorCode(error) === 'ENOENT' ? 'Gone' : 'Unknown'; }
  }

  async records(): Promise<TunnelRecord[]> {
    return Promise.all(this.read().map(async record => {
      const probe = await this.probe(record);
      const status: Status = probe === 'Unknown' ? 'Unknown' : probe === 'Alive' ? record.status === 'Running' ? 'Running' : 'Unknown' : ['Stopped', 'Failed'].includes(record.status) ? record.status : 'Disconnected';
      return { ...record, status };
    }));
  }

  commands(record: TunnelRecord): string[][] {
    const settings = validateSettings(record.settings);
    const master = [this.ssh];
    if (this.configPath) master.push('-F', this.configPath);
    master.push('-n', '-N', '-T', '-M', '-S', this.path(record.id, 'sock'), '-E', this.path(record.id, 'log'));
    for (const option of ['ClearAllForwardings=yes', 'ControlPersist=no', 'ExitOnForwardFailure=yes', 'PermitLocalCommand=no',
      'RemoteCommand=none', 'ForwardAgent=no', 'ForwardX11=no', 'Tunnel=no', 'ServerAliveInterval=15', 'ServerAliveCountMax=3',
      'LogLevel=ERROR', 'ConnectTimeout=15', 'ConnectionAttempts=1', `ForkAfterAuthentication=${settings.mode === 'background' ? 'yes' : 'no'}`]) master.push('-o', option);
    master.push(settings.alias);
    const destination = settings.destination.includes(':') ? `[${settings.destination}]` : settings.destination;
    const forward = this.controlArgs(record, 'forward');
    forward.splice(-1, 0, '-o', 'ClearAllForwardings=no', '-L', `127.0.0.1:${settings.local_port}:${destination}:${settings.remote_port}`);
    return [master, forward];
  }

  prepare(settings: Settings): TunnelRecord {
    const record: TunnelRecord = { id: randomBytes(6).toString('hex'), settings: { ...validateSettings(settings) }, created_at: now(), status: 'Connecting', successful: false, last_error: null, commands: [] };
    record.commands = this.commands(record);
    return record;
  }

  private find(records: TunnelRecord[], id: string): TunnelRecord {
    this.path(id, 'sock');
    const record = records.find(item => item.id === id);
    if (!record) throw new TunnelError('Tunnel not found. Use tunnel-ts list to see managed IDs.');
    return record;
  }

  async logs(id: string): Promise<string> {
    this.find(this.read(), id);
    let fd: number;
    try { fd = this.open(this.path(id, 'log'), constants.O_RDWR); }
    catch (error) { if (errorCode(error) === 'ENOENT') return emptyLog; throw error; }
    try {
      const size = fstatSync(fd).size;
      const output = Buffer.alloc(Math.min(size, 65_536));
      const count = readSync(fd, output, 0, output.length, Math.max(0, size - output.length));
      // shortcut: SSH owns the log; trim on access, add rotation if continuous growth matters.
      if (size > 65_536) { writeSync(fd, output, 0, count, 0); ftruncateSync(fd, count); }
      return output.subarray(0, count).toString('utf8') || emptyLog;
    } finally { closeSync(fd); }
  }

  private async exit(record: TunnelRecord): Promise<void> {
    const probe = await this.probe(record);
    if (probe === 'Unknown') throw new TunnelError('Cannot verify the owned control socket. No process was killed.');
    if (probe === 'Gone') return;
    const result = await run(this.controlArgs(record, 'exit'));
    for (let retry = 0; retry < 30; retry++) {
      if (await this.probe(record) === 'Gone') return;
      await delay(100);
    }
    throw new TunnelError(result.stderr.trim() || 'SSH shutdown could not be verified.');
  }

  async stop(id: string): Promise<TunnelRecord> {
    let interrupted = false;
    const interrupt = () => { interrupted = true; };
    const signals: NodeJS.Signals[] = ['SIGINT', 'SIGHUP', 'SIGTERM'];
    signals.forEach(signal => process.on(signal, interrupt));
    try {
      const result = await this.locked(async () => {
        const records = this.read();
        const record = this.find(records, id);
        await this.exit(record);
        Object.assign(record, { status: 'Stopped', stopped_at: now(), last_error: null });
        this.save(records);
        return structuredClone(record);
      });
      if (interrupted) throw new TunnelCancelled('Cancelled after stopping the tunnel safely.');
      return result;
    } finally { signals.forEach(signal => process.off(signal, interrupt)); }
  }

  private async cleanup(record: TunnelRecord, owned?: OwnedChild): Promise<void> {
    if (owned && !owned.outcome) {
      owned.child.kill('SIGTERM');
      await Promise.race([owned.done, delay(5_000, undefined, { ref: false })]);
      if (!owned.outcome) { owned.child.kill('SIGKILL'); await owned.done; }
    }
    await this.exit(record);
  }

  async start(prepared: TunnelRecord, onStarted?: (record: TunnelRecord) => void | Promise<void>): Promise<TunnelRecord> {
    const record = structuredClone(prepared);
    const settings = validateSettings(record.settings);
    let owned: OwnedChild | undefined;
    let interrupted = false;
    let interrupt!: () => void;
    const cancellation = new Promise<void>(resolveCancel => { interrupt = () => { interrupted = true; resolveCancel(); }; });
    const signals: NodeJS.Signals[] = ['SIGINT', 'SIGHUP', 'SIGTERM'];
    signals.forEach(signal => process.on(signal, interrupt));
    const checkCancelled = () => { if (interrupted) throw new TunnelCancelled('Cancelled during startup.'); };
    try {
      await this.locked(async () => {
        const records = this.read();
        if (!discoverHosts(this.configPath).aliases.includes(settings.alias)) throw new TunnelError('The selected alias is no longer in the discovered SSH configuration.');
        if (records.some(item => item.id === record.id)) throw new TunnelError('This launch was already attempted. Review a fresh attempt before retrying.');
        for (const existing of records) {
          if (existing.settings.alias === settings.alias && existing.settings.destination === settings.destination && existing.settings.local_port === settings.local_port && existing.settings.remote_port === settings.remote_port && await this.probe(existing) === 'Alive') throw new TunnelError(`This tunnel is already active: ${existing.id}. Open Manage tunnels.`);
        }
        if (!await availablePort(settings.local_port)) throw new TunnelError(`Port ${settings.local_port} is unavailable. Choose another local port.`);
        try { lstatSync(this.path(record.id, 'sock')); throw new TunnelError('Control path already exists. Review a fresh attempt.'); }
        catch (error) { if (errorCode(error) !== 'ENOENT') throw error; }
        if (JSON.stringify(record.commands) !== JSON.stringify(this.commands(record))) throw new TunnelError('Launch settings changed after review. Review again before starting.');
        checkCancelled();
        Object.assign(record, { status: 'Connecting', started_at: now() });
        records.push(record);
        this.save(records);
        try {
          checkCancelled();
          closeSync(this.open(this.path(record.id, 'log'), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL));
          owned = launch(record.commands[0]!);
          while (await this.probe(record) !== 'Alive') {
            checkCancelled();
            if (owned.outcome) {
              if (settings.mode === 'background' && owned.outcome.code === 0) {
                let alive = false;
                for (let retry = 0; retry < 30; retry++) { checkCancelled(); if (await this.probe(record) === 'Alive') { alive = true; break; } await delay(100); }
                if (!alive) throw new TunnelError('SSH exited without a responsive owned control socket.');
                break;
              }
              throw new TunnelError(`SSH startup failed (${owned.outcome.error?.message || `exit ${owned.outcome.code ?? owned.outcome.signal}`}). ${await this.logs(record.id)}`);
            }
            await delay(50);
          }
          checkCancelled();
          if (settings.mode === 'background') {
            const outcome = await Promise.race([owned.done, delay(5_000, null, { ref: false })]);
            if (!outcome || outcome.code !== 0) throw new TunnelError('SSH backgrounding failed or timed out.');
          }
          checkCancelled();
          const result = await run(record.commands[1]!, 10_000);
          if (result.code) throw new TunnelError(result.stderr.trim() || 'SSH refused the requested forward.');
          if (await this.probe(record) !== 'Alive') throw new TunnelError('SSH disconnected while opening the forward.');
          checkCancelled();
          Object.assign(record, { status: 'Running', successful: true, last_error: null });
          this.save(records);
        } catch (error) {
          try { await this.cleanup(record, owned); record.status = 'Failed'; }
          catch (cleanupError) { record.status = 'Unknown'; record.last_error = `Cleanup needs attention: ${message(cleanupError)}`; }
          record.last_error ||= message(error);
          record.successful = false;
          try { this.save(records); }
          catch (saveError) {
            const Failure = interrupted ? TunnelCancelled : TunnelError;
            throw new Failure(`Launch failed; could not save its outcome. Record ${record.id} was reserved before launch. ${message(saveError)}`);
          }
          if (interrupted || error instanceof TunnelCancelled) throw new TunnelCancelled(record.last_error);
          throw new TunnelError(record.last_error);
        }
      });
      if (settings.mode === 'foreground') {
        let result = record;
        try {
          if (onStarted) await onStarted(structuredClone(record));
          await Promise.race([owned!.done, cancellation]);
        } finally {
          const stopped = interrupted || !owned!.outcome;
          // Release our connection before touching state, even when the callback or registry fails.
          await this.cleanup(record, owned);
          result = await this.locked(async () => {
            const records = this.read();
            const stored = this.find(records, record.id);
            if (stopped) Object.assign(stored, { status: 'Stopped', stopped_at: now(), last_error: null });
            else if (stored.status !== 'Stopped') Object.assign(stored, { status: 'Disconnected', last_error: `SSH exited (${owned!.outcome?.code ?? owned!.outcome?.signal}).` });
            this.save(records);
            return stored;
          });
        }
        return structuredClone(result);
      }
      if (onStarted) await onStarted(structuredClone(record));
      return structuredClone(record);
    } finally { signals.forEach(signal => process.off(signal, interrupt)); }
  }
}
