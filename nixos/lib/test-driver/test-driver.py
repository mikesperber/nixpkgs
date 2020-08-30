#! /somewhere/python3
import argparse
import atexit
import base64
import io
import logging
import os
import pathlib
import pty
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import _thread
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import ptpython.repl

CHAR_TO_KEY = {
    "A": "shift-a",
    "N": "shift-n",
    "-": "0x0C",
    "_": "shift-0x0C",
    "B": "shift-b",
    "O": "shift-o",
    "=": "0x0D",
    "+": "shift-0x0D",
    "C": "shift-c",
    "P": "shift-p",
    "[": "0x1A",
    "{": "shift-0x1A",
    "D": "shift-d",
    "Q": "shift-q",
    "]": "0x1B",
    "}": "shift-0x1B",
    "E": "shift-e",
    "R": "shift-r",
    ";": "0x27",
    ":": "shift-0x27",
    "F": "shift-f",
    "S": "shift-s",
    "'": "0x28",
    '"': "shift-0x28",
    "G": "shift-g",
    "T": "shift-t",
    "`": "0x29",
    "~": "shift-0x29",
    "H": "shift-h",
    "U": "shift-u",
    "\\": "0x2B",
    "|": "shift-0x2B",
    "I": "shift-i",
    "V": "shift-v",
    ",": "0x33",
    "<": "shift-0x33",
    "J": "shift-j",
    "W": "shift-w",
    ".": "0x34",
    ">": "shift-0x34",
    "K": "shift-k",
    "X": "shift-x",
    "/": "0x35",
    "?": "shift-0x35",
    "L": "shift-l",
    "Y": "shift-y",
    " ": "spc",
    "M": "shift-m",
    "Z": "shift-z",
    "\n": "ret",
    "!": "shift-0x02",
    "@": "shift-0x03",
    "#": "shift-0x04",
    "$": "shift-0x05",
    "%": "shift-0x06",
    "^": "shift-0x07",
    "&": "shift-0x08",
    "*": "shift-0x09",
    "(": "shift-0x0A",
    ")": "shift-0x0B",
}

# Forward reference
machines: "List[Machine]"

logging.basicConfig(format="%(message)s")
logger = logging.getLogger("test-driver")
logger.setLevel(logging.INFO)


class MachineLogAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> Tuple[str, Any]:
        return f"{self.extra['machine']}: {msg}", kwargs


def make_command(args: list) -> str:
    return " ".join(map(shlex.quote, (map(str, args))))


