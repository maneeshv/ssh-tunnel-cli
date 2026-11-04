//! Native OpenSSH lifecycle and private ownership records.
use crate::config::{discover_hosts, is_alias};
use libc::{SIGHUP, SIGINT, SIGTERM};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::net::{IpAddr, TcpListener};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{
    Mutex, MutexGuard,
    atomic::{AtomicBool, Ordering},
};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Debug)]
pub enum TunnelError {
    Message(String),
    Cancelled(String),
}
impl TunnelError {
    pub fn is_cancelled(&self) -> bool {
        matches!(self, Self::Cancelled(_))
    }
}
impl fmt::Display for TunnelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Message(s) | Self::Cancelled(s) => f.write_str(s),
        }
    }
}
impl std::error::Error for TunnelError {}
impl From<io::Error> for TunnelError {
    fn from(e: io::Error) -> Self {
        Self::Message(e.to_string())
    }
}
impl From<serde_json::Error> for TunnelError {
    fn from(e: serde_json::Error) -> Self {
        Self::Message(e.to_string())
    }
}
pub type Result<T> = std::result::Result<T, TunnelError>;
fn failure(message: impl Into<String>) -> TunnelError {
    TunnelError::Message(message.into())
}

pub fn validate_port(value: &str) -> Result<u16> {
    if value.is_empty() || value.len() > 5 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(failure("Enter a decimal port from 1 to 65535."));
    }
    value
        .parse::<u16>()
        .ok()
        .filter(|p| *p > 0)
        .ok_or_else(|| failure("Enter a decimal port from 1 to 65535."))
}
pub fn validate_destination(value: &str) -> Result<String> {
    if let Ok(ip) = value.parse::<IpAddr>() {
        return Ok(ip.to_string());
    }
    let valid = value.len() <= 253
        && value
            .strip_suffix('.')
            .unwrap_or(value)
            .split('.')
            .all(|part| {
                !part.is_empty()
                    && part.len() <= 63
                    && part.as_bytes()[0].is_ascii_alphanumeric()
                    && part.as_bytes()[part.len() - 1].is_ascii_alphanumeric()
                    && part.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
            });
    if valid {
        Ok(value.into())
    } else {
        Err(failure(
            "Enter a hostname or IP address, without a port or URL.",
        ))
    }
}
pub fn available_port(port: u16) -> bool {
    port != 0 && TcpListener::bind(("127.0.0.1", port)).is_ok()
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Settings {
    pub alias: String,
    pub remote_port: u16,
    pub local_port: u16,
    pub destination: String,
    pub mode: String,
}
impl Settings {
    pub fn validate(&self) -> Result<()> {
        if !is_alias(&self.alias) {
            return Err(failure("Choose a literal SSH-config alias."));
        }
        validate_port(&self.remote_port.to_string())?;
        validate_port(&self.local_port.to_string())?;
        validate_destination(&self.destination)?;
        if self.mode != "background" && self.mode != "foreground" {
            return Err(failure("Run mode must be background or foreground."));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Record {
    pub id: String,
    pub settings: Settings,
    pub created_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stopped_at: Option<String>,
    pub status: String,
    pub successful: bool,
    pub last_error: Option<String>,
    pub commands: Vec<Vec<String>>,
}
#[derive(Serialize, Deserialize)]
struct Registry {
    implementation: String,
    version: u32,
    tunnels: Vec<Record>,
}

fn now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as libc::time_t;
    // gmtime_r writes into caller-owned storage and does not use shared libc time state.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::gmtime_r(&seconds, &mut tm) }.is_null() {
        return seconds.to_string();
    }
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}
fn random_id() -> Result<String> {
    let mut bytes = [0u8; 6];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn absolute(path: PathBuf) -> Result<PathBuf> {
    let path = if path == Path::new("~") || path.starts_with("~/") {
        let home = std::env::var_os("HOME").ok_or_else(|| failure("HOME is not set."))?;
        PathBuf::from(home).join(path.strip_prefix("~").unwrap())
    } else {
        path
    };
    Ok(if path.is_absolute() {
        path
    } else {
        std::env::current_dir()?.join(path)
    })
}

static SIGNAL_OWNER: Mutex<()> = Mutex::new(());
static INTERRUPTED: AtomicBool = AtomicBool::new(false);
extern "C" fn interrupt(_: libc::c_int) {
    INTERRUPTED.store(true, Ordering::Relaxed);
}
struct Interrupts {
    previous: Vec<(libc::c_int, libc::sigaction)>,
    _owner: MutexGuard<'static, ()>,
}
impl Interrupts {
    fn new() -> Result<Self> {
        // Signal disposition belongs to the process, so serialize scopes even in library callers.
        let owner = SIGNAL_OWNER
            .lock()
            .map_err(|_| failure("Signal handling is unavailable after an earlier panic."))?;
        INTERRUPTED.store(false, Ordering::Relaxed);
        let mut result = Self {
            previous: vec![],
            _owner: owner,
        };
        for signal in [SIGINT, SIGHUP, SIGTERM] {
            let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
            let mut previous: libc::sigaction = unsafe { std::mem::zeroed() };
            action.sa_sigaction = interrupt as *const () as usize;
            action.sa_flags = libc::SA_RESTART;
            unsafe {
                libc::sigemptyset(&mut action.sa_mask);
            }
            if unsafe { libc::sigaction(signal, &action, &mut previous) } != 0 {
                return Err(io::Error::last_os_error().into());
            }
            result.previous.push((signal, previous));
        }
        Ok(result)
    }
    fn interrupted(&self) -> bool {
        INTERRUPTED.load(Ordering::Relaxed)
    }
    fn check(&self) -> Result<()> {
        if self.interrupted() {
            Err(TunnelError::Cancelled("Cancelled during startup.".into()))
        } else {
            Ok(())
        }
    }
}
impl Drop for Interrupts {
    fn drop(&mut self) {
        for (signal, action) in self.previous.iter().rev() {
            unsafe {
                libc::sigaction(*signal, action, std::ptr::null_mut());
            }
        }
    }
}

// A child handle refers only to a process launched here; never signal a stored/reused PID.
struct OwnedChild(Child);
impl OwnedChild {
    fn terminate(&mut self) -> Result<()> {
        if self.0.try_wait()?.is_none() {
            // The unreaped child cannot have its PID reused.
            unsafe {
                libc::kill(self.0.id() as libc::pid_t, SIGTERM);
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            while self.0.try_wait()?.is_none() {
                if Instant::now() >= deadline {
                    self.0.kill()?;
                    self.0.wait()?;
                    break;
                }
                thread::sleep(Duration::from_millis(25));
            }
        }
        Ok(())
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
#[derive(Debug)]
struct ControlResult {
    status: ExitStatus,
    stderr: String,
}
fn run(command: &[String], timeout: Duration) -> Result<ControlResult> {
    // Mux commands need no terminal; a separate process group protects cleanup from terminal Ctrl+C.
    let mut child = OwnedChild(
        Command::new(&command[0])
            .args(&command[1..])
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()?,
    );
    let mut pipe = child
        .0
        .stderr
        .take()
        .ok_or_else(|| failure("Missing SSH diagnostics pipe."))?;
    let fd = pipe.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error().into());
    }
    let deadline = Instant::now() + timeout;
    let mut bytes = Vec::new();
    let mut exit = None;
    loop {
        let mut buffer = [0u8; 4096];
        match pipe.read(&mut buffer) {
            Ok(0) if exit.is_some() => break,
            Ok(n) => {
                bytes.extend_from_slice(&buffer[..n]);
                if bytes.len() > 65536 {
                    return Err(failure("SSH control diagnostics exceeded 64 KiB."));
                }
            }
            Err(e)
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e.into()),
        }
        if exit.is_none() {
            exit = child.0.try_wait()?;
        }
        if Instant::now() >= deadline {
            return Err(failure(
                "SSH control command timed out. Inspect the tunnel status.",
            ));
        }
        thread::sleep(Duration::from_millis(10));
    }
    Ok(ControlResult {
        status: exit.unwrap(),
        stderr: String::from_utf8_lossy(&bytes).into_owned(),
    })
}
#[derive(PartialEq, Eq)]
enum Probe {
    Alive,
    Gone,
    Unknown,
}

struct StateLock(File);
impl Drop for StateLock {
    fn drop(&mut self) {
        // Explicitly unlock: a concurrent fork can briefly inherit this open file description.
        let _ = self.0.unlock();
    }
}

pub struct Manager {
    pub config_path: Option<PathBuf>,
    pub state_dir: PathBuf,
    ssh: String,
}
impl Manager {
    pub fn new(config_path: Option<PathBuf>, state_dir: Option<PathBuf>) -> Result<Self> {
        let state_dir = match state_dir {
            Some(path) => absolute(path)?,
            None => absolute(
                PathBuf::from(std::env::var_os("HOME").ok_or_else(|| failure("HOME is not set."))?)
                    .join(".tunnel-rs"),
            )?,
        };
        let config_path = config_path.map(absolute).transpose()?;
        if state_dir.to_str().is_none()
            || config_path.as_ref().is_some_and(|p| p.to_str().is_none())
        {
            return Err(failure(
                "State and SSH configuration paths must be valid UTF-8.",
            ));
        }
        if !Path::new("/usr/bin/ssh").is_file() {
            return Err(failure("System OpenSSH was not found at /usr/bin/ssh."));
        }
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&state_dir)?;
        let info = fs::symlink_metadata(&state_dir)?;
        if !info.is_dir() || info.uid() != unsafe { libc::getuid() } || info.mode() & 0o077 != 0 {
            return Err(failure(format!(
                "State directory must be owned by you, private (0700), and not a symlink: {}",
                state_dir.display()
            )));
        }
        if state_dir
            .join("000000000000.sock")
            .as_os_str()
            .as_bytes()
            .len()
            >= 100
        {
            return Err(failure(
                "State path is too long for SSH control sockets. Choose a shorter --state-dir.",
            ));
        }
        Ok(Self {
            config_path,
            state_dir,
            ssh: "/usr/bin/ssh".into(),
        })
    }
    fn path(&self, id: &str, suffix: &str) -> Result<PathBuf> {
        if id.len() != 12
            || !id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(failure("Invalid tunnel ID."));
        }
        Ok(self.state_dir.join(format!("{id}.{suffix}")))
    }
    fn open(&self, path: &Path, write: bool, create: bool, exclusive: bool) -> io::Result<File> {
        let file = OpenOptions::new()
            .read(true)
            .write(write)
            .create(create)
            .create_new(exclusive)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)?;
        let info = file.metadata()?;
        if !info.is_file()
            || info.uid() != unsafe { libc::getuid() }
            || info.mode() & 0o077 != 0
            || info.nlink() != 1
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                format!(
                    "Unsafe state file (expected a private regular file): {}",
                    path.display()
                ),
            ));
        }
        Ok(file)
    }
    fn locked(&self) -> Result<StateLock> {
        let file = self.open(&self.state_dir.join("lock"), true, true, false)?;
        file.try_lock().map_err(|e| {
            failure(format!(
                "Cannot lock state; another tunnel-rs command may be changing it: {e}"
            ))
        })?;
        Ok(StateLock(file))
    }
    fn read(&self) -> Result<Vec<Record>> {
        let file = match self.open(&self.state_dir.join("registry.json"), false, false, false) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
            Err(e) => return Err(e.into()),
        };
        let read = || -> Result<Vec<Record>> {
            let registry: Registry = serde_json::from_reader(file)?;
            if registry.version != 1 || registry.implementation != "rust" {
                return Err(failure(
                    "unsupported registry; Python and TypeScript state cannot be managed here",
                ));
            }
            let mut ids = HashSet::new();
            for record in &registry.tunnels {
                self.path(&record.id, "sock")?;
                if !ids.insert(&record.id) {
                    return Err(failure("duplicate tunnel ID"));
                }
                record.settings.validate()?;
                if ![
                    "Connecting",
                    "Running",
                    "Stopped",
                    "Disconnected",
                    "Failed",
                    "Unknown",
                ]
                .contains(&record.status.as_str())
                    || record.commands.is_empty()
                    || record.commands.iter().any(Vec::is_empty)
                {
                    return Err(failure("invalid record"));
                }
            }
            Ok(registry.tunnels)
        };
        read().map_err(|e| failure(format!("Cannot read registry; preserved it unchanged: {e}")))
    }
    fn save(&self, records: &[Record]) -> Result<()> {
        let pending = self.state_dir.join("registry.pending");
        let mut file = self.open(&pending, true, true, false)?;
        file.set_len(0)?;
        serde_json::to_writer_pretty(
            &mut file,
            &Registry {
                implementation: "rust".into(),
                version: 1,
                tunnels: records.to_vec(),
            },
        )?;
        file.flush()?;
        file.sync_all()?;
        fs::rename(&pending, self.state_dir.join("registry.json"))?;
        File::open(&self.state_dir)?.sync_all()?;
        Ok(())
    }
    fn control_args(&self, record: &Record, operation: &str) -> Result<Vec<String>> {
        // Management never re-evaluates user config or opens a replacement connection.
        Ok(vec![
            self.ssh.clone(),
            "-F".into(),
            "/dev/null".into(),
            "-S".into(),
            self.path(&record.id, "sock")?
                .to_string_lossy()
                .into_owned(),
            "-O".into(),
            operation.into(),
            record.settings.alias.clone(),
        ])
    }
    fn probe(&self, record: &Record) -> Probe {
        let Ok(path) = self.path(&record.id, "sock") else {
            return Probe::Unknown;
        };
        let info = match fs::symlink_metadata(&path) {
            Ok(info) => info,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Probe::Gone,
            Err(_) => return Probe::Unknown,
        };
        if !info.file_type().is_socket() || info.uid() != unsafe { libc::getuid() } {
            return Probe::Unknown;
        }
        match self
            .control_args(record, "check")
            .and_then(|args| run(&args, Duration::from_secs(5)))
        {
            Ok(result) if result.status.success() => Probe::Alive,
            Ok(_) => socket_gone(&path),
            Err(_) => Probe::Unknown,
        }
    }
    pub fn records(&self) -> Result<Vec<Record>> {
        let mut records = self.read()?;
        for record in &mut records {
            record.status = match self.probe(record) {
                Probe::Unknown => "Unknown".into(),
                Probe::Alive if record.status == "Running" => "Running".into(),
                Probe::Alive => "Unknown".into(),
                Probe::Gone if record.status == "Stopped" || record.status == "Failed" => {
                    record.status.clone()
                }
                Probe::Gone => "Disconnected".into(),
            };
        }
        Ok(records)
    }
    pub fn commands(&self, record: &Record) -> Result<Vec<Vec<String>>> {
        let settings = &record.settings;
        settings.validate()?;
        let mut master = vec![self.ssh.clone()];
        if let Some(config) = &self.config_path {
            master.extend(["-F".into(), config.to_string_lossy().into_owned()]);
        }
        master.extend(["-n", "-N", "-T", "-M", "-S"].map(str::to_owned));
        master.push(
            self.path(&record.id, "sock")?
                .to_string_lossy()
                .into_owned(),
        );
        master.push("-E".into());
        master.push(self.path(&record.id, "log")?.to_string_lossy().into_owned());
        for option in [
            "ClearAllForwardings=yes",
            "ControlPersist=no",
            "ExitOnForwardFailure=yes",
            "PermitLocalCommand=no",
            "RemoteCommand=none",
            "ForwardAgent=no",
            "ForwardX11=no",
            "Tunnel=no",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=3",
            "LogLevel=ERROR",
            "ConnectTimeout=15",
            "ConnectionAttempts=1",
            if settings.mode == "background" {
                "ForkAfterAuthentication=yes"
            } else {
                "ForkAfterAuthentication=no"
            },
        ] {
            master.extend(["-o".into(), option.into()]);
        }
        master.push(settings.alias.clone());
        let destination = if settings.destination.contains(':') {
            format!("[{}]", settings.destination)
        } else {
            settings.destination.clone()
        };
        let mut forward = self.control_args(record, "forward")?;
        forward.pop();
        forward.extend([
            "-o".into(),
            "ClearAllForwardings=no".into(),
            "-L".into(),
            format!(
                "127.0.0.1:{}:{destination}:{}",
                settings.local_port, settings.remote_port
            ),
            settings.alias.clone(),
        ]);
        Ok(vec![master, forward])
    }
    pub fn prepare(&self, settings: Settings) -> Result<Record> {
        settings.validate()?;
        let mut record = Record {
            id: random_id()?,
            settings,
            created_at: now(),
            started_at: None,
            stopped_at: None,
            status: "Connecting".into(),
            successful: false,
            last_error: None,
            commands: vec![],
        };
        record.commands = self.commands(&record)?;
        Ok(record)
    }
    fn find(&self, records: &[Record], id: &str) -> Result<usize> {
        self.path(id, "sock")?;
        records
            .iter()
            .position(|record| record.id == id)
            .ok_or_else(|| failure("Tunnel not found. Use tunnel-rs list to see managed IDs."))
    }
    pub fn logs(&self, id: &str) -> Result<String> {
        self.find(&self.read()?, id)?;
        let empty = "No SSH diagnostics recorded. Application logs are not collected.";
        let mut file = match self.open(&self.path(id, "log")?, true, false, false) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(empty.into()),
            Err(e) => return Err(e.into()),
        };
        let size = file.metadata()?.len();
        file.seek(SeekFrom::Start(size.saturating_sub(65536)))?;
        let mut bytes = Vec::new();
        Read::by_ref(&mut file)
            .take(65536)
            .read_to_end(&mut bytes)?;
        // shortcut: SSH owns the log; trim on access, add rotation if continuous growth matters.
        if size > 65536 {
            file.seek(SeekFrom::Start(0))?;
            file.write_all(&bytes)?;
            file.set_len(bytes.len() as u64)?;
        }
        Ok(if bytes.is_empty() {
            empty.into()
        } else {
            String::from_utf8_lossy(&bytes).into_owned()
        })
    }
    fn exit(&self, record: &Record) -> Result<()> {
        match self.probe(record) {
            Probe::Gone => return Ok(()),
            Probe::Unknown => {
                return Err(failure(
                    "Cannot verify the owned control socket. No process was killed.",
                ));
            }
            Probe::Alive => {}
        }
        let result = run(&self.control_args(record, "exit")?, Duration::from_secs(5))?;
        for _ in 0..30 {
            if self.probe(record) == Probe::Gone {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(100));
        }
        Err(failure(if result.stderr.trim().is_empty() {
            "SSH shutdown could not be verified.".into()
        } else {
            result.stderr.trim().to_owned()
        }))
    }
    pub fn stop(&self, id: &str) -> Result<Record> {
        let interrupts = Interrupts::new()?;
        let _lock = self.locked()?;
        let mut records = self.read()?;
        let index = self.find(&records, id)?;
        self.exit(&records[index])?;
        records[index].status = "Stopped".into();
        records[index].stopped_at = Some(now());
        records[index].last_error = None;
        self.save(&records)?;
        if interrupts.interrupted() {
            return Err(TunnelError::Cancelled(
                "Cancelled after stopping the tunnel safely.".into(),
            ));
        }
        Ok(records[index].clone())
    }
    fn cleanup(&self, record: &Record, child: &mut Option<OwnedChild>) -> Result<()> {
        if let Some(child) = child {
            child.terminate()?;
        }
        self.exit(record)
    }
    pub fn start(
        &self,
        mut record: Record,
        on_started: impl FnOnce(&Record) -> Result<()>,
    ) -> Result<Record> {
        record.settings.validate()?;
        let interrupts = Interrupts::new()?;
        let mut child: Option<OwnedChild> = None;
        {
            let _lock = self.locked()?;
            let mut records = self.read()?;
            if !discover_hosts(self.config_path.as_deref())
                .aliases
                .contains(&record.settings.alias)
            {
                return Err(failure(
                    "The selected alias is no longer in the discovered SSH configuration.",
                ));
            }
            if records.iter().any(|item| item.id == record.id) {
                return Err(failure(
                    "This launch was already attempted. Review a fresh attempt before retrying.",
                ));
            }
            for existing in &records {
                let a = &existing.settings;
                let b = &record.settings;
                if a.alias == b.alias
                    && a.destination == b.destination
                    && a.local_port == b.local_port
                    && a.remote_port == b.remote_port
                    && self.probe(existing) == Probe::Alive
                {
                    return Err(failure(format!(
                        "This tunnel is already active: {}. Open Manage tunnels.",
                        existing.id
                    )));
                }
            }
            if record.commands != self.commands(&record)? {
                return Err(failure(
                    "Launch settings changed after review. Review again before starting.",
                ));
            }
            if !available_port(record.settings.local_port) {
                return Err(failure(format!(
                    "Port {} is unavailable. Choose another local port.",
                    record.settings.local_port
                )));
            }
            match fs::symlink_metadata(self.path(&record.id, "sock")?) {
                Ok(_) => {
                    return Err(failure(
                        "Control path already exists. Review a fresh attempt.",
                    ));
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
            interrupts.check()?;
            record.status = "Connecting".into();
            record.started_at = Some(now());
            let index = records.len();
            records.push(record.clone());
            self.save(&records)?;
            let launch = (|| -> Result<()> {
                interrupts.check()?;
                self.open(&self.path(&record.id, "log")?, true, true, true)?;
                child = Some(OwnedChild(
                    Command::new(&record.commands[0][0])
                        .args(&record.commands[0][1..])
                        .spawn()?,
                ));
                while self.probe(&record) != Probe::Alive {
                    interrupts.check()?;
                    if let Some(status) = child.as_mut().unwrap().0.try_wait()? {
                        if record.settings.mode == "background" && status.success() {
                            let mut alive = false;
                            for _ in 0..30 {
                                interrupts.check()?;
                                if self.probe(&record) == Probe::Alive {
                                    alive = true;
                                    break;
                                }
                                thread::sleep(Duration::from_millis(100));
                            }
                            if !alive {
                                return Err(failure(
                                    "SSH exited without a responsive owned control socket.",
                                ));
                            }
                            break;
                        }
                        return Err(failure(format!(
                            "SSH startup failed ({status}). {}",
                            self.logs(&record.id)?
                        )));
                    }
                    thread::sleep(Duration::from_millis(50));
                }
                interrupts.check()?;
                if record.settings.mode == "background" {
                    let deadline = Instant::now() + Duration::from_secs(5);
                    loop {
                        interrupts.check()?;
                        if let Some(status) = child.as_mut().unwrap().0.try_wait()? {
                            if !status.success() {
                                return Err(failure(format!(
                                    "SSH backgrounding failed ({status})."
                                )));
                            }
                            break;
                        }
                        if Instant::now() >= deadline {
                            return Err(failure("SSH backgrounding timed out."));
                        }
                        thread::sleep(Duration::from_millis(25));
                    }
                }
                interrupts.check()?;
                let result = run(&record.commands[1], Duration::from_secs(10))?;
                if !result.status.success() {
                    return Err(failure(if result.stderr.trim().is_empty() {
                        "SSH refused the requested forward."
                    } else {
                        result.stderr.trim()
                    }));
                }
                if self.probe(&record) != Probe::Alive {
                    return Err(failure("SSH disconnected while opening the forward."));
                }
                interrupts.check()?;
                record.status = "Running".into();
                record.successful = true;
                record.last_error = None;
                records[index] = record.clone();
                self.save(&records)
            })();
            if let Err(error) = launch {
                match self.cleanup(&record, &mut child) {
                    Ok(()) => {
                        record.status = "Failed".into();
                        record.last_error = Some(error.to_string());
                    }
                    Err(cleanup) => {
                        record.status = "Unknown".into();
                        record.last_error = Some(format!("Cleanup needs attention: {cleanup}"));
                    }
                }
                record.successful = false;
                records[index] = record.clone();
                let diagnostic = match self.save(&records) {
                    Ok(()) => record.last_error.unwrap_or_else(|| error.to_string()),
                    Err(save) => format!(
                        "Launch failed; could not save its outcome. Record {} was reserved before launch. {save}",
                        record.id
                    ),
                };
                return Err(if interrupts.interrupted() || error.is_cancelled() {
                    TunnelError::Cancelled(diagnostic)
                } else {
                    failure(diagnostic)
                });
            }
        }
        if record.settings.mode == "foreground" {
            let callback =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| on_started(&record)));
            let mut outcome = None;
            let wait = (|| -> Result<()> {
                if matches!(&callback, Ok(Ok(()))) {
                    loop {
                        if interrupts.interrupted() {
                            break;
                        }
                        outcome = child.as_mut().unwrap().0.try_wait()?;
                        if outcome.is_some() {
                            break;
                        }
                        thread::sleep(Duration::from_millis(50));
                    }
                }
                Ok(())
            })();
            let stopped = interrupts.interrupted() || outcome.is_none();
            // Release the connection before writing state, including callback and registry failures.
            self.cleanup(&record, &mut child)?;
            let _lock = self.locked()?;
            let mut records = self.read()?;
            let index = self.find(&records, &record.id)?;
            if stopped {
                records[index].status = "Stopped".into();
                records[index].stopped_at = Some(now());
                records[index].last_error = None;
            } else if records[index].status != "Stopped" {
                records[index].status = "Disconnected".into();
                records[index].last_error = Some(format!("SSH exited ({:?}).", outcome));
            }
            self.save(&records)?;
            wait?;
            match callback {
                Ok(result) => result?,
                Err(panic) => std::panic::resume_unwind(panic),
            }
            return Ok(records[index].clone());
        }
        on_started(&record)?;
        Ok(record)
    }
}

