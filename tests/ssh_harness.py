"""An opt-in loopback sshd fixture; never reads the user's SSH configuration."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time


class _Echo(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request.recv(4096)
        if data:
            self.request.sendall(data)


class SSHHarness:
    def __init__(self):
        self.directory = Path(tempfile.mkdtemp(prefix="tsh-", dir="/tmp"))
        self.config = self.directory / "config"
        self.sockets = []
        self.processes = []
        self.ports = set()
        self.server = None
        self.sshd = None

    def port(self):
        while True:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            if port not in self.ports:
                self.ports.add(port)
                return port

    def __enter__(self):
        try:
            self.setup()
            return self
        except BaseException:
            self.close()
            raise

    def setup(self):
        for key in ("host", "client"):
            subprocess.run(
                ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.directory / key)],
                check=True, capture_output=True,
            )
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
        self.server.daemon_threads = True
        self.target_port = self.server.server_address[1]
        self.ports.add(self.target_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.ssh_port = self.port()
        self.inherited_port = self.port()
        self.shared_socket = self.directory / "shared"
        self.local_command_marker = self.directory / "unexpected-local-command"
        daemon_config = self.directory / "sshd_config"
        daemon_config.write_text(
            f"ListenAddress 127.0.0.1\nPort {self.ssh_port}\n"
            f"HostKey {self.directory}/host\nPidFile {self.directory}/sshd.pid\n"
            f"AuthorizedKeysFile {self.directory}/client.pub\n"
            "StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            "UsePAM no\nAllowTcpForwarding yes\nLogLevel ERROR\n"
        )
        public_key = (self.directory / "host.pub").read_text().split()
        (self.directory / "known_hosts").write_text(
            f"[127.0.0.1]:{self.ssh_port} {' '.join(public_key[:2])}\n"
        )
        self.config.write_text(
            f"Host fixture\n HostName 127.0.0.1\n Port {self.ssh_port}\n User {getpass.getuser()}\n"
            f" IdentityFile {self.directory}/client\n IdentitiesOnly yes\n IdentityAgent none\n"
            f" UserKnownHostsFile {self.directory}/known_hosts\n GlobalKnownHostsFile /dev/null\n"
            " StrictHostKeyChecking yes\n BatchMode yes\n ConnectTimeout 3\n"
            f" LocalForward 127.0.0.1:{self.inherited_port} 127.0.0.1:{self.target_port}\n"
            f" ControlMaster auto\n ControlPath {self.shared_socket}\n ControlPersist 60\n"
            f" PermitLocalCommand yes\n LocalCommand touch {self.local_command_marker}\n"
        )
        with (self.directory / "sshd.log").open("w") as log:
            self.sshd = subprocess.Popen(
                ["/usr/sbin/sshd", "-D", "-e", "-f", str(daemon_config)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.sshd.poll() is not None:
                raise RuntimeError((self.directory / "sshd.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", self.ssh_port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("isolated sshd did not begin listening")

    def master_command(self, path, background=False):
        command = [
            "/usr/bin/ssh", "-F", str(self.config), "-N", "-T", "-M", "-S", str(path),
            "-o", "ClearAllForwardings=yes", "-o", "ControlPersist=no",
            "-o", "ForkAfterAuthentication=no", "-o", "ExitOnForwardFailure=yes",
            "-o", "PermitLocalCommand=no",
        ]
        if background:
            command.append("-f")
        return command + ["fixture"]

    def launch(self, name, background=False):
        path = self.directory / name
        self.sockets.append(path)
        with (self.directory / f"{name}.log").open("w") as log:
            process = subprocess.Popen(
                self.master_command(path, background), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log,
            )
        self.processes.append(process)
        if background:
            if process.wait(timeout=10):
                raise RuntimeError((self.directory / f"{name}.log").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.control(path, "check").returncode == 0:
                return path, process
            if process.poll() is not None and not background:
                raise RuntimeError((self.directory / f"{name}.log").read_text())
            time.sleep(0.02)
        raise RuntimeError(f"master did not become ready: {path}")

    @staticmethod
    def control(path, operation, *arguments):
        return subprocess.run(
            ["/usr/bin/ssh", "-F", "/dev/null", "-S", str(path), "-O", operation,
             *arguments, "fixture"],
            capture_output=True, text=True, timeout=5,
        )

    def forward(self, path, local_port):
        return self.control(
            path, "forward", "-o", "ClearAllForwardings=no", "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{self.target_port}",
        )

    @staticmethod
    def exchange(port):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(b"owned-tunnel-round-trip")
            return client.recv(4096)

    @staticmethod
    def listening(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return True
        except OSError:
            return False

    def close(self):
        for path in reversed(self.sockets):
            try:
                self.control(path, "exit")
            except (OSError, subprocess.TimeoutExpired):
                pass
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.sshd is not None:
            if self.sshd.poll() is None:
                self.sshd.terminate()
            self.sshd.wait(timeout=5)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        # Keep failed fixtures available when explicitly requested for diagnosis.
        if not os.environ.get("TUNNEL_KEEP_SSH_FIXTURE"):
            trash = shutil.which("trash")
            if trash:
                subprocess.run([trash, str(self.directory)], check=True, capture_output=True)

    def __exit__(self, *exception):
        self.close()
