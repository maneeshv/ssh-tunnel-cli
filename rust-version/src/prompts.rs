//! Small prompt layer over inquire: Esc surfaces as Back, Ctrl+C as
//! Cancelled, a filter that matches nothing can never submit, and say()
//! prints untrusted text without terminal control sequences.

use inquire::ui::RenderConfig;
use inquire::validator::Validation;
use inquire::{InquireError, Select, Text};

use crate::core::TunnelError;

const HINT: &str = "Esc back · Ctrl+C cancel";

/// Outcome of an interactive step that did not produce an answer.
#[derive(Debug, PartialEq, Eq)]
pub enum UiError {
    /// The user pressed Esc to go back one step; collected answers stay valid.
    Back,
    /// The user pressed Ctrl+C (or the core was interrupted); exit code 130.
    Cancelled(String),
    /// Anything else worth reporting; shown to the user, flow continues or exits 1.
    Message(String),
}

pub type UiResult<T> = Result<T, UiError>;

impl From<InquireError> for UiError {
    fn from(error: InquireError) -> Self {
        match error {
            InquireError::OperationCanceled => UiError::Back,
            InquireError::OperationInterrupted => UiError::Cancelled("Cancelled.".into()),
            other => UiError::Message(other.to_string()),
        }
    }
}

impl From<TunnelError> for UiError {
    fn from(error: TunnelError) -> Self {
        match error {
            TunnelError::Cancelled(message) => UiError::Cancelled(message),
            TunnelError::Message(message) => UiError::Message(message),
        }
    }
}

/// Remove terminal control sequences — OSC (title/hyperlink injection), CSI,
/// any other escape even truncated, C0 except tab/newline, DEL, and C1 —
/// mirroring the Python/TypeScript UNSAFE pattern. Newlines and tabs survive.
pub fn sanitize(message: &str) -> String {
    let mut output = String::with_capacity(message.len());
    let mut chars = message.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\x1b' => match chars.peek() {
                // OSC: consume until BEL; a terminating ESC (as in ESC \) is
                // left for the next iteration, like the regex's optional ST.
                Some(']') => {
                    chars.next();
                    while let Some(&next) = chars.peek() {
                        if next == '\x1b' {
                            break;
                        }
                        chars.next();
                        if next == '\x07' {
                            break;
                        }
                    }
                }
                // CSI: parameter bytes, intermediate bytes, optional final byte.
                Some('[') => {
                    chars.next();
                    while chars
                        .peek()
                        .is_some_and(|&n| ('\x30'..='\x3f').contains(&n))
                    {
                        chars.next();
                    }
                    while chars
                        .peek()
                        .is_some_and(|&n| ('\x20'..='\x2f').contains(&n))
                    {
                        chars.next();
                    }
                    if chars
                        .peek()
                        .is_some_and(|&n| ('\x40'..='\x7e').contains(&n))
                    {
                        chars.next();
                    }
                }
                // Any other escape, even truncated: drop ESC plus one char.
                Some(_) => {
                    chars.next();
                }
                None => {}
            },
            '\t' | '\n' => output.push(c),
            '\x00'..='\x1f' | '\x7f' | '\u{80}'..='\u{9f}' => {}
            _ => output.push(c),
        }
    }
    output
}

/// Sanitize a menu label down to a single line: control sequences removed,
/// tabs and newlines collapsed to spaces.
pub fn one_line(label: &str) -> String {
    sanitize(label).replace(['\t', '\n'], " ")
}

/// Print possibly untrusted text (SSH diagnostics, config warnings) with
/// terminal control characters removed; newlines and tabs survive.
pub fn say(message: &str) {
    println!("{}", sanitize(message));
}

/// NO_COLOR present (even empty) forces monochrome rendering. inquire's own
/// default also treats presence as set, but pinning the global config keeps
/// that guarantee ours across inquire upgrades.
pub fn apply_color_preference() {
    if std::env::var_os("NO_COLOR").is_some() {
        inquire::set_global_render_config(RenderConfig::empty());
    }
}

/// Arrow-key menu returning the index of the selected option. With search,
/// typing filters the options; inquire refuses to submit while nothing is
/// highlighted, so an unmatched filter can never select and typed text is
/// never the answer. Errs Back on Esc, Cancelled on Ctrl+C.
pub fn choose<S: AsRef<str>>(
    message: &str,
    options: &[S],
    default: Option<usize>,
    search: bool,
) -> UiResult<usize> {
    let labels: Vec<String> = options
        .iter()
        .map(|option| one_line(option.as_ref()))
        .collect();
    let hint = format!(
        "{}↑↓ navigate · Enter select · {HINT}",
        if search { "type to filter · " } else { "" }
    );
    let mut select = Select::new(message, labels).with_help_message(&hint);
    if !search {
        select = select.without_filtering();
    }
    if let Some(index) = default {
        select = select.with_starting_cursor(index);
    }
    Ok(select.raw_prompt()?.index)
}

/// One-line answer with validation: invalid input shows the message and stays
/// on the same question. Errs Back on Esc, Cancelled on Ctrl+C.
pub fn text<V>(message: &str, initial: &str, validate: V) -> UiResult<String>
where
    V: Fn(&str) -> Result<(), String> + Clone + 'static,
{
    let answer = Text::new(message)
        .with_initial_value(initial)
        .with_help_message(HINT)
        .with_validator(move |input: &str| match validate(input) {
            Ok(()) => Ok(Validation::Valid),
            Err(message) => Ok(Validation::Invalid(message.into())),
        })
        .prompt()?;
    Ok(answer)
}

#[cfg(test)]
mod tests {
    use crate::prompts::{one_line, sanitize};

    #[test]
    fn sanitize_strips_osc() {
        assert_eq!(sanitize("a\x1b]0;evil title\x07b"), "ab");
        assert_eq!(sanitize("a\x1b]8;;http://x\x1b\\b"), "ab");
        assert_eq!(sanitize("a\x1b]unterminated"), "a");
        assert_eq!(sanitize("a\x1b]body\x1b[31mred"), "ared");
    }

    #[test]
    fn sanitize_strips_csi() {
        assert_eq!(sanitize("\x1b[31mred\x1b[0m"), "red");
        assert_eq!(sanitize("\x1b[m"), "");
        assert_eq!(sanitize("a\x1b[12;24"), "a");
        assert_eq!(sanitize("a\x1b[?25lb"), "ab");
    }

    #[test]
    fn sanitize_strips_other_escapes_even_truncated() {
        assert_eq!(sanitize("a\x1bZb"), "ab");
        assert_eq!(sanitize("a\x1b"), "a");
    }

    #[test]
    fn sanitize_strips_c0_del_c1_keeps_tabs_newlines() {
        assert_eq!(sanitize("a\x00b\rc\x7fd\u{80}e\u{9f}f"), "abcdef");
        assert_eq!(sanitize("keep\ttabs\nand lines"), "keep\ttabs\nand lines");
        assert_eq!(sanitize("café ↑"), "café ↑");
    }

    #[test]
    fn one_line_flattens() {
        assert_eq!(one_line("two\nlines\there"), "two lines here");
        assert_eq!(one_line("\x1b[31mhost\n"), "host ");
    }
}
