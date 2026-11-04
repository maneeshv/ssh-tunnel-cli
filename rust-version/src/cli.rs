//! Guided setup and management, with no connection before a reviewed Start.

use std::path::PathBuf;
use std::process::ExitCode;

use crate::config::discover_hosts;
use crate::core::{Manager, Record, Settings, available_port, validate_destination, validate_port};
use crate::prompts::{UiError, UiResult, apply_color_preference, choose, say, text};

const HELP: &str = "Usage: tunnel-rs [--config PATH] [--state-dir PATH] [list|stop [ID]|restart [ID]]\n\n\
Guided local SSH tunnels using existing config aliases.\n\n\
--version        Show version\n\
--config PATH    SSH config (default: ~/.ssh/config)\n\
--state-dir PATH Private, short state directory (default: ~/.tunnel-rs)\n\n\
list            Print status without prompting\n\
stop [ID]       Confirm and stop an owned tunnel\n\
restart [ID]    Review and restart an owned tunnel";

const REVIEW: usize = 5;

fn label(record: &Record) -> String {
    let s = &record.settings;
    format!(
        "{}  127.0.0.1:{} → {}:{}  {}",
        s.alias, s.local_port, s.destination, s.remote_port, record.status
    )
}

/// Shell-quote one argument for display only; nothing is executed via a shell.
fn quote(argument: &str) -> String {
    let safe = !argument.is_empty()
        && argument
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&c));
    if safe {
        argument.to_string()
    } else {
        format!("'{}'", argument.replace('\'', "'\"'\"'"))
    }
}

fn show_commands(record: &Record) {
    say("\nExact commands — private SSH connection, then the selected forward:");
    for (index, command) in record.commands.iter().enumerate() {
        let joined = command
            .iter()
            .map(|arg| quote(arg))
            .collect::<Vec<_>>()
            .join(" ");
        say(&format!("  {}. {joined}", index + 1));
    }
}

fn show_details(record: &Record) {
    say(&format!(
        "\n{}\nID: {}\nMode: {}\nCreated: {}",
        label(record),
        record.id,
        record.settings.mode,
        record.created_at
    ));
    if let Some(last_error) = &record.last_error {
        say(&format!("Last diagnostic: {last_error}"));
    }
    say("Running describes the SSH tunnel; application health is not checked.");
}

fn host(manager: &Manager, selected: Option<&str>) -> UiResult<String> {
    loop {
        let discovery = discover_hosts(manager.config_path.as_deref());
        for warning in &discovery.warnings {
            say(&format!("Warning: {warning}"));
        }
        if !discovery.aliases.is_empty() {
            let default =
                selected.and_then(|alias| discovery.aliases.iter().position(|a| a == alias));
            let index = choose("Choose a machine", &discovery.aliases, default, true)?;
            return Ok(discovery.aliases[index].clone());
        }
        say(
            "No literal SSH aliases found. Add a Host alias to your SSH config manually, then reload.",
        );
        if choose("Next", &["Reload hosts", "Back"], None, false)? == 1 {
            return Err(UiError::Back);
        }
    }
}

fn same_route(a: &Settings, b: &Settings) -> bool {
    a.alias == b.alias
        && a.local_port == b.local_port
        && a.remote_port == b.remote_port
        && a.destination == b.destination
}

fn started(record: &Record) -> crate::core::Result<()> {
    let settings = &record.settings;
    say(&format!(
        "\nTunnel running in {}: 127.0.0.1:{}\nID: {}",
        settings.mode, settings.local_port, record.id
    ));
    say("Tunnel active; application health not checked.");
    if settings.mode == "foreground" {
        say("Ctrl+C stops this tunnel.");
    }
    Ok(())
}

