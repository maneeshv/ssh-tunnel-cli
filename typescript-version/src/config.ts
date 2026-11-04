import { existsSync, readFileSync, readdirSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';

export interface Discovery {
  aliases: string[];
  warnings: string[];
}

export function isAlias(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_]/.test(value) && !/[^A-Za-z0-9_.-]/.test(value);
}

function words(input: string): string[] {
  const result: string[] = [];
  let value = '';
  let quote = '';
  let started = false;
  for (let index = 0; index < input.length; index++) {
    const character = input[index]!;
    if (character === '\\' && quote !== "'") {
      const next = input[++index];
      if (next === undefined) throw new Error('trailing escape');
      if (quote === '"' && next !== '"' && next !== '\\') value += '\\';
      value += next;
      started = true;
    } else if (quote) {
      if (character === quote) quote = '';
      else value += character;
    } else if (character === '#') {
      break;
    } else if (character === '"' || character === "'") {
      quote = character;
      started = true;
    } else if (/\s/.test(character)) {
      if (started) result.push(value);
      value = '';
      started = false;
    } else {
      value += character;
      started = true;
    }
  }
  if (quote) throw new Error('unterminated quote');
  if (started) result.push(value);
  if (!result.length) throw new Error('missing argument');
  return result;
}

function expandHome(value: string): string {
  if (value === '~') return homedir();
  if (value.startsWith('~/')) return join(homedir(), value.slice(2));
  // shortcut: named-user tildes need a passwd lookup; warn until that use case is requested.
  if (value.startsWith('~')) throw new Error('named-user home expansion is unsupported');
  return value;
}

function glob(pattern: string): string[] {
  let paths = ['/'];
  for (const part of pattern.split('/').filter(Boolean)) {
    if (!/[*?[]/.test(part)) {
      paths = paths.map((path) => join(path, part)).filter(existsSync);
      continue;
    }
    let expression = '';
    for (let index = 0; index < part.length; index++) {
      const character = part[index]!;
      if (character === '*') expression += '[\\s\\S]*';
      else if (character === '?') expression += '[\\s\\S]';
      else if (character === '[') {
        let end = index + 1;
        if (part[end] === '!') end++;
        if (part[end] === ']') end++;
        end = part.indexOf(']', end);
        if (end === -1) expression += '\\[';
        else {
          let members = part.slice(index + 1, end).replace(/\\/g, '\\\\').replace(/\]/g, '\\]');
          if (members.startsWith('!')) members = `^${members.slice(1)}`;
          else if (members.startsWith('^')) members = `\\${members}`;
          expression += `[${members}]`;
          index = end;
        }
      } else expression += character.replace(/[\\^$.*+?()[\]{}|]/g, '\\$&');
    }
    const matcher = new RegExp(`^(?:${expression})(?![\\s\\S])`);
    paths = paths.flatMap((path) => {
      let names: string[];
      try { names = readdirSync(path); }
      catch { return []; }
      return names.filter((name) => (!name.startsWith('.') || part.startsWith('.')) && matcher.test(name)).map((name) => join(path, name));
    });
  }
  return paths.sort();
}

export function discoverHosts(configPath = join(homedir(), '.ssh', 'config')): Discovery {
  const base = join(homedir(), '.ssh');
  const result: Discovery = { aliases: [], warnings: [] };
  const seen = new Set<string>();
  const active = new Set<string>();
  const incomplete = '; host list may be incomplete.';

  function read(path: string, unconditional: boolean, depth: number): boolean {
    let lines: string[];
    try {
      path = realpathSync(expandHome(path));
      if (active.has(path)) {
        result.warnings.push(`${path}: Include cycle skipped${incomplete}`);
        return unconditional;
      }
      if (depth > 16) {
        result.warnings.push(`${path}: Include depth limit reached${incomplete}`);
        return unconditional;
      }
      lines = new TextDecoder('utf-8', { fatal: true }).decode(readFileSync(path)).split(/\r\n|[\r\n]/);
    } catch (error) {
      result.warnings.push(`${path}: Cannot read SSH config (${String(error)})${incomplete}`);
      return unconditional;
    }
    active.add(path);
    try {
      for (const [index, line] of lines.entries()) {
        const match = /^\s*([^\s=#]+)(?:\s*=\s*|\s+)?(.*)/.exec(line);
        if (!match) continue;
        const keyword = match[1]!.toLowerCase();
        if (!['host', 'match', 'include'].includes(keyword)) continue;
        const location = `${path}:${index + 1}`;
        let values: string[];
        try {
          values = words(match[2]!);
        } catch (error) {
          result.warnings.push(`${location}: Invalid ${keyword} (${String(error)})${incomplete}`);
          if (keyword !== 'include') unconditional = false;
          continue;
        }
        if (keyword === 'host') {
          unconditional = values.length === 1 && values[0] === '*';
          for (const alias of values) {
            if (/[*?!]/.test(alias)) continue;
            if (!isAlias(alias)) result.warnings.push(`${location}: Unsupported alias ${JSON.stringify(alias)} skipped.`);
            else if (!seen.has(alias)) {
              seen.add(alias);
              result.aliases.push(alias);
            }
          }
        } else if (keyword === 'match') {
          unconditional = values.length === 1 && values[0]!.toLowerCase() === 'all';
        } else if (!unconditional) {
          result.warnings.push(`${location}: Conditional Include skipped${incomplete}`);
        } else {
          for (const value of values) {
            if (/[$%]/.test(value)) {
              result.warnings.push(`${location}: Dynamic Include ${JSON.stringify(value)} skipped${incomplete}`);
              continue;
            }
            try {
              let pattern = expandHome(value);
              if (!isAbsolute(pattern)) pattern = join(base, pattern);
              // Node glob expands braces and recursive stars; SSH Includes only use shell wildcards.
              const paths = glob(pattern);
              if (!paths.length) result.warnings.push(`${location}: Include ${JSON.stringify(value)} matched no files${incomplete}`);
              for (const included of paths) unconditional = read(included, unconditional, depth + 1);
            } catch (error) {
              result.warnings.push(`${location}: Cannot expand Include ${JSON.stringify(value)} (${String(error)})${incomplete}`);
            }
          }
        }
      }
    } finally {
      active.delete(path);
    }
    return unconditional;
  }

  read(configPath, true, 0);
  return result;
}
