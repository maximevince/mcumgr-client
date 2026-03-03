# Copyright (c) 2026 Finalmouse LLC
# SPDX-License-Identifier: Apache-2.0
#
# USB enumeration tests — verify device is visible and has correct descriptors.

import subprocess

import pytest

from conftest import FINALMOUSE_VID


@pytest.mark.hardware
class TestEnumeration:
    """USB enumeration and descriptor checks."""

    def test_device_present(self, vid, pid):
        """VID:PID visible in lsusb."""
        result = subprocess.run(
            ["lsusb", "-d", f"{vid:04x}:{pid:04x}"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, f"Device {vid:04x}:{pid:04x} not found"
        assert result.stdout.strip(), "lsusb returned empty output"

    def test_hid_interfaces(self, vid, pid):
        """At least two HID interfaces exist (boot mouse + vendor)."""
        result = subprocess.run(
            ["lsusb", "-d", f"{vid:04x}:{pid:04x}", "-v"],
            capture_output=True, text=True, timeout=5,
        )
        hid_count = result.stdout.count("bInterfaceClass")
        assert hid_count >= 2, (
            f"Expected at least 2 interfaces for {vid:04x}:{pid:04x}, "
            f"found {hid_count}"
        )

    def test_manufacturer_string(self, vid, pid):
        """Manufacturer string contains 'Finalmouse'."""
        result = subprocess.run(
            ["lsusb", "-d", f"{vid:04x}:{pid:04x}", "-v"],
            capture_output=True, text=True, timeout=5,
        )
        assert "Finalmouse" in result.stdout, (
            f"'Finalmouse' not found in USB descriptor output"
        )
