/** Small prompt layer over Inquirer: Esc rejects with Back, search never
 * submits an unmatched host, and say() prints untrusted text without
 * terminal control sequences. */

import { input, search, select } from '@inquirer/prompts';

const HINT = 'Esc back · Ctrl+C cancel';

/** The user pressed Esc to go back one step; collected answers stay valid. */
export class Back extends Error {
  override name = 'Back';
}

/** The user pressed Ctrl+C to cancel the whole flow. */
export class Cancelled extends Error {
  override name = 'Cancelled';
}

const UNSAFE = new RegExp(
  '\\x1b\\][^\\x07\\x1b]*(?:\\x07|\\x1b\\\\)?' + // OSC (title/hyperlink injection)
    '|\\x1b\\[[0-?]*[ -/]*[@-~]?' + // CSI sequences
    '|\\x1b.?' + // any other escape, even truncated
    '|[\\x00-\\x08\\x0b-\\x1f\\x7f\\x80-\\x9f]', // C0 except \t\n, DEL, C1
  'g',
);

/** Remove terminal control characters; newlines and tabs survive. */
export function sanitize(message: string): string {
  return String(message).replace(UNSAFE, '');
}

/** Print possibly untrusted text (SSH diagnostics, config warnings). */
export function say(message = ''): void {
  console.log(sanitize(message));
}

type Context = {
  input?: NodeJS.ReadableStream;
  output?: NodeJS.WritableStream;
};

type PromptTheme = {
  prefix?: { idle: string; done: string };
  spinner?: { interval: number; frames: string[] };
  style: {
    answer?: (text: string) => string;
    message?: (text: string) => string;
    error?: (text: string) => string;
    defaultAnswer?: (text: string) => string;
    help?: (text: string) => string;
    highlight?: (text: string) => string;
    key?: (text: string) => string;
    description?: (text: string) => string;
    searchTerm?: (text: string) => string;
    disabled?: (text: string) => string;
    keysHelpTip?: () => string;
  };
};

const plain = (text: string) => text;

/** NO_COLOR present (even empty) forces monochrome: Inquirer's default theme
 * styles via util.styleText, which treats an empty NO_COLOR as unset, so we
 * replace every style function rather than relying on it. */
function makeTheme(keysHelpTip?: () => string): PromptTheme {
  const theme: PromptTheme = { style: keysHelpTip ? { keysHelpTip } : {} };
  if ('NO_COLOR' in process.env) {
    theme.prefix = { idle: '?', done: '✔' };
    theme.spinner = { interval: 80, frames: ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'] };
    Object.assign(theme.style, {
      answer: plain,
      message: plain,
      error: (text: string) => `> ${text}`,
      defaultAnswer: (text: string) => `(${text})`,
      help: plain,
      highlight: plain,
      key: (text: string) => `<${text}>`,
      description: plain,
      searchTerm: plain,
      disabled: (text: string) => `- ${text}`,
    });
  }
  return theme;
}

/** Run one Inquirer prompt with Esc-goes-back wired in: an escape keypress
 * aborts the prompt (Inquirer tears the terminal down before rejecting, so
 * the screen is clean before any SSH process starts) and surfaces as Back;
 * Ctrl+C surfaces as Cancelled. */
async function ask<T>(
  run: (context: {
    input: NodeJS.ReadableStream;
    output?: NodeJS.WritableStream;
    signal: AbortSignal;
  }) => Promise<T>,
  context?: Context,
): Promise<T> {
  const stream = context?.input ?? process.stdin;
  const controller = new AbortController();
  const onKeypress = (_char: unknown, key?: { name?: string }) => {
    if (key?.name === 'escape') controller.abort(new Back());
  };
  // Inquirer's readline enables keypress events on this same stream.
  stream.on('keypress', onKeypress);
  try {
    return await run({ input: stream, output: context?.output, signal: controller.signal });
  } catch (error) {
    if (error instanceof Error) {
      if (error.name === 'AbortPromptError' && error.cause instanceof Back) throw error.cause;
      if (error.name === 'ExitPromptError') throw new Cancelled();
    }
    throw error;
  } finally {
    stream.removeListener('keypress', onKeypress);
  }
}

/** Arrow-key menu. With search, typing filters the choices; Inquirer's search
 * prompt only ever submits the highlighted match, so an invisible choice can
 * never be selected and typed text is never the answer. Rejects with Back on
 * Esc, Cancelled on Ctrl+C. */
export async function choose<T>(
  message: string,
  choices: ReadonlyArray<string | { name: string; value: T }>,
  options?: { default?: T; search?: boolean },
  context?: Context,
): Promise<T> {
  const items = choices.map((choice) =>
    typeof choice === 'string' ? { name: choice, value: choice as unknown as T } : choice,
  );
  const hint = `(${options?.search ? 'type to filter · ' : ''}↑↓ navigate · Enter select · ${HINT})`;
  const theme = makeTheme(() => hint);
  const preset = options?.default === undefined ? {} : { default: options.default };
  if (options?.search) {
    return ask(
      (io) =>
        search<T>(
          {
            message,
            source: (term) =>
              term
                ? items.filter((item) => item.name.toLowerCase().includes(term.toLowerCase()))
                : items,
            ...preset,
            theme,
          },
          io,
        ),
      context,
    );
  }
  return ask((io) => select<T>({ message, choices: items, ...preset, theme }, io), context);
}

/** One-line answer with optional validation: the callback marks input invalid
 * by returning false or throwing an Error (whose message is shown); anything
 * else accepts. Rejects with Back on Esc, Cancelled on Ctrl+C. */
export async function text(
  message: string,
  defaultValue?: string,
  validate?: (value: string) => void | boolean | Promise<void | boolean>,
  context?: Context,
): Promise<string> {
  const checked = validate
    ? async (value: string): Promise<true | string> => {
        try {
          return (await validate(value)) === false ? 'Invalid input.' : true;
        } catch (error) {
          if (error instanceof Error) return error.message || 'Invalid input.';
          throw error;
        }
      }
    : undefined;
  return ask(
    (io) =>
      input(
        {
          message: `${message} (${HINT})`,
          ...(defaultValue === undefined ? {} : { default: defaultValue }),
          ...(checked ? { validate: checked } : {}),
          theme: makeTheme(),
        },
        io,
      ),
    context,
  );
}