def create_vlan(vlan_nr: str) -> Tuple[str, str, "subprocess.Popen[bytes]", Any]:
    logger.info(f"starting VDE switch for network {vlan_nr}")
    vde_socket = tempfile.mkdtemp(
        prefix="nixos-test-vde-", suffix="-vde{}.ctl".format(vlan_nr)
    )
    pty_master, pty_slave = pty.openpty()
    vde_process = subprocess.Popen(
        ["vde_switch", "-s", vde_socket, "--dirmode", "0700"],
        bufsize=1,
        stdin=pty_slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    fd = os.fdopen(pty_master, "w")
    fd.write("version\n")
    # TODO: perl version checks if this can be read from
    # an if not, dies. we could hang here forever. Fix it.
    assert vde_process.stdout is not None
    vde_process.stdout.readline()
    if not os.path.exists(os.path.join(vde_socket, "ctl")):
        raise Exception("cannot start vde_switch")

    return (vlan_nr, vde_socket, vde_process, fd)


def retry(fn: Callable) -> None:
    """Call the given function repeatedly, with 1 second intervals,
    until it returns True or a timeout is reached.
    """

    for _ in range(900):
        if fn(False):
            return
        time.sleep(1)

    if not fn(True):
        raise Exception("action timed out")


class Machine:
    def __init__(self, args: Dict[str, Any]) -> None:
        if "name" in args:
            self.name = args["name"]
        else:
            self.name = "machine"
            cmd = args.get("startCommand", None)
            if cmd:
                match = re.search("run-(.+)-vm$", cmd)
                if match:
                    self.name = match.group(1)

        self.script = args.get("startCommand", self.create_startcommand(args))

        tmp_dir = os.environ.get("TMPDIR", tempfile.gettempdir())

        def create_dir(name: str) -> str:
            path = os.path.join(tmp_dir, name)
            os.makedirs(path, mode=0o700, exist_ok=True)
            return path

        self.state_dir = create_dir("vm-state-{}".format(self.name))
        self.shared_dir = create_dir("shared-xchg")

        self.booted = False
        self.connected = False
        self.pid: Optional[int] = None
        self.socket = None
        self.monitor: Optional[socket.socket] = None
        self.allow_reboot = args.get("allowReboot", False)
        self.logger = MachineLogAdapter(logger, extra=dict(machine=self.name))

    @staticmethod
    def create_startcommand(args: Dict[str, str]) -> str:
        net_backend = "-netdev user,id=net0"
        net_frontend = "-device virtio-net-pci,netdev=net0"

        if "netBackendArgs" in args:
            net_backend += "," + args["netBackendArgs"]

        if "netFrontendArgs" in args:
            net_frontend += "," + args["netFrontendArgs"]

        start_command = (
            "qemu-kvm -m 384 " + net_backend + " " + net_frontend + " $QEMU_OPTS "
        )

        if "hda" in args:
            hda_path = os.path.abspath(args["hda"])
            if args.get("hdaInterface", "") == "scsi":
                start_command += (
                    "-drive id=hda,file="
                    + hda_path
                    + ",werror=report,if=none "
                    + "-device scsi-hd,drive=hda "
                )
            else:
                start_command += (
                    "-drive file="
                    + hda_path
                    + ",if="
                    + args["hdaInterface"]
                    + ",werror=report "
                )

        if "cdrom" in args:
            start_command += "-cdrom " + args["cdrom"] + " "

        if "usb" in args:
            start_command += (
                "-device piix3-usb-uhci -drive "
                + "id=usbdisk,file="
                + args["usb"]
                + ",if=none,readonly "
                + "-device usb-storage,drive=usbdisk "
            )
        if "bios" in args:
            start_command += "-bios " + args["bios"] + " "

        start_command += args.get("qemuFlags", "")

        return start_command

    def is_up(self) -> bool:
        return self.booted and self.connected

    def wait_for_monitor_prompt(self) -> str:
        assert self.monitor is not None
        answer = ""
        while True:
            undecoded_answer = self.monitor.recv(1024)
            if not undecoded_answer:
                break
            answer += undecoded_answer.decode()
            if answer.endswith("(qemu) "):
                break
        return answer

    def send_monitor_command(self, command: str) -> str:
        message = ("{}\n".format(command)).encode()
        self.logger.info(f"sending monitor command: {command}")
        assert self.monitor is not None
        self.monitor.send(message)
        return self.wait_for_monitor_prompt()

    def wait_for_unit(self, unit: str, user: Optional[str] = None) -> None:
        """Wait for a systemd unit to get into "active" state.
        Throws exceptions on "failed" and "inactive" states as well as
        after timing out.
        """

        def check_active(_: Any) -> bool:
            info = self.get_unit_info(unit, user)
            state = info["ActiveState"]
            if state == "failed":
                raise Exception('unit "{}" reached state "{}"'.format(unit, state))

            if state == "inactive":
                status, jobs = self.systemctl("list-jobs --full 2>&1", user)
                if "No jobs" in jobs:
                    info = self.get_unit_info(unit, user)
                    if info["ActiveState"] == state:
                        raise Exception(
                            (
                                'unit "{}" is inactive and there ' "are no pending jobs"
                            ).format(unit)
                        )

            return state == "active"

        retry(check_active)

    def get_unit_info(self, unit: str, user: Optional[str] = None) -> Dict[str, str]:
        status, lines = self.systemctl('--no-pager show "{}"'.format(unit), user)
        if status != 0:
            raise Exception(
                'retrieving systemctl info for unit "{}" {} failed with exit code {}'.format(
                    unit, "" if user is None else 'under user "{}"'.format(user), status
                )
            )

        line_pattern = re.compile(r"^([^=]+)=(.*)$")

        def tuple_from_line(line: str) -> Tuple[str, str]:
            match = line_pattern.match(line)
            assert match is not None
            return match[1], match[2]

        return dict(
            tuple_from_line(line)
            for line in lines.split("\n")
            if line_pattern.match(line)
        )

    def systemctl(self, q: str, user: Optional[str] = None) -> Tuple[int, str]:
        if user is not None:
            q = q.replace("'", "\\'")
            return self.execute(
                (
                    "su -l {} --shell /bin/sh -c "
                    "$'XDG_RUNTIME_DIR=/run/user/`id -u` "
                    "systemctl --user {}'"
                ).format(user, q)
            )
        return self.execute("systemctl {}".format(q))

    def require_unit_state(self, unit: str, require_state: str = "active") -> None:
        self.logger.info(
            f"checking if unit ‘{unit}’ has reached state '{require_state}'"
        )
        info = self.get_unit_info(unit)
        state = info["ActiveState"]
        if state != require_state:
            raise Exception(
                "Expected unit ‘{}’ to to be in state ".format(unit)
                + "'{}' but it is in state ‘{}’".format(require_state, state)
            )

    def log(self, message: str) -> None:
        self.logger.info(message)

    def execute(self, command: str) -> Tuple[int, str]:
        self.connect()

        out_command = "( {} ); echo '|!=EOF' $?\n".format(command)
        self.shell.send(out_command.encode())

        output = ""
        status_code_pattern = re.compile(r"(.*)\|\!=EOF\s+(\d+)")

        while True:
            chunk = self.shell.recv(4096).decode(errors="ignore")
            match = status_code_pattern.match(chunk)
            if match:
                output += match[1]
                status_code = int(match[2])
                return (status_code, output)
            output += chunk

    def succeed(self, *commands: str) -> str:
        """Execute each command and check that it succeeds."""
        output = ""
        for command in commands:
            self.logger.info(f"must succeed: {command}")
            (status, out) = self.execute(command)
            if status != 0:
                self.logger.info(f"output: {out}")
                raise Exception(
                    "command `{}` failed (exit code {})".format(command, status)
                )
            output += out
        return output

    def fail(self, *commands: str) -> str:
        """Execute each command and check that it fails."""
        output = ""
        for command in commands:
            self.logger.info(f"must fail: {command}")
            (status, out) = self.execute(command)
            if status == 0:
                raise Exception("command `{}` unexpectedly succeeded".format(command))
            output += out
        return output

    def wait_until_succeeds(self, command: str) -> str:
        """Wait until a command returns success and return its output.
        Throws an exception on timeout.
        """
        output = ""

        def check_success(_: Any) -> bool:
            nonlocal output
            status, output = self.execute(command)
            return status == 0

        self.logger.info(f"waiting for success: {command}")
        retry(check_success)
        return output

    def wait_until_fails(self, command: str) -> str:
        """Wait until a command returns failure.
        Throws an exception on timeout.
        """
        output = ""

        def check_failure(_: Any) -> bool:
            nonlocal output
            status, output = self.execute(command)
            return status != 0

        self.logger.info(f"waiting for failure: {command}")
        retry(check_failure)
        return output

    def wait_for_shutdown(self) -> None:
        if not self.booted:
            return

        self.logger.info("waiting for the VM to power off")
        sys.stdout.flush()
        self.process.wait()

        self.pid = None
        self.booted = False
        self.connected = False

    def get_tty_text(self, tty: str) -> str:
        status, output = self.execute(
            "fold -w$(stty -F /dev/tty{0} size | "
            "awk '{{print $2}}') /dev/vcs{0}".format(tty)
        )
        return output

    def wait_until_tty_matches(self, tty: str, regexp: str) -> None:
        """Wait until the visible output on the chosen TTY matches regular
        expression. Throws an exception on timeout.
        """
        matcher = re.compile(regexp)

        def tty_matches(last: bool) -> bool:
            text = self.get_tty_text(tty)
            if last:
                self.logger.info(
                    f"Last chance to match /{regexp}/ on TTY{tty}, "
                    f"which currently contains: {text}"
                )
            return len(matcher.findall(text)) > 0

        self.logger.info(f"waiting for {regexp} to appear on tty {tty}")
        retry(tty_matches)

    def send_chars(self, chars: List[str]) -> None:
        self.logger.info(f"sending keys ‘{chars}‘")
        for char in chars:
            self.send_key(char)

    def wait_for_file(self, filename: str) -> None:
        """Waits until the file exists in machine's file system."""

        def check_file(_: Any) -> bool:
            status, _ = self.execute("test -e {}".format(filename))
            return status == 0

        self.logger.info(f"waiting for file ‘{filename}‘")
        retry(check_file)

    def wait_for_open_port(self, port: int) -> None:
        def port_is_open(_: Any) -> bool:
            status, _ = self.execute("nc -z localhost {}".format(port))
            return status == 0

        self.logger.info(f"waiting for TCP port {port}")
        retry(port_is_open)

    def wait_for_closed_port(self, port: int) -> None:
        def port_is_closed(_: Any) -> bool:
            status, _ = self.execute("nc -z localhost {}".format(port))
            return status != 0

        retry(port_is_closed)

    def start_job(self, jobname: str, user: Optional[str] = None) -> Tuple[int, str]:
        return self.systemctl("start {}".format(jobname), user)

    def stop_job(self, jobname: str, user: Optional[str] = None) -> Tuple[int, str]:
        return self.systemctl("stop {}".format(jobname), user)

    def wait_for_job(self, jobname: str) -> None:
        self.wait_for_unit(jobname)

    def connect(self) -> None:
        if self.connected:
            return

        self.logger.info("waiting for the VM to finish booting")
        self.start()

        tic = time.time()
        self.shell.recv(1024)
        # TODO: Timeout
        toc = time.time()

        self.logger.info("connected to guest root shell")
        self.logger.info(f"(connecting took {toc - tic:.2f} seconds)")
        self.connected = True

    def screenshot(self, filename: str) -> None:
        out_dir = os.environ.get("out", os.getcwd())
        word_pattern = re.compile(r"^\w+$")
        if word_pattern.match(filename):
            filename = os.path.join(out_dir, "{}.png".format(filename))
        tmp = "{}.ppm".format(filename)

        self.logger.info(f"making screenshot {filename}")
        self.send_monitor_command("screendump {}".format(tmp))
        ret = subprocess.run("pnmtopng {} > {}".format(tmp, filename), shell=True)
        os.unlink(tmp)
        if ret.returncode != 0:
            raise Exception("Cannot convert screenshot")

    def copy_from_host_via_shell(self, source: str, target: str) -> None:
        """Copy a file from the host into the guest by piping it over the
        shell into the destination file. Works without host-guest shared folder.
        Prefer copy_from_host for whenever possible.
        """
        with open(source, "rb") as fh:
            content_b64 = base64.b64encode(fh.read()).decode()
            self.succeed(
                f"mkdir -p $(dirname {target})",
                f"echo -n {content_b64} | base64 -d > {target}",
            )

    def copy_from_host(self, source: str, target: str) -> None:
        """Copy a file from the host into the guest via the `shared_dir` shared
        among all the VMs (using a temporary directory).
        """
        host_src = pathlib.Path(source)
        vm_target = pathlib.Path(target)
        with tempfile.TemporaryDirectory(dir=self.shared_dir) as shared_td:
            shared_temp = pathlib.Path(shared_td)
            host_intermediate = shared_temp / host_src.name
            vm_shared_temp = pathlib.Path("/tmp/shared") / shared_temp.name
            vm_intermediate = vm_shared_temp / host_src.name

            self.succeed(make_command(["mkdir", "-p", vm_shared_temp]))
            if host_src.is_dir():
                shutil.copytree(host_src, host_intermediate)
            else:
                shutil.copy(host_src, host_intermediate)
            self.succeed(make_command(["mkdir", "-p", vm_target.parent]))
            self.succeed(make_command(["cp", "-r", vm_intermediate, vm_target]))

    def copy_from_vm(self, source: str, target_dir: str = "") -> None:
        """Copy a file from the VM (specified by an in-VM source path) to a path
        relative to `$out`. The file is copied via the `shared_dir` shared among
        all the VMs (using a temporary directory).
        """
        # Compute the source, target, and intermediate shared file names
        out_dir = pathlib.Path(os.environ.get("out", os.getcwd()))
        vm_src = pathlib.Path(source)
        with tempfile.TemporaryDirectory(dir=self.shared_dir) as shared_td:
            shared_temp = pathlib.Path(shared_td)
            vm_shared_temp = pathlib.Path("/tmp/shared") / shared_temp.name
            vm_intermediate = vm_shared_temp / vm_src.name
            intermediate = shared_temp / vm_src.name
            # Copy the file to the shared directory inside VM
            self.succeed(make_command(["mkdir", "-p", vm_shared_temp]))
            self.succeed(make_command(["cp", "-r", vm_src, vm_intermediate]))
            abs_target = out_dir / target_dir / vm_src.name
            abs_target.parent.mkdir(exist_ok=True, parents=True)
            # Copy the file from the shared directory outside VM
            if intermediate.is_dir():
                shutil.copytree(intermediate, abs_target)
            else:
                shutil.copy(intermediate, abs_target)

    def dump_tty_contents(self, tty: str) -> None:
        """Debugging: Dump the contents of the TTY<n>
        """
        self.execute("fold -w 80 /dev/vcs{} | systemd-cat".format(tty))

    def get_screen_text(self) -> str:
        if shutil.which("tesseract") is None:
            raise Exception("get_screen_text used but enableOCR is false")

        magick_args = (
            "-filter Catrom -density 72 -resample 300 "
            + "-contrast -normalize -despeckle -type grayscale "
            + "-sharpen 1 -posterize 3 -negate -gamma 100 "
            + "-blur 1x65535"
        )

        tess_args = "-c debug_file=/dev/null --psm 11 --oem 2"

        self.logger.info("performing optical character recognition")
        with tempfile.NamedTemporaryFile() as tmpin:
            self.send_monitor_command("screendump {}".format(tmpin.name))

            cmd = "convert {} {} tiff:- | tesseract - - {}".format(
                magick_args, tmpin.name, tess_args
            )
            ret = subprocess.run(cmd, shell=True, capture_output=True)
            if ret.returncode != 0:
                raise Exception("OCR failed with exit code {}".format(ret.returncode))

            return ret.stdout.decode("utf-8")

    def wait_for_text(self, regex: str) -> None:
        def screen_matches(last: bool) -> bool:
            text = self.get_screen_text()
            matches = re.search(regex, text) is not None

            if last and not matches:
                self.logger.info(f"Last OCR attempt failed. Text was: {text}")

            return matches

        self.logger.info(f"waiting for {regex} to appear on screen")
        retry(screen_matches)

    def wait_for_console_text(self, regex: str) -> None:
        self.logger.info(f"waiting for {regex} to appear on console")
        # Buffer the console output, this is needed
        # to match multiline regexes.
        console = io.StringIO()
        while True:
            try:
                console.write(self.last_lines.get())
            except queue.Empty:
                self.sleep(1)
                continue
            console.seek(0)
            matches = re.search(regex, console.read())
            if matches is not None:
                return

    def send_key(self, key: str) -> None:
        key = CHAR_TO_KEY.get(key, key)
        self.send_monitor_command("sendkey {}".format(key))

    def start(self) -> None:
        if self.booted:
            return

        self.logger.info("starting vm")

        def create_socket(path: str) -> socket.socket:
            if os.path.exists(path):
                os.unlink(path)
            s = socket.socket(family=socket.AF_UNIX, type=socket.SOCK_STREAM)
            s.bind(path)
            s.listen(1)
            return s

        monitor_path = os.path.join(self.state_dir, "monitor")
        self.monitor_socket = create_socket(monitor_path)

        shell_path = os.path.join(self.state_dir, "shell")
        self.shell_socket = create_socket(shell_path)

        qemu_options = (
            " ".join(
                [
                    "" if self.allow_reboot else "-no-reboot",
                    "-monitor unix:{}".format(monitor_path),
                    "-chardev socket,id=shell,path={}".format(shell_path),
                    "-device virtio-serial",
                    "-device virtconsole,chardev=shell",
                    "-device virtio-rng-pci",
                    "-serial stdio" if "DISPLAY" in os.environ else "-nographic",
                ]
            )
            + " "
            + os.environ.get("QEMU_OPTS", "")
        )

        environment = dict(os.environ)
        environment.update(
            {
                "TMPDIR": self.state_dir,
                "SHARED_DIR": self.shared_dir,
                "USE_TMPDIR": "1",
                "QEMU_OPTS": qemu_options,
            }
        )

        self.process = subprocess.Popen(
            self.script,
            bufsize=1,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            cwd=self.state_dir,
            env=environment,
        )
        self.monitor, _ = self.monitor_socket.accept()
        self.shell, _ = self.shell_socket.accept()

        # Store last serial console lines for use
        # of wait_for_console_text
        self.last_lines: queue.Queue = queue.Queue()

        def process_serial_output() -> None:
            assert self.process.stdout is not None
            for _line in self.process.stdout:
                # Ignore undecodable bytes that may occur in boot menus
                line = _line.decode(errors="ignore").replace("\r", "").rstrip()
                self.last_lines.put(line)
                self.logger.info(line)

        _thread.start_new_thread(process_serial_output, ())

        self.wait_for_monitor_prompt()

        self.pid = self.process.pid
        self.booted = True

        self.logger.info(f"QEMU running (pid {self.pid})")

    def cleanup_statedir(self) -> None:
        self.logger.info("delete the VM state directory")
        if os.path.isfile(self.state_dir):
            shutil.rmtree(self.state_dir)

    def shutdown(self) -> None:
        if not self.booted:
            return

        self.shell.send("poweroff\n".encode())
        self.wait_for_shutdown()

    def crash(self) -> None:
        if not self.booted:
            return

        self.logger.info("forced crash")
        self.send_monitor_command("quit")
        self.wait_for_shutdown()

    def wait_for_x(self) -> None:
        """Wait until it is possible to connect to the X server.  Note that
        testing the existence of /tmp/.X11-unix/X0 is insufficient.
        """

        def check_x(_: Any) -> bool:
            cmd = (
                "journalctl -b SYSLOG_IDENTIFIER=systemd | "
                + 'grep "Reached target Current graphical"'
            )
            status, _ = self.execute(cmd)
            if status != 0:
                return False
            status, _ = self.execute("[ -e /tmp/.X11-unix/X0 ]")
            return status == 0

        self.logger.info("waiting for the X11 server")
        retry(check_x)

    def get_window_names(self) -> List[str]:
        return self.succeed(
            r"xwininfo -root -tree | sed 's/.*0x[0-9a-f]* \"\([^\"]*\)\".*/\1/; t; d'"
        ).splitlines()

    def wait_for_window(self, regexp: str) -> None:
        pattern = re.compile(regexp)

        def window_is_visible(last_try: bool) -> bool:
            names = self.get_window_names()
            if last_try:
                self.logger.info(
                    f"Last chance to match {regexp} on the window list, "
                    + f"which currently contains: {', '.join(names)}"
                )
            return any(pattern.search(name) for name in names)

        self.logger.info("Waiting for a window to appear")
        retry(window_is_visible)

    def sleep(self, secs: int) -> None:
        # We want to sleep in *guest* time, not *host* time.
        self.succeed(f"sleep {secs}")

    def forward_port(self, host_port: int = 8080, guest_port: int = 80) -> None:
        """Forward a TCP port on the host to a TCP port on the guest.
        Useful during interactive testing.
        """
        self.send_monitor_command(
            "hostfwd_add tcp::{}-:{}".format(host_port, guest_port)
        )

    def block(self) -> None:
        """Make the machine unreachable by shutting down eth1 (the multicast
        interface used to talk to the other VMs).  We keep eth0 up so that
        the test driver can continue to talk to the machine.
        """
        self.send_monitor_command("set_link virtio-net-pci.1 off")

    def unblock(self) -> None:
        """Make the machine reachable.
        """
        self.send_monitor_command("set_link virtio-net-pci.1 on")


def create_machine(args: Dict[str, Any]) -> Machine:
    global log
    args["redirectSerial"] = os.environ.get("USE_SERIAL", "0") == "1"
    return Machine(args)


def start_all() -> None:
    global machines
    logger.info("starting all VMs")
    for machine in machines:
        machine.start()


def join_all() -> None:
    global machines
    logger.info("waiting for all VMs to finish")
    for machine in machines:
        machine.wait_for_shutdown()


def test_script() -> None:
    exec(os.environ["testScript"])


def run_tests() -> None:
    global machines
    tests = os.environ.get("tests", None)
    if tests is not None:
        logger.info("running the VM test script")
        try:
            exec(tests, globals())
        except Exception:
            logging.exception("error:")
            sys.exit(1)
    else:
        ptpython.repl.embed(locals(), globals())

    # TODO: Collect coverage data

    for machine in machines:
        if machine.is_up():
            machine.execute("sync")


@contextmanager
def subtest(name: str) -> Iterator[None]:
    logger.info(name)
    try:
        yield
        return True
    except Exception as e:
        logger.info(f'Test "{name}" failed with error: "{e}"')
        raise e

    return False


def main() -> None:
    global machines
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument(
        "-K",
        "--keep-vm-state",
        help="re-use a VM state coming from a previous run",
        action="store_true",
    )
    (cli_args, vm_scripts) = arg_parser.parse_known_args()

    vlan_nrs = list(dict.fromkeys(os.environ.get("VLANS", "").split()))
    vde_sockets = [create_vlan(v) for v in vlan_nrs]
    for nr, vde_socket, _, _ in vde_sockets:
        os.environ["QEMU_VDE_SOCKET_{}".format(nr)] = vde_socket

    machines = [create_machine({"startCommand": s}) for s in vm_scripts]
    for machine in machines:
        if not cli_args.keep_vm_state:
            machine.cleanup_statedir()
    machine_eval = [
        "global {0}; {0} = machines[{1}]".format(m.name, idx)
        for idx, m in enumerate(machines)
    ]
    exec("\n".join(machine_eval))

    @atexit.register
    def clean_up() -> None:
        logger.info("cleaning up")
        for machine in machines:
            if machine.pid is None:
                continue
            logger.info(f"killing {machine.name} (pid {machine.pid})")
            machine.process.kill()
        for _, _, process, _ in vde_sockets:
            process.terminate()

    tic = time.time()
    run_tests()
    toc = time.time()
    print("test script finished in {:.2f}s".format(toc - tic))


if __name__ == "__main__":
    main()