fn socket_gone(path: &Path) -> Probe {
    // Nonblocking connect avoids hanging on an unresponsive or full Unix socket backlog.
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM, 0) };
    if fd < 0 {
        return Probe::Unknown;
    }
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    let bytes = path.as_os_str().as_bytes();
    if bytes.len() >= address.sun_path.len() {
        unsafe {
            libc::close(fd);
        }
        return Probe::Unknown;
    }
    for (target, byte) in address.sun_path.iter_mut().zip(bytes) {
        *target = *byte as libc::c_char;
    }
    let result = unsafe {
        if libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) < 0 {
            libc::close(fd);
            return Probe::Unknown;
        }
        libc::connect(
            fd,
            &address as *const libc::sockaddr_un as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t,
        )
    };
    let error = io::Error::last_os_error();
    unsafe {
        libc::close(fd);
    }
    if result < 0
        && matches!(
            error.raw_os_error(),
            Some(libc::ENOENT | libc::ECONNREFUSED)
        )
    {
        Probe::Gone
    } else {
        Probe::Unknown
    }
}

#[cfg(test)]
mod tests {
    use crate::core::*;
    use std::os::unix::fs::{PermissionsExt, symlink};
    struct Fixture {
        dir: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let dir = PathBuf::from("/tmp").join(format!("trs-{}", random_id().unwrap()));
            fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
            Self { dir }
        }
        fn manager(&self) -> Manager {
            let config = self.dir.join("config");
            fs::write(&config, "Host fixture\n HostName 127.0.0.1\n").unwrap();
            Manager::new(Some(config), Some(self.dir.join("state"))).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let result = Command::new("trash").arg(&self.dir).status();
            assert!(
                result.is_ok_and(|s| s.success()),
                "could not trash test fixture"
            );
        }
    }
    fn settings() -> Settings {
        Settings {
            alias: "fixture".into(),
            remote_port: 3000,
            local_port: 41327,
            destination: "127.0.0.1".into(),
            mode: "background".into(),
        }
    }
    fn save_one(manager: &Manager) -> Record {
        let record = manager.prepare(settings()).unwrap();
        let _lock = manager.locked().unwrap();
        manager.save(std::slice::from_ref(&record)).unwrap();
        record
    }
    #[test]
    fn ports_are_decimal_and_in_range() {
        for value in [
            "0", "65536", "-1", "+1", "1\n", "1 ", "1.5", "", "１２", "000001",
        ] {
            assert!(validate_port(value).is_err(), "{value:?}");
        }
        assert_eq!(validate_port("00001").unwrap(), 1);
        assert_eq!(validate_port("65535").unwrap(), 65535);
    }
    #[test]
    fn destinations_reject_options_urls_and_control_characters() {
        for value in [
            "-x",
            "http://host",
            "host:22",
            "a\n",
            "a..b",
            "a..",
            "",
            "_host",
            "[::1]",
            "a\0b",
        ] {
            assert!(validate_destination(value).is_err(), "{value:?}");
        }
        for value in [
            "::1",
            "127.0.0.1",
            "service.internal",
            "example.com.",
            "a-b",
        ] {
            assert!(validate_destination(value).is_ok(), "{value}");
        }
        assert!(validate_destination(&"a".repeat(64)).is_err());
    }
    #[test]
    fn port_probe_reports_busy_and_invalid_ports() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(!available_port(port));
        drop(listener);
        assert!(!available_port(0));
    }
    #[test]
    fn settings_reject_invalid_modes_and_aliases() {
        let mut config = settings();
        config.mode = "daemon".into();
        assert!(config.validate().is_err());
        config.mode = "foreground".into();
        config.alias = "-F".into();
        assert!(config.validate().is_err());
        config.alias = "fixture".into();
        config.local_port = 0;
        assert!(config.validate().is_err());
    }
    #[test]
    fn prepare_is_inert_and_commands_clear_then_add_forward() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let mut settings = settings();
        settings.destination = "::1".into();
        let record = manager.prepare(settings).unwrap();
        assert_eq!(fs::read_dir(&manager.state_dir).unwrap().count(), 0);
        assert_eq!(record.id.len(), 12);
        assert_eq!(record.commands.len(), 2);
        assert!(record.commands[0].contains(&"ClearAllForwardings=yes".into()));
        assert!(record.commands[0].contains(&"PermitLocalCommand=no".into()));
        assert!(record.commands[0].contains(&"ForkAfterAuthentication=yes".into()));
        assert!(record.commands[1].contains(&"ClearAllForwardings=no".into()));
        assert!(record.commands[1].contains(&"127.0.0.1:41327:[::1]:3000".into()));
        assert_eq!(&record.commands[1][1..3], &["-F", "/dev/null"]);
        assert!(manager.records().unwrap().is_empty());
    }
    #[test]
    fn state_directory_requires_private_nonsymlink_directory() {
        let fixture = Fixture::new();
        let public = fixture.dir.join("public");
        fs::create_dir(&public).unwrap();
        fs::set_permissions(&public, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(Manager::new(None, Some(public)).is_err());
        let target = fixture.dir.join("target");
        fs::DirBuilder::new().mode(0o700).create(&target).unwrap();
        let link = fixture.dir.join("link");
        symlink(target, &link).unwrap();
        assert!(Manager::new(None, Some(link)).is_err());
    }
    #[test]
    fn lock_is_exclusive_and_released_on_drop_without_stale_lock() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let first = manager.locked().unwrap();
        assert!(manager.locked().is_err());
        drop(first);
        assert!(manager.locked().is_ok());
        assert!(manager.state_dir.join("lock").is_file());
    }
    #[test]
    fn lock_release_is_not_delayed_by_inherited_descriptor() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let first = manager.locked().unwrap();
        // A duplicated descriptor shares flock ownership just like a descriptor inherited at fork.
        let inherited = first.0.try_clone().unwrap();
        drop(first);
        assert!(manager.locked().is_ok());
        drop(inherited);
    }
    #[test]
    fn saved_registry_is_private_and_rust_scoped() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let record = save_one(&manager);
        let path = manager.state_dir.join("registry.json");
        assert_eq!(fs::metadata(&path).unwrap().mode() & 0o777, 0o600);
        let data: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(data["implementation"], "rust");
        assert_eq!(manager.records().unwrap()[0].id, record.id);
        assert_eq!(manager.records().unwrap()[0].status, "Disconnected");
    }
    #[test]
    fn corrupt_or_foreign_registry_is_preserved() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let path = manager.state_dir.join("registry.json");
        for data in [
            "{",
            r#"{"version":1,"implementation":"typescript","tunnels":[]}"#,
            r#"{"version":1,"tunnels":[]}"#,
        ] {
            let mut file = manager.open(&path, true, true, false).unwrap();
            file.set_len(0).unwrap();
            file.write_all(data.as_bytes()).unwrap();
            drop(file);
            assert!(manager.records().is_err());
            assert_eq!(fs::read_to_string(&path).unwrap(), data);
        }
    }
    #[test]
    fn invalid_saved_records_are_rejected() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let record = save_one(&manager);
        for mutation in 0..4 {
            let mut bad = record.clone();
            match mutation {
                0 => bad.id = "../outside".into(),
                1 => bad.status = "bogus".into(),
                2 => bad.commands.clear(),
                _ => bad.settings.local_port = 0,
            }
            manager.save(&[bad]).unwrap();
            assert!(manager.records().is_err());
        }
        manager.save(&[record.clone(), record]).unwrap();
        assert!(manager.records().is_err());
    }
    #[test]
    fn symlink_and_hardlink_state_files_are_rejected() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let victim = fixture.dir.join("victim");
        fs::write(&victim, "keep").unwrap();
        let link = manager.state_dir.join("registry.json");
        symlink(&victim, &link).unwrap();
        assert!(manager.records().is_err());
        assert_eq!(fs::read_to_string(&victim).unwrap(), "keep");
        let hard = manager.state_dir.join("registry.pending");
        fs::hard_link(&victim, &hard).unwrap();
        fs::set_permissions(&victim, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(manager.save(&[]).is_err());
        assert_eq!(fs::read_to_string(&victim).unwrap(), "keep");
    }
    #[test]
    fn foreign_regular_control_path_is_unknown_and_never_removed() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let record = save_one(&manager);
        let path = manager.path(&record.id, "sock").unwrap();
        fs::write(&path, "keep").unwrap();
        assert_eq!(manager.records().unwrap()[0].status, "Unknown");
        assert!(manager.stop(&record.id).is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), "keep");
    }
    #[test]
    fn stale_unix_socket_is_disconnected() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let record = save_one(&manager);
        let listener =
            std::os::unix::net::UnixListener::bind(manager.path(&record.id, "sock").unwrap())
                .unwrap();
        drop(listener);
        assert_eq!(manager.records().unwrap()[0].status, "Disconnected");
        assert_eq!(manager.stop(&record.id).unwrap().status, "Stopped");
    }
    #[test]
    fn logs_keep_only_last_64_kib_and_require_known_id() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let record = save_one(&manager);
        assert!(
            manager
                .logs(&record.id)
                .unwrap()
                .starts_with("No SSH diagnostics")
        );
        let path = manager.path(&record.id, "log").unwrap();
        let mut file = manager.open(&path, true, true, false).unwrap();
        file.write_all(&vec![b'a'; 70000]).unwrap();
        file.write_all(b"end").unwrap();
        drop(file);
        let result = manager.logs(&record.id).unwrap();
        assert_eq!(result.len(), 65536);
        assert!(result.ends_with("end"));
        assert_eq!(fs::metadata(path).unwrap().len(), 65536);
        assert!(manager.logs("ffffffffffff").is_err());
        assert!(manager.logs("../outside").is_err());
    }
    #[test]
    fn tampered_commands_and_missing_alias_fail_before_launch() {
        let fixture = Fixture::new();
        let manager = fixture.manager();
        let mut settings = settings();
        let mut record = manager.prepare(settings.clone()).unwrap();
        record.commands[0].push("-v".into());
        let result = manager.start(record, |_| panic!("must not start"));
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("changed after review")
        );
        settings.alias = "missing".into();
        let record = manager.prepare(settings).unwrap();
        assert!(
            manager
                .start(record, |_| panic!("must not start"))
                .unwrap_err()
                .to_string()
                .contains("no longer")
        );
        assert!(manager.records().unwrap().is_empty());
    }
    #[test]
    fn control_command_timeout_is_bounded() {
        let started = Instant::now();
        let result = run(
            &["/bin/sleep".into(), "10".into()],
            Duration::from_millis(100),
        );
        assert!(result.unwrap_err().to_string().contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(2));
    }
}