fn capitalize(value: &str) -> String {
    let mut chars = value.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

/// Wizard answers collected so far; every field is set once review is reached.
#[derive(Clone, Default)]
struct Draft {
    alias: Option<String>,
    remote_port: Option<u16>,
    local_port: Option<u16>,
    destination: Option<String>,
    mode: Option<String>,
}

impl Draft {
    fn from_settings(settings: &Settings) -> Self {
        Self {
            alias: Some(settings.alias.clone()),
            remote_port: Some(settings.remote_port),
            local_port: Some(settings.local_port),
            destination: Some(settings.destination.clone()),
            mode: Some(settings.mode.clone()),
        }
    }

    fn settings(&self) -> Settings {
        Settings {
            alias: self.alias.clone().unwrap_or_default(),
            remote_port: self.remote_port.unwrap_or_default(),
            local_port: self.local_port.unwrap_or_default(),
            destination: self.destination.clone().unwrap_or_default(),
            mode: self.mode.clone().unwrap_or_default(),
        }
    }
}

/// Where Esc lands: the review screen while editing a single field, out of
/// the wizard from the first step, otherwise one step back.
fn step_after_back(step: usize, editing: bool) -> Option<usize> {
    if editing {
        Some(REVIEW)
    } else if step == 0 {
        None
    } else {
        Some(step.checked_sub(1)?)
    }
}

enum StepResult {
    Next,
    Edit(usize),
    Review,
    Finished(bool),
}

fn ask_step(
    manager: &Manager,
    answers: &mut Draft,
    step: usize,
    restart: Option<&Record>,
) -> UiResult<()> {
    match step {
        0 => {
            let alias = host(manager, answers.alias.as_deref())?;
            answers.alias = Some(alias);
        }
        1 => {
            say("Remote application port — the SSH server port comes from SSH config.");
            let initial = answers
                .remote_port
                .map_or(String::new(), |port| port.to_string());
            let value = text("Remote port", &initial, |input| {
                validate_port(input)
                    .map(|_| ())
                    .map_err(|error| error.to_string())
            })?;
            answers.remote_port = Some(validate_port(&value)?);
        }
        2 => {
            let owned_port = restart
                .filter(|record| record.status == "Running")
                .map(|record| record.settings.local_port);
            let initial = answers
                .local_port
                .or(answers.remote_port)
                .map_or(String::new(), |port| port.to_string());
            let value = text("Local port", &initial, move |input| {
                let port = validate_port(input).map_err(|error| error.to_string())?;
                if owned_port != Some(port) && !available_port(port) {
                    return Err(format!(
                        "Port {port} is unavailable. Enter another local port."
                    ));
                }
                Ok(())
            })?;
            answers.local_port = Some(validate_port(&value)?);
        }
        3 => {
            say(
                "127.0.0.1 means the selected remote machine. Advanced: another host/IP reachable from it.",
            );
            let initial = answers
                .destination
                .clone()
                .unwrap_or_else(|| "127.0.0.1".into());
            let value = text("Destination", &initial, |input| {
                validate_destination(input)
                    .map(|_| ())
                    .map_err(|error| error.to_string())
            })?;
            answers.destination = Some(validate_destination(&value)?);
        }
        _ => {
            say("Background returns to your shell. Foreground stays attached until Ctrl+C.");
            let default = usize::from(answers.mode.as_deref() == Some("foreground"));
            let index = choose(
                "How should it run?",
                &["Background", "Foreground"],
                Some(default),
                false,
            )?;
            answers.mode = Some(
                if index == 1 {
                    "foreground"
                } else {
                    "background"
                }
                .into(),
            );
        }
    }
    Ok(())
}

fn review(
    manager: &Manager,
    answers: &Draft,
    restart: &mut Option<Record>,
) -> UiResult<StepResult> {
    let settings = answers.settings();
    let record = manager.prepare(settings.clone())?;
    say("\nReady to start");
    say(&format!(
        "  Machine       {}\n  Local         127.0.0.1:{}\n  Destination   {}:{} on {}\n  Mode          {}",
        settings.alias,
        settings.local_port,
        settings.destination,
        settings.remote_port,
        settings.alias,
        capitalize(&settings.mode)
    ));
    if let Some(existing) = restart.as_ref() {
        say(&format!(
            "Restart will stop tunnel {} before starting this route. Existing connections will be interrupted.",
            existing.id
        ));
    }
    show_commands(&record);
    match choose(
        "Review",
        &["Start tunnel", "Edit settings", "Cancel"],
        None,
        false,
    )? {
        2 => return Ok(StepResult::Finished(false)),
        1 => {
            let fields = [
                "Machine",
                "Remote port",
                "Local port",
                "Destination",
                "Run mode",
            ];
            return match choose("Edit which setting?", &fields, None, false) {
                Ok(index) => Ok(StepResult::Edit(index)),
                Err(UiError::Back) => Ok(StepResult::Review),
                Err(error) => Err(error),
            };
        }
        _ => {}
    }
    let current = manager.records()?;
    let restart_id = restart.as_ref().map(|existing| existing.id.clone());
    let duplicate = current.iter().find(|item| {
        item.status == "Running"
            && same_route(&item.settings, &settings)
            && restart_id.as_deref() != Some(item.id.as_str())
    });
    if let Some(existing) = duplicate {
        say("That route is already running. Opening its management actions.");
        return Ok(StepResult::Finished(manage(
            manager,
            Some(existing.id.clone()),
            None,
        )?));
    }
    if let Some(id) = restart_id {
        // Check alias and port before stopping, so a bad replacement never
        // takes down the tunnel it was meant to replace.
        if !discover_hosts(manager.config_path.as_deref())
            .aliases
            .contains(&settings.alias)
        {
            return Err(UiError::Message(
                "Selected alias is no longer discoverable. Edit Machine before restarting; existing tunnel was left running.".into(),
            ));
        }
        let original = current.iter().find(|item| item.id == id);
        let owns_port = original.is_some_and(|item| {
            item.status == "Running" && item.settings.local_port == settings.local_port
        });
        if !owns_port && !available_port(settings.local_port) {
            return Err(UiError::Message(
                "Replacement port is unavailable. Edit Local port; existing tunnel was left running.".into(),
            ));
        }
        manager.stop(&id)?;
        *restart = None;
    }
    say(&format!(
        "Connecting to {}… SSH may request authentication.",
        settings.alias
    ));
    let result = manager.start(record, started)?;
    if settings.mode == "foreground" {
        say(&format!("Tunnel {}.", result.status.to_lowercase()));
    } else {
        let next = choose(
            "Next",
            &[
                "Return to shell",
                "Open as HTTP website",
                "Open as HTTPS website",
            ],
            None,
            false,
        );
        match next {
            Ok(0) => {}
            Ok(selection) => {
                let scheme = if selection == 2 { "https" } else { "http" };
                // URL is built from the validated u16 port only.
                let url = format!("{scheme}://127.0.0.1:{}", settings.local_port);
                let opened = std::process::Command::new("/usr/bin/open")
                    .arg(&url)
                    .status();
                if !opened.map(|status| status.success()).unwrap_or(false) {
                    say("Could not open the browser. Open the local address manually.");
                }
            }
            Err(UiError::Back | UiError::Cancelled(_)) => {
                say("Tunnel remains running in the background.");
            }
            Err(error) => return Err(error),
        }
    }
    Ok(StepResult::Finished(true))
}

fn run_wizard(
    manager: &Manager,
    initial: Option<&Settings>,
    mut restart: Option<Record>,
) -> UiResult<bool> {
    let mut answers = initial.map(Draft::from_settings).unwrap_or_default();
    let mut step = if initial.is_some() { REVIEW } else { 0 };
    let mut editing = false;
    loop {
        let result = if step < REVIEW {
            ask_step(manager, &mut answers, step, restart.as_ref()).map(|()| StepResult::Next)
        } else {
            review(manager, &answers, &mut restart)
        };
        match result {
            Ok(StepResult::Next) => {
                step = if editing { REVIEW } else { step + 1 };
                editing = false;
            }
            Ok(StepResult::Edit(index)) => {
                editing = true;
                step = index;
            }
            Ok(StepResult::Review) => {
                step = REVIEW;
                editing = false;
            }
            Ok(StepResult::Finished(finished)) => return Ok(finished),
            Err(UiError::Back) => match step_after_back(step, editing) {
                Some(next) => {
                    step = next;
                    editing = false;
                }
                None => return Ok(false),
            },
            Err(cancelled @ UiError::Cancelled(_)) => return Err(cancelled),
            Err(UiError::Message(message)) => {
                // Answers stay intact; review is rebuilt for every attempt,
                // so retry cannot reuse a reserved ID.
                say(&format!("Could not start tunnel: {message}"));
            }
        }
    }
}

fn manage(manager: &Manager, mut id: Option<String>, action: Option<&str>) -> UiResult<bool> {
    loop {
        match manage_once(manager, &mut id, action) {
            Ok(Some(finished)) => return Ok(finished),
            Ok(None) => {}
            Err(UiError::Back) => return Ok(false),
            Err(error) => return Err(error),
        }
    }
}

fn manage_once(
    manager: &Manager,
    id: &mut Option<String>,
    action: Option<&str>,
) -> UiResult<Option<bool>> {
    let records = manager.records()?;
    if records.is_empty() {
        say("No managed tunnels yet.");
        return Ok(Some(false));
    }
    if id.is_none() {
        let mut labels: Vec<String> = records.iter().map(label).collect();
        labels.push("Back".into());
        let index = choose("Manage tunnels", &labels, None, false)?;
        if index == records.len() {
            return Ok(Some(false));
        }
        *id = Some(records[index].id.clone());
    }
    let selected_id = id.clone().unwrap_or_default();
    let record = records
        .iter()
        .find(|item| item.id == selected_id)
        .ok_or_else(|| {
            UiError::Message("Tunnel not found. Use tunnel-rs list for managed IDs.".into())
        })?;
    let options = [
        "Details",
        "Show command",
        "View logs",
        "Stop tunnel",
        "Restart tunnel",
        "Back",
    ];
    let selected = match action {
        Some(name) => name,
        None => options[choose(&label(record), &options, None, false)?],
    };
    match selected {
        "Details" => show_details(record),
        "Show command" => show_commands(record),
        "View logs" => say(&manager.logs(&selected_id)?),
        "Stop tunnel" | "stop" => {
            let message = format!(
                "Stop {} on local port {}?",
                record.settings.alias, record.settings.local_port
            );
            if choose(&message, &["Cancel", "Stop tunnel"], None, false)? == 1 {
                manager.stop(&selected_id)?;
                say("Tunnel stopped.");
            }
        }
        "Restart tunnel" | "restart" => {
            return Ok(Some(run_wizard(
                manager,
                Some(&record.settings),
                Some(record.clone()),
            )?));
        }
        _ => return Ok(Some(false)),
    }
    if action.is_some() {
        return Ok(Some(false));
    }
    Ok(None)
}

fn recent(manager: &Manager) -> UiResult<bool> {
    let mut unique: Vec<Record> = Vec::new();
    for record in manager
        .records()?
        .into_iter()
        .rev()
        .filter(|record| record.successful)
    {
        let settings = &record.settings;
        if !unique.iter().any(|existing| {
            same_route(&existing.settings, settings) && existing.settings.mode == settings.mode
        }) {
            unique.push(record);
        }
    }
    if unique.is_empty() {
        say("No successful tunnels to reuse yet.");
        return Ok(false);
    }
    let labels: Vec<String> = unique.iter().map(label).collect();
    let index = match choose("Recent tunnels", &labels, None, false) {
        Ok(index) => index,
        Err(UiError::Back) => return Ok(false),
        Err(error) => return Err(error),
    };
    let record = &unique[index];
    if record.status == "Running" {
        manage(manager, Some(record.id.clone()), None)
    } else {
        run_wizard(manager, Some(&record.settings), None)
    }
}

fn home(manager: &Manager) -> UiResult<()> {
    loop {
        let count = manager
            .records()?
            .iter()
            .filter(|record| record.status == "Running")
            .count();
        let options = [
            "New tunnel".to_string(),
            format!("Manage tunnels · {count} running"),
            "Recent tunnels".to_string(),
            "Exit".to_string(),
        ];
        let choice = match choose("What would you like to do?", &options, None, false) {
            Ok(choice) => choice,
            Err(UiError::Back) => return Ok(()),
            Err(error) => return Err(error),
        };
        let finished = match choice {
            0 => run_wizard(manager, None, None)?,
            1 => manage(manager, None, None)?,
            2 => recent(manager)?,
            _ => return Ok(()),
        };
        if finished {
            return Ok(());
        }
    }
}

fn list(manager: &Manager) -> UiResult<()> {
    let records = manager.records()?;
    if records.is_empty() {
        say("No managed tunnels.");
        return Ok(());
    }
    say("ID            MACHINE / LOCAL → DESTINATION / STATUS");
    for record in &records {
        say(&format!("{}  {}", record.id, label(record)));
    }
    Ok(())
}

#[derive(Debug, Default, PartialEq, Eq)]
struct Args {
    config: Option<PathBuf>,
    state_dir: Option<PathBuf>,
    help: bool,
    version: bool,
    command: Option<String>,
    id: Option<String>,
}

fn parse_args(argv: &[String]) -> Result<Args, String> {
    let mut args = Args::default();
    let mut positionals: Vec<String> = Vec::new();
    let mut iter = argv.iter();
    while let Some(argument) = iter.next() {
        let (flag, inline) = match argument.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_string())),
            _ => (argument.as_str(), None),
        };
        match flag {
            "--help" | "-h" => args.help = true,
            "--version" => args.version = true,
            "--config" | "--state-dir" => {
                let value = inline
                    .or_else(|| iter.next().cloned())
                    .ok_or_else(|| format!("Missing value for {flag}. Use tunnel-rs --help."))?;
                if flag == "--config" {
                    args.config = Some(PathBuf::from(value));
                } else {
                    args.state_dir = Some(PathBuf::from(value));
                }
            }
            _ if flag.starts_with('-') => {
                return Err("Invalid command. Use tunnel-rs --help.".into());
            }
            _ => positionals.push(argument.clone()),
        }
    }
    let mut positionals = positionals.into_iter();
    args.command = positionals.next();
    args.id = positionals.next();
    let valid = positionals.next().is_none()
        && match args.command.as_deref() {
            None | Some("stop" | "restart") => true,
            Some("list") => args.id.is_none(),
            Some(_) => false,
        };
    if !valid {
        return Err("Invalid command. Use tunnel-rs --help.".into());
    }
    Ok(args)
}

