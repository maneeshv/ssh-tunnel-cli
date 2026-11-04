#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { realpathSync } from 'node:fs';
import { discoverHosts } from '#config';
import { Manager, TunnelError, TunnelCancelled, availablePort, validatePort, validateDestination, type Settings, type TunnelRecord } from '#core';
import { Back, Cancelled, choose, text, say } from '#prompts';

export const VERSION = '0.1.0';
export function label(record: TunnelRecord): string {
  const s = record.settings;
  return `${s.alias}  127.0.0.1:${s.local_port} → ${s.destination}:${s.remote_port}  ${record.status}`;
}
export function quote(argument: string): string {
  return /^[a-zA-Z0-9_@%+=:,./-]+(?![\s\S])/.test(argument) ? argument : `'${argument.replaceAll("'", "'\"'\"'")}'`;
}
function showCommands(record: TunnelRecord): void {
  say('\nExact commands — private SSH connection, then the selected forward:');
  record.commands.forEach((args, index) => say(`  ${index + 1}. ${args.map(quote).join(' ')}`));
}
function details(record: TunnelRecord): void {
  say(`\n${label(record)}\nID: ${record.id}\nMode: ${record.settings.mode}\nCreated: ${record.created_at}`);
  if (record.last_error) say(`Last diagnostic: ${record.last_error}`);
  say('Running describes the SSH tunnel; application health is not checked.');
}
async function host(manager: Manager, selected?: string): Promise<string> {
  while (true) {
    const found = discoverHosts(manager.configPath);
    found.warnings.forEach(warning => say(`Warning: ${warning}`));
    if (found.aliases.length) return choose<string>('Choose a machine', found.aliases,
      { default: found.aliases.includes(selected ?? '') ? selected : undefined, search: true });
    say('No literal SSH aliases found. Add a Host alias to your SSH config manually, then reload.');
    if (await choose<string>('Next', ['Reload hosts', 'Back']) === 'Back') throw new Back();
  }
}
function sameRoute(a: Settings, b: Settings): boolean {
  return a.alias === b.alias && a.local_port === b.local_port && a.remote_port === b.remote_port && a.destination === b.destination;
}
function started(record: TunnelRecord): void {
  say(`\nTunnel running in ${record.settings.mode}: 127.0.0.1:${record.settings.local_port}\nID: ${record.id}`);
  say('Tunnel active; application health not checked.');
  if (record.settings.mode === 'foreground') say('Ctrl+C stops this tunnel.');
}

