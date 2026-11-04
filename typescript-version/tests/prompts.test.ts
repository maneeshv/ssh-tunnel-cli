import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { render } from '@inquirer/testing';
import { Back, Cancelled, choose, sanitize, say, text } from '#prompts';

type ChooseArgs<T> = {
  message: string;
  choices: ReadonlyArray<string | { name: string; value: T }>;
  options?: { default?: T; search?: boolean };
};

// Adapters so @inquirer/testing's render() can drive our (message, ..., context) signatures.
const choosePrompt = <T>(config: ChooseArgs<T>, context?: Parameters<typeof choose>[3]) =>
  choose(config.message, config.choices, config.options, context);

type TextArgs = {
  message: string;
  default?: string;
  validate?: (value: string) => void | boolean | Promise<void | boolean>;
};
const textPrompt = (config: TextArgs, context?: Parameters<typeof text>[3]) =>
  text(config.message, config.default, config.validate, context);

const tick = () => new Promise((resolve) => setImmediate(resolve));

function settles(promise: Promise<unknown>): () => boolean {
  let settled = false;
  promise.then(
    () => (settled = true),
    () => (settled = true),
  );
  return () => settled;
}

const hadNoColor = 'NO_COLOR' in process.env;
afterEach(() => {
  if (!hadNoColor) delete process.env.NO_COLOR;
});

test('choose: arrow keys select a typed value, hint is shown', async () => {
  const { answer, events, getScreen } = await render(choosePrompt<number>, {
    message: 'Pick a port',
    choices: [
      { name: 'web', value: 8080 },
      { name: 'db', value: 5432 },
    ],
  });
  const screen = getScreen();
  assert.match(screen, /Pick a port/);
  assert.match(screen, /↑↓ navigate · Enter select · Esc back · Ctrl\+C cancel/);
  events.keypress('down');
  events.keypress('enter');
  assert.equal(await answer, 5432);
});

test('choose: default picks the initially highlighted choice', async () => {
  const { answer, events } = await render(choosePrompt<string>, {
    message: 'Host',
    choices: ['alpha', 'beta', 'gamma'],
    options: { default: 'beta' },
  });
  events.keypress('enter');
  assert.equal(await answer, 'beta');
});

test('choose: Esc rejects with Back', async () => {
  const { answer, events } = await render(choosePrompt<string>, {
    message: 'Host',
    choices: ['alpha', 'beta'],
  });
  events.keypress('escape');
  await assert.rejects(answer, Back);
});

test('choose: Ctrl+C rejects with Cancelled', async () => {
  const { answer, events } = await render(choosePrompt<string>, {
    message: 'Host',
    choices: ['alpha', 'beta'],
  });
  events.keypress({ name: 'c', ctrl: true });
  await assert.rejects(answer, Cancelled);
});

test('choose search: typing filters and Enter submits the match value, not the text', async () => {
  const { answer, events, getScreen, nextRender } = await render(choosePrompt<string>, {
    message: 'Host',
    choices: [
      { name: 'web-prod', value: 'web-prod.example' },
      { name: 'db-prod', value: 'db-prod.example' },
    ],
    options: { search: true },
  });
  assert.match(getScreen(), /type to filter/);
  events.type('web');
  await nextRender();
  const screen = getScreen();
  assert.match(screen, /web-prod/);
  assert.doesNotMatch(screen, /db-prod/);
  events.keypress('enter');
  assert.equal(await answer, 'web-prod.example');
});

test('choose search: Enter on an unmatched filter submits nothing; Esc still goes back', async () => {
  const { answer, events, getScreen, nextRender } = await render(choosePrompt<string>, {
    message: 'Host',
    choices: [{ name: 'web-prod', value: 'web-prod.example' }],
    options: { search: true },
  });
  const done = settles(answer);
  events.type('zzz');
  await nextRender();
  assert.match(getScreen(), /No results found/);
  events.keypress('enter');
  await tick();
  assert.equal(done(), false);
  events.keypress('escape');
  await assert.rejects(answer, Back);
});

test('text: Enter on empty input returns the default', async () => {
  const { answer, events, getScreen } = await render(textPrompt, {
    message: 'Local port',
    default: '8080',
  });
  assert.match(getScreen(), /Local port \(Esc back · Ctrl\+C cancel\)/);
  events.keypress('enter');
  assert.equal(await answer, '8080');
});

test('text: validator returning false and throwing Error both block submission', async () => {
  const { answer, events, getScreen, nextRender } = await render(textPrompt, {
    message: 'Port',
    validate: (value: string) => {
      if (value === 'bad') return false;
      if (!/^\d+$/.test(value)) throw new Error('Port must be a number.');
      return true;
    },
  });
  const done = settles(answer);
  events.type('bad');
  events.keypress('enter');
  await nextRender();
  assert.match(getScreen(), /Invalid input\./);
  assert.equal(done(), false);
  for (let i = 0; i < 3; i++) events.keypress('backspace');
  events.type('x1');
  events.keypress('enter');
  await nextRender();
  assert.match(getScreen(), /Port must be a number\./);
  assert.equal(done(), false);
  for (let i = 0; i < 2; i++) events.keypress('backspace');
  events.type('22');
  events.keypress('enter');
  assert.equal(await answer, '22');
});

test('text: Esc rejects with Back even after typing', async () => {
  const { answer, events } = await render(textPrompt, { message: 'Host' });
  events.type('partial');
  events.keypress('escape');
  await assert.rejects(answer, Back);
});

test('NO_COLOR set to empty string removes ANSI color codes', async () => {
  process.env.NO_COLOR = '';
  const { events, getScreen, answer } = await render(choosePrompt<string>, {
    message: 'Host',
    choices: ['alpha', 'beta'],
  });
  // SGR (color) sequences end in 'm'; cursor-control sequences are allowed.
  assert.doesNotMatch(getScreen({ raw: true }), /\x1b\[[0-9;]*m/);
  events.keypress('enter');
  await answer;
});

test('sanitize strips OSC, CSI, and C0 controls but keeps tabs and newlines', () => {
  assert.equal(sanitize('\x1b]0;evil title\x07hi'), 'hi');
  assert.equal(sanitize('\x1b]8;;https://evil\x1b\\link'), 'link');
  assert.equal(sanitize('\x1b[31mred\x1b[0m'), 'red');
  assert.equal(sanitize('a\x00b\x07c\x7fd'), 'abcd');
  assert.equal(sanitize('truncated\x1b'), 'truncated');
  assert.equal(sanitize('keep\tthis\nline'), 'keep\tthis\nline');
});

test('say prints sanitized text', (t) => {
  const log = t.mock.method(console, 'log', () => {});
  say('\x1b[2Jdiag\x1b]0;x\x07nostic');
  assert.deepEqual(log.mock.calls[0]?.arguments, ['diagnostic']);
  say();
  assert.deepEqual(log.mock.calls[1]?.arguments, ['']);
});