fn is_tty() -> bool {
    // SAFETY: isatty only inspects the file descriptors.
    unsafe { libc::isatty(libc::STDIN_FILENO) == 1 && libc::isatty(libc::STDOUT_FILENO) == 1 }
}

pub fn run(argv: &[String]) -> u8 {
    let args = match parse_args(argv) {
        Ok(args) => args,
        Err(message) => {
            say(&message);
            return 2;
        }
    };
    if args.help {
        say(HELP);
        return 0;
    }
    if args.version {
        say(&format!("tunnel-rs {}", env!("CARGO_PKG_VERSION")));
        return 0;
    }
    if !cfg!(target_os = "macos") {
        say("This comparison build currently supports macOS only.");
        return 2;
    }
    if args.command.as_deref() != Some("list") && !is_tty() {
        say("Interactive commands need a terminal. Use tunnel-rs list for noninteractive status.");
        return 2;
    }
    apply_color_preference();
    let command = args.command;
    let id = args.id;
    let outcome = Manager::new(args.config, args.state_dir)
        .map_err(UiError::from)
        .and_then(|manager| match command.as_deref() {
            Some("list") => list(&manager),
            Some(action) => manage(&manager, id, Some(action)).map(|_| ()),
            None => home(&manager),
        });
    match outcome {
        Ok(()) | Err(UiError::Back) => 0,
        Err(UiError::Cancelled(message)) => {
            say(&format!("\n{message}"));
            130
        }
        Err(UiError::Message(message)) => {
            say(&format!("Error: {message}"));
            1
        }
    }
}

