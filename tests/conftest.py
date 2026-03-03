# Copyright (c) 2026 Finalmouse LLC
# SPDX-License-Identifier: Apache-2.0
#
# Pytest fixtures for mcumgr-client CLI integration tests.

import json
import logging
import os
import re
import subprocess
import time

import pytest

logger = logging.getLogger(__name__)

FINALMOUSE_VID = 0x361D
DONGLE_APP_PID = 0x0300

# Locate the mcumgr-client binary relative to this file
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_TESTS_DIR)
_BINARY_RELEASE = os.path.join(_PROJECT_DIR, "target", "release", "mcumgr-client")
_BINARY_DEBUG = os.path.join(_PROJECT_DIR, "target", "debug", "mcumgr-client")


def pytest_addoption(parser):
    parser.addoption("--vid", type=lambda x: int(x, 0), default=FINALMOUSE_VID,
                     help="USB Vendor ID (hex)")
    parser.addoption("--pid", type=lambda x: int(x, 0), default=DONGLE_APP_PID,
                     help="USB Product ID (hex)")
    parser.addoption("--interface", type=int, default=1,
                     help="USB HID interface number")
    parser.addoption("--report-id-out", type=lambda x: int(x, 0), default=0x03,
                     help="HID OUT report ID")
    parser.addoption("--report-id-in", type=lambda x: int(x, 0), default=0x04,
                     help="HID IN report ID")
    parser.addoption("--firmware-bin", default=None,
                     help="Path to signed firmware binary for upload tests")
    parser.addoption("--mcumgr-bin", default=None,
                     help="Path to mcumgr-client binary (auto-detected if omitted)")


def is_usb_device_present(vid, pid):
    """Check if a USB device with given VID:PID is enumerated."""
    try:
        result = subprocess.run(
            ["lsusb", "-d", f"{vid:04x}:{pid:04x}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def wait_for_device(vid, pid, timeout_s=30):
    """Wait until a USB device with given VID:PID appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_usb_device_present(vid, pid):
            return True
        time.sleep(0.5)
    return False


def wait_for_device_gone(vid, pid, timeout_s=10):
    """Wait until a USB device with given VID:PID disappears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not is_usb_device_present(vid, pid):
            return True
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def vid(request):
    return request.config.getoption("--vid")


@pytest.fixture(scope="session")
def pid(request):
    return request.config.getoption("--pid")


@pytest.fixture(scope="session")
def mcumgr_bin(request):
    """Locate the mcumgr-client binary."""
    explicit = request.config.getoption("--mcumgr-bin")
    if explicit:
        assert os.path.isfile(explicit), f"Binary not found: {explicit}"
        return explicit
    if os.path.isfile(_BINARY_RELEASE):
        return _BINARY_RELEASE
    if os.path.isfile(_BINARY_DEBUG):
        return _BINARY_DEBUG
    pytest.fail(
        "mcumgr-client binary not found. "
        "Run 'cargo build --release --features hid' or pass --mcumgr-bin"
    )


@pytest.fixture(scope="session")
def hid_args(request):
    """Common HID CLI arguments."""
    vid = request.config.getoption("--vid")
    pid = request.config.getoption("--pid")
    iface = request.config.getoption("--interface")
    rid_out = request.config.getoption("report_id_out")
    rid_in = request.config.getoption("report_id_in")
    return [
        "--hid",
        "--vid", f"0x{vid:04x}",
        "--pid", f"0x{pid:04x}",
        "--interface", str(iface),
        "--report-id-out", f"0x{rid_out:02x}",
        "--report-id-in", f"0x{rid_in:02x}",
    ]


class McuMgrCli:
    """Wrapper around the mcumgr-client binary for test assertions."""

    def __init__(self, binary, hid_args):
        self.binary = binary
        self.hid_args = hid_args

    def run(self, *cmd_args, timeout=60):
        """Run mcumgr-client with given subcommand args, return stdout."""
        full_cmd = [self.binary] + self.hid_args + list(cmd_args)
        logger.info("Running: %s", " ".join(full_cmd))
        result = subprocess.run(
            full_cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        logger.debug("stdout: %s", result.stdout)
        if result.stderr:
            logger.debug("stderr: %s", result.stderr)
        return result

    def run_ok(self, *cmd_args, timeout=60):
        """Run and assert success, return stdout."""
        result = self.run(*cmd_args, timeout=timeout)
        assert result.returncode == 0, (
            f"mcumgr-client failed (rc={result.returncode}):\n"
            f"  cmd: {result.args}\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )
        return result.stdout

    def image_list(self):
        """Run 'list' and parse the JSON response."""
        stdout = self.run_ok("list")
        # Extract JSON after "response: " line
        match = re.search(r"response:\s*(\{.*)", stdout, re.DOTALL)
        assert match, f"No JSON in list output:\n{stdout}"
        return json.loads(match.group(1))

    def echo(self, message):
        """Run 'echo' and return the echoed string."""
        stdout = self.run_ok("echo", message)
        match = re.search(r"Echo response:\s*(.*)", stdout)
        assert match, f"No echo response in output:\n{stdout}"
        return match.group(1).strip()

    def upload(self, firmware_path, timeout=120):
        """Upload a firmware binary."""
        return self.run_ok("-t", "30", "upload", firmware_path, timeout=timeout)

    def test_image(self, hash_hex):
        """Mark an image for test swap."""
        return self.run_ok("test", hash_hex)

    def reset(self):
        """Reset the device. May fail if device disconnects mid-response."""
        result = self.run("reset", timeout=10)
        # Reset often succeeds but device disconnects before we read response
        return result


@pytest.fixture(scope="function")
def mcumgr(mcumgr_bin, hid_args):
    """MCUmgr CLI client fixture."""
    return McuMgrCli(mcumgr_bin, hid_args)


@pytest.fixture(scope="session")
def firmware_bin_path(request):
    """Locate a signed firmware binary for upload tests."""
    import glob as glob_mod

    explicit = request.config.getoption("--firmware-bin")
    if explicit:
        assert os.path.isfile(explicit), f"Firmware not found: {explicit}"
        return explicit

    # Auto-discover from build directory
    base = os.path.join(
        os.path.dirname(_PROJECT_DIR),  # tools/
        "..",  # slx-fw root
        "application", "slx_dongle_app",
    )
    patterns = [
        os.path.join(base, "build", "slx_dongle_app", "zephyr", "zephyr.signed.bin"),
        os.path.join(base, "build", "zephyr", "zephyr.signed.bin"),
    ]
    for pattern in patterns:
        matches = glob_mod.glob(pattern)
        if matches:
            logger.info("Auto-discovered firmware: %s", matches[0])
            return matches[0]

    pytest.skip("No firmware binary found (use --firmware-bin or build first)")