export async function runWizard(manager: Manager, initial?: Settings, restart?: TunnelRecord): Promise<boolean> {
  const answers: Partial<Settings> = { ...initial };
  let step = initial ? 5 : 0;
  let editing = false;
  const fields = ['Machine', 'Remote port', 'Local port', 'Destination', 'Run mode'];
  while (true) {
    try {
      if (step === 0) answers.alias = await host(manager, answers.alias);
      else if (step === 1) {
        say('Remote application port — the SSH server port comes from SSH config.');
        answers.remote_port = validatePort(await text('Remote port', String(answers.remote_port ?? ''), value => { validatePort(value); return true; }));
      } else if (step === 2) {
        answers.local_port = validatePort(await text('Local port', String(answers.local_port ?? answers.remote_port ?? ''), async value => {
          const port = validatePort(value);
          const ownsPort = restart?.status === 'Running' && restart.settings.local_port === port;
          if (!ownsPort && !await availablePort(port)) throw new Error(`Port ${port} is unavailable. Enter another local port.`);
          return true;
        }));
      } else if (step === 3) {
        say('127.0.0.1 means the selected remote machine. Advanced: another host/IP reachable from it.');
        answers.destination = validateDestination(await text('Destination', answers.destination ?? '127.0.0.1', value => { validateDestination(value); return true; }));
      } else if (step === 4) {
        say('Background returns to your shell. Foreground stays attached until Ctrl+C.');
        answers.mode = await choose<Settings['mode']>('How should it run?', [
          { name: 'Background', value: 'background' }, { name: 'Foreground', value: 'foreground' },
        ], { default: answers.mode ?? 'background' });
      } else {
        const settings = answers as Settings;
        const record = manager.prepare(settings);
        say(`\nReady to start\n  Machine       ${settings.alias}\n  Local         127.0.0.1:${settings.local_port}` +
          `\n  Destination   ${settings.destination}:${settings.remote_port} on ${settings.alias}\n  Mode          ${settings.mode}`);
        if (restart) say(`Restart will stop tunnel ${restart.id} before starting this route. Existing connections will be interrupted.`);
        showCommands(record);
        const action = await choose<string>('Review', ['Start tunnel', 'Edit settings', 'Cancel']);
        if (action === 'Cancel') return false;
        if (action === 'Edit settings') {
          editing = true;
          step = await choose<number>('Edit which setting?', fields.map((name, value) => ({ name, value })));
          continue;
        }
        const current = await manager.records();
        const duplicate = current.find(item => item.status === 'Running' && sameRoute(item.settings, settings) && item.id !== restart?.id);
        if (duplicate) {
          say('That route is already running. Opening its management actions.');
          return manage(manager, duplicate.id);
        }
        if (restart) {
          if (!discoverHosts(manager.configPath).aliases.includes(settings.alias)) {
            throw new TunnelError('Selected alias is no longer discoverable. Edit Machine; existing tunnel was left running.');
          }
          const original = current.find(item => item.id === restart?.id);
          const ownsPort = original?.status === 'Running' && original.settings.local_port === settings.local_port;
          if (!ownsPort && !await availablePort(settings.local_port)) {
            throw new TunnelError('Replacement port is unavailable. Edit Local port; existing tunnel was left running.');
          }
          await manager.stop(restart.id);
          restart = undefined;
        }
        say(`Connecting to ${settings.alias}… SSH may request authentication.`);
        const result = await manager.start(record, started);
        if (settings.mode === 'foreground') say(`Tunnel ${result.status.toLowerCase()}.`);
        else {
          try {
            const next = await choose<string>('Next', ['Return to shell', 'Open as HTTP website', 'Open as HTTPS website']);
            if (next !== 'Return to shell') {
              const scheme = next === 'Open as HTTPS website' ? 'https' : 'http';
              const opened = spawnSync('/usr/bin/open', [`${scheme}://127.0.0.1:${settings.local_port}`], { stdio: 'ignore', timeout: 5000 });
              if (opened.status !== 0) say('Could not open the browser. Open the local address manually.');
            }
          } catch (error) {
            if (!(error instanceof Back || error instanceof Cancelled)) throw error;
            say('Tunnel remains running in the background.');
          }
        }
        return true;
      }
      step = editing ? 5 : step + 1;
      editing = false;
    } catch (error) {
      if (error instanceof Back) {
        if (editing) { step = 5; editing = false; }
        else if (step === 0) return false;
        else step--;
      } else if (error instanceof Cancelled || error instanceof TunnelCancelled) throw error;
      else if (error instanceof Error) say(`Could not start tunnel: ${error.message}`);
      else throw error;
    }
  }
}

