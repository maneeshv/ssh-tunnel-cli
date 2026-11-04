"""Native SSH lifecycle and private, durable ownership records."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import time
from typing import Callable
import uuid

from tunnel_cli.config import discover_hosts, is_alias


class TunnelError(Exception):
    """An actionable failure, suitable for displaying without a traceback."""


def validate_port(value: str | int) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,5}", str(value)):
        raise ValueError("Enter a decimal port from 1 to 65535.")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("Enter a decimal port from 1 to 65535.")
    return port


def validate_destination(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Destination must be a hostname or IP address string.")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if len(value) > 253 or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
            for part in value.rstrip(".").split(".")
        ):
            raise ValueError("Enter a hostname or IP address, without a port or URL.") from None
        return value


def available_port(port: int) -> bool:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", validate_port(port)))
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class Settings:
    alias: str
    remote_port: int
    local_port: int
    destination: str = "127.0.0.1"
    mode: str = "background"

    def __post_init__(self):
        if not isinstance(self.alias, str) or not is_alias(self.alias):
            raise ValueError("Choose a literal SSH-config alias.")
        validate_port(self.remote_port)
        validate_port(self.local_port)
        if type(self.remote_port) is not int or type(self.local_port) is not int:
            raise ValueError("Stored ports must be integers.")
        validate_destination(self.destination)
        if self.mode not in ("background", "foreground"):
            raise ValueError("Run mode must be background or foreground.")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _interrupt(*_):
    raise KeyboardInterrupt


class Manager:
    def __init__(self, config_path: Path | None = None, state_dir: Path | None = None):
        self.config_path = Path(config_path).expanduser().absolute() if config_path else None
        self.state_dir = Path(state_dir or Path.home() / ".tunnel").expanduser().absolute()
        self.ssh = "/usr/bin/ssh"
        if not Path(self.ssh).is_file():
            raise TunnelError("System OpenSSH was not found at /usr/bin/ssh.")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TunnelError(f"State directory must be owned by you, private (0700), and not a symlink: {self.state_dir}")
        if len(os.fsencode(self.state_dir / ('0' * 12 + '.sock'))) >= 100:
            raise TunnelError("State path is too long for SSH control sockets. Choose a shorter --state-dir.")

    def _path(self, tunnel_id: str, suffix: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", tunnel_id):
            raise TunnelError("Invalid tunnel ID.")
        return self.state_dir / f"{tunnel_id}.{suffix}"

    def _open(self, path: Path, flags: int):
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            os.close(fd)
            raise TunnelError(f"Unsafe state file (expected a private regular file): {path}")
        return fd

    @contextmanager
    def _locked(self):
        fd = self._open(self.state_dir / "lock", os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TunnelError("Another tunnel command is changing state. Try again when it finishes.") from None
            yield
        finally:
            os.close(fd)

    def _read(self) -> list[dict]:
        try:
            fd = self._open(self.state_dir / "registry.json", os.O_RDONLY)
        except FileNotFoundError:
            return []
        try:
            with os.fdopen(fd) as file:
                data = json.load(file)
            if data["version"] != 1 or not isinstance(data["tunnels"], list):
                raise ValueError("unsupported schema")
            ids = set()
            for record in data["tunnels"]:
                self._path(record["id"], "sock")
                if record["id"] in ids:
                    raise ValueError("duplicate tunnel ID")
                ids.add(record["id"])
                Settings(**record["settings"])
                if not isinstance(record["created_at"], str):
                    raise ValueError("invalid creation time")
                if record.get("last_error") is not None and not isinstance(record["last_error"], str):
                    raise ValueError("invalid diagnostic")
                if record["status"] not in ("Connecting", "Running", "Stopped", "Disconnected", "Failed", "Unknown"):
                    raise ValueError("unknown status")
                if not isinstance(record["successful"], bool) or not isinstance(record["commands"], list):
                    raise ValueError("invalid record")
                if not record["commands"] or any(
                    not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command)
                    for command in record["commands"]
                ):
                    raise ValueError("invalid saved command")
            return data["tunnels"]
        except (ValueError, TypeError, KeyError, TunnelError) as exc:
            raise TunnelError(f"Cannot read registry; preserved it unchanged: {exc}") from exc

    def _save(self, records: list[dict]):
        pending = self.state_dir / "registry.pending"
        fd = self._open(pending, os.O_WRONLY | os.O_CREAT)
        with os.fdopen(fd, "w") as file:
            file.truncate(0)
            json.dump({"version": 1, "tunnels": records}, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(pending, self.state_dir / "registry.json")
        directory = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _control_args(self, record: dict, operation: str) -> list[str]:
        # Management must never re-evaluate user configuration or fall back to a new connection.
        return [self.ssh, "-F", "/dev/null", "-S", str(self._path(record["id"], "sock")),
                "-O", operation, record["settings"]["alias"]]

    def _control(self, record: dict, operation: str) -> subprocess.CompletedProcess:
        return subprocess.run(self._control_args(record, operation), capture_output=True, text=True, timeout=5)

    def _probe(self, record: dict) -> str:
        path = self._path(record["id"], "sock")
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                return "Unknown"
            result = self._control(record, "check")
            if result.returncode == 0:
                return "Alive"
            # Distinguish stale/unlinked sockets from inspection failures.
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                try:
                    client.connect(str(path))
                except OSError as exc:
                    if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                        return "Gone"
            return "Unknown"
        except FileNotFoundError:
            return "Gone"
        except (OSError, subprocess.TimeoutExpired):
            return "Unknown"

    def _status(self, record: dict) -> str:
        probe = self._probe(record)
        if probe == "Unknown":
            return "Unknown"
        if probe == "Alive":
            return "Running" if record["status"] == "Running" else "Unknown"
        return record["status"] if record["status"] in ("Stopped", "Failed") else "Disconnected"

    def records(self) -> list[dict]:
        return [dict(record, status=self._status(record)) for record in self._read()]

    def commands(self, record: dict) -> list[list[str]]:
        settings = Settings(**record["settings"])
        master = [self.ssh]
        if self.config_path:
            master += ["-F", str(self.config_path)]
        master += ["-n", "-N", "-T", "-M", "-S", str(self._path(record["id"], "sock")),
                   "-E", str(self._path(record["id"], "log"))]
        for option in ("ClearAllForwardings=yes", "ControlPersist=no", "ExitOnForwardFailure=yes",
                       "PermitLocalCommand=no", "RemoteCommand=none", "ForwardAgent=no",
                       "ForwardX11=no", "Tunnel=no", "ServerAliveInterval=15", "ServerAliveCountMax=3",
                       "LogLevel=ERROR", "ConnectTimeout=15", "ConnectionAttempts=1",
                       f"ForkAfterAuthentication={'yes' if settings.mode == 'background' else 'no'}"):
            master += ["-o", option]
        master += [settings.alias]
        destination = f"[{settings.destination}]" if ":" in settings.destination else settings.destination
        forward = self._control_args(record, "forward")
        forward[-1:-1] = ["-o", "ClearAllForwardings=no", "-L",
                          f"127.0.0.1:{settings.local_port}:{destination}:{settings.remote_port}"]
        return [master, forward]

    def prepare(self, settings: Settings) -> dict:
        record = {"id": uuid.uuid4().hex[:12], "settings": asdict(settings), "created_at": now(),
                  "status": "Connecting", "successful": False, "last_error": None}
        record["commands"] = self.commands(record)
        return record

    def _find(self, records: list[dict], tunnel_id: str) -> dict:
        self._path(tunnel_id, "sock")
        for record in records:
            if record["id"] == tunnel_id:
                return record
        raise TunnelError("Tunnel not found. Use tunnel list to see managed IDs.")

    def logs(self, tunnel_id: str) -> str:
        self._find(self._read(), tunnel_id)
        path = self._path(tunnel_id, "log")
        try:
            fd = self._open(path, os.O_RDWR)
        except FileNotFoundError:
            return "No SSH diagnostics recorded. Application logs are not collected."
        with os.fdopen(fd, "r+b") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - 65536))
            output = file.read()
            # shortcut: SSH owns the log; trim on access, add rotation if continuous log growth matters.
            if size > 65536:
                file.seek(0)
                file.write(output)
                file.truncate()
        return output.decode(errors="replace") or "No SSH diagnostics recorded. Application logs are not collected."

    def _exit(self, record: dict):
        probe = self._probe(record)
        if probe == "Unknown":
            raise TunnelError("Cannot verify the owned control socket. No process was killed.")
        if probe == "Gone":
            return
        try:
            result = self._control(record, "exit")
        except subprocess.TimeoutExpired as exc:
            raise TunnelError("Timed out stopping the owned SSH connection; inspect its status.") from exc
        for _ in range(30):
            if self._probe(record) == "Gone":
                return
            time.sleep(0.1)
        raise TunnelError(result.stderr.strip() or "SSH shutdown could not be verified.")

    def stop(self, tunnel_id: str) -> dict:
        with self._locked():
            records = self._read()
            record = self._find(records, tunnel_id)
            self._exit(record)
            record.update(status="Stopped", stopped_at=now(), last_error=None)
            self._save(records)
            return dict(record)

    def _cleanup_attempt(self, record: dict, process: subprocess.Popen | None):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._exit(record)

    def start(self, record: dict, on_started: Callable[[dict], None] | None = None) -> dict:
        settings = Settings(**record["settings"])
        process = None
        previous_hup = None
        try:
            previous_hup = signal.signal(signal.SIGHUP, _interrupt)
            with self._locked():
                records = self._read()
                if settings.alias not in discover_hosts(self.config_path).aliases:
                    raise TunnelError("The selected alias is no longer in the discovered SSH configuration.")
                if any(item["id"] == record["id"] for item in records):
                    raise TunnelError("This launch was already attempted. Review a fresh attempt before retrying.")
                for existing in records:
                    route = ("alias", "destination", "local_port", "remote_port")
                    if all(existing["settings"][key] == record["settings"][key] for key in route) and self._probe(existing) == "Alive":
                        raise TunnelError(f"This tunnel is already active: {existing['id']}. Open Manage tunnels.")
                if not available_port(settings.local_port):
                    raise TunnelError(f"Port {settings.local_port} is unavailable. Choose another local port.")
                if self._path(record["id"], "sock").exists():
                    raise TunnelError("Control path already exists. Review a fresh attempt.")
                if record["commands"] != self.commands(record):
                    raise TunnelError("Launch settings changed after review. Review again before starting.")
                record = dict(record, status="Connecting", started_at=now())
                records.append(record)
                self._save(records)
                try:
                    fd = self._open(self._path(record["id"], "log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    os.close(fd)
                    process = subprocess.Popen(record["commands"][0])
                    while self._probe(record) != "Alive":
                        if process.poll() is not None:
                            # A background master may publish its socket just after its parent exits.
                            if settings.mode == "background" and process.returncode == 0:
                                for _ in range(30):
                                    if self._probe(record) == "Alive":
                                        break
                                    time.sleep(0.1)
                                else:
                                    raise TunnelError("SSH exited without a responsive owned control socket.")
                                break
                            raise TunnelError(f"SSH startup failed (exit {process.returncode}). {self.logs(record['id'])}")
                        time.sleep(0.05)
                    if settings.mode == "background":
                        process.wait(timeout=5)
                        if process.returncode:
                            raise TunnelError(f"SSH backgrounding failed (exit {process.returncode}).")
                    result = subprocess.run(record["commands"][1], capture_output=True, text=True, timeout=10)
                    if result.returncode:
                        raise TunnelError(result.stderr.strip() or "SSH refused the requested forward.")
                    if self._probe(record) != "Alive":
                        raise TunnelError("SSH disconnected while opening the forward.")
                    record.update(status="Running", successful=True, last_error=None)
                    self._save(records)
                except BaseException as exc:
                    try:
                        self._cleanup_attempt(record, process)
                        record["status"] = "Failed"
                    except (TunnelError, OSError) as cleanup_error:
                        record["status"] = "Unknown"
                        record["last_error"] = f"Cleanup needs attention: {cleanup_error}"
                    record["last_error"] = record["last_error"] or ("Cancelled during startup." if isinstance(exc, KeyboardInterrupt) else str(exc))
                    record["successful"] = False
                    try:
                        self._save(records)
                    except OSError as save_error:
                        raise TunnelError(f"Launch failed; could not save its outcome. Record {record['id']} was reserved before launch. {save_error}") from exc
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    raise TunnelError(record["last_error"]) from exc
            if settings.mode == "foreground":
                interrupted = False
                try:
                    if on_started:
                        on_started(dict(record))
                    try:
                        process.wait()
                    except KeyboardInterrupt:
                        interrupted = True
                finally:
                    stopped = interrupted or process.poll() is None
                    # Release the owned connection even if updating the registry later fails.
                    self._exit(record)
                    process.wait(timeout=5)
                    with self._locked():
                        records = self._read()
                        stored = self._find(records, record["id"])
                        if stopped:
                            stored.update(status="Stopped", stopped_at=now(), last_error=None)
                        elif stored["status"] != "Stopped":
                            stored.update(status="Disconnected", last_error=f"SSH exited ({process.returncode}).")
                        self._save(records)
                return dict(stored)
            if on_started:
                on_started(dict(record))
            return dict(record)
        finally:
            if previous_hup is not None:
                signal.signal(signal.SIGHUP, previous_hup)