pub fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    ExitCode::from(run(&argv))
}

#[cfg(test)]
mod tests {
    use crate::cli::{Args, REVIEW, capitalize, parse_args, quote, step_after_back};
    use std::path::PathBuf;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn parse_args_defaults_and_flags() {
        assert_eq!(parse_args(&[]).unwrap(), Args::default());
        let args = parse_args(&strings(&[
            "--config",
            "/a",
            "--state-dir=/b",
            "stop",
            "t1",
        ]))
        .unwrap();
        assert_eq!(args.config, Some(PathBuf::from("/a")));
        assert_eq!(args.state_dir, Some(PathBuf::from("/b")));
        assert_eq!(args.command.as_deref(), Some("stop"));
        assert_eq!(args.id.as_deref(), Some("t1"));
        assert!(parse_args(&strings(&["-h"])).unwrap().help);
        assert!(parse_args(&strings(&["--version"])).unwrap().version);
        assert_eq!(parse_args(&strings(&["restart"])).unwrap().id, None);
        assert_eq!(
            parse_args(&strings(&["list"])).unwrap().command.as_deref(),
            Some("list")
        );
    }

    #[test]
    fn parse_args_rejects_invalid() {
        for invalid in [
            vec!["bogus"],
            vec!["list", "extra"],
            vec!["stop", "id", "extra"],
            vec!["--config"],
            vec!["--unknown"],
            vec!["-x"],
        ] {
            assert!(parse_args(&strings(&invalid)).is_err(), "{invalid:?}");
        }
    }

    #[test]
    fn quote_matches_shlex_rules() {
        assert_eq!(quote("simple-arg_1.0:/@%+=,"), "simple-arg_1.0:/@%+=,");
        assert_eq!(quote(""), "''");
        assert_eq!(quote("has space"), "'has space'");
        assert_eq!(quote("semi;colon"), "'semi;colon'");
        assert_eq!(quote("it's"), "'it'\"'\"'s'");
        assert_eq!(quote("*"), "'*'");
    }

    #[test]
    fn back_navigation() {
        assert_eq!(step_after_back(0, false), None);
        assert_eq!(step_after_back(3, false), Some(2));
        assert_eq!(step_after_back(REVIEW, false), Some(REVIEW - 1));
        assert_eq!(step_after_back(2, true), Some(REVIEW));
        assert_eq!(step_after_back(0, true), Some(REVIEW));
    }

    #[test]
    fn capitalize_mode() {
        assert_eq!(capitalize("background"), "Background");
        assert_eq!(capitalize(""), "");
    }
}