async function manage(manager: Manager, id?: string, action?: string): Promise<boolean> {
  while (true) {
    try {
      const records = await manager.records();
      if (!records.length) { say('No managed tunnels yet.'); return false; }
      if (!id) id = await choose<string>('Manage tunnels', [...records.map(record => ({ name: label(record), value: record.id })), { name: 'Back', value: 'back' }]);
      if (id === 'back') return false;
      const record = records.find(item => item.id === id);
      if (!record) throw new TunnelError('Tunnel not found. Use tunnel-ts list for managed IDs.');
      const selected = action ?? await choose<string>(label(record), ['Details', 'Show command', 'View logs', 'Stop tunnel', 'Restart tunnel', 'Back']);
      if (selected === 'Details') details(record);
      else if (selected === 'Show command') showCommands(record);
      else if (selected === 'View logs') say(await manager.logs(record.id));
      else if (selected === 'Stop tunnel' || selected === 'stop') {
        if (await choose<string>(`Stop ${record.settings.alias} on local port ${record.settings.local_port}?`, ['Cancel', 'Stop tunnel']) === 'Stop tunnel') {
          await manager.stop(record.id);
          say('Tunnel stopped.');
        }
      } else if (selected === 'Restart tunnel' || selected === 'restart') return runWizard(manager, record.settings, record);
      else return false;
      if (action) return false;
    } catch (error) {
      if (error instanceof Back) return false;
      throw error;
    }
  }
}
async function recent(manager: Manager): Promise<boolean> {
  const records = (await manager.records()).reverse().filter(record => record.successful);
  const unique = new Map<string, TunnelRecord>();
  for (const record of records) {
    const s = record.settings;
    const key = JSON.stringify([s.alias, s.local_port, s.remote_port, s.destination, s.mode]);
    if (!unique.has(key)) unique.set(key, record);
  }
  if (!unique.size) { say('No successful tunnels to reuse yet.'); return false; }
  try {
    const record = await choose<TunnelRecord>('Recent tunnels', [...unique.values()].map(value => ({ name: label(value), value })));
    return record.status === 'Running' ? manage(manager, record.id) : runWizard(manager, record.settings);
  } catch (error) {
    if (error instanceof Back) return false;
    throw error;
  }
}
async function home(manager: Manager): Promise<void> {
  while (true) {
    const count = (await manager.records()).filter(record => record.status === 'Running').length;
    const action = await choose<string>('What would you like to do?', ['New tunnel', `Manage tunnels · ${count} running`, 'Recent tunnels', 'Exit']);
    if (action === 'Exit') return;
    const finished = action === 'New tunnel' ? await runWizard(manager) : action === 'Recent tunnels' ? await recent(manager) : await manage(manager);
    if (finished) return;
  }
}
export async function main(argv = process.argv.slice(2)): Promise<number> {
  try {
    const { values, positionals } = parseArgs({ args: argv, allowPositionals: true, options: {
      config: { type: 'string' }, 'state-dir': { type: 'string' },
      help: { type: 'boolean', short: 'h' }, version: { type: 'boolean' },
    } });
    if (values.help) {
      say('Usage: tunnel-ts [--config PATH] [--state-dir PATH] [list|stop [ID]|restart [ID]]\n\nGuided local SSH tunnels using existing config aliases.\n\n--version       Show version\n--config PATH   SSH config (default: ~/.ssh/config)\n--state-dir PATH Private, short state directory (default: ~/.tunnel-ts)\n\nlist            Print status without prompting\nstop [ID]       Confirm and stop an owned tunnel\nrestart [ID]    Review and restart an owned tunnel');
      return 0;
    }
    if (values.version) { say(`tunnel-ts ${VERSION}`); return 0; }
    const [command, id] = positionals;
    if (positionals.length > (command === 'list' ? 1 : 2) || (command && !['list', 'stop', 'restart'].includes(command))) {
      say('Invalid command. Use tunnel-ts --help.'); return 2;
    }
    if (command !== 'list' && (!process.stdin.isTTY || !process.stdout.isTTY)) {
      say('Interactive commands need a terminal. Use tunnel-ts list for noninteractive status.'); return 2;
    }
    if (process.platform !== 'darwin') { say('This comparison build currently supports macOS only.'); return 2; }
    const manager = new Manager(values.config, values['state-dir']);
    if (command === 'list') {
      const records = await manager.records();
      if (!records.length) say('No managed tunnels.');
      else { say('ID            MACHINE / LOCAL → DESTINATION / STATUS'); records.forEach(record => say(`${record.id}  ${label(record)}`)); }
    } else if (command) await manage(manager, id, command);
    else await home(manager);
    return 0;
  } catch (error) {
    if (error instanceof Back) return 0;
    if (error instanceof TunnelCancelled) { say(`\n${error.message}`); return 130; }
    if (error instanceof Cancelled) { say('\nCancelled.'); return 130; }
    say(`Error: ${error instanceof Error ? error.message : String(error)}`);
    return 1;
  }
}
if (process.argv[1] && realpathSync(resolve(process.argv[1])) === fileURLToPath(import.meta.url)) process.exitCode = await main();
