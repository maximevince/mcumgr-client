# Copyright (c) 2026 Finalmouse LLC
# SPDX-License-Identifier: Apache-2.0
#
# MCUmgr DFU operations via mcumgr-client CLI over USB HID.

import logging
import re
import time

import pytest

from conftest import wait_for_device, wait_for_device_gone

logger = logging.getLogger(__name__)


@pytest.mark.hardware
@pytest.mark.dfu
class TestImageList:
    """MCUmgr image list (read image state)."""

    def test_image_list_returns_images(self, mcumgr):
        """Response has 'images' array with at least one entry."""
        resp = mcumgr.image_list()
        assert "images" in resp, f"No 'images' key in response: {resp}"
        assert len(resp["images"]) >= 1, "Expected at least 1 image entry"

    def test_image_list_primary_confirmed(self, mcumgr):
        """Slot 0 is active and confirmed."""
        resp = mcumgr.image_list()
        slot0 = resp["images"][0]
        assert slot0.get("active"), f"Slot 0 not active: {slot0}"
        assert slot0.get("confirmed"), f"Slot 0 not confirmed: {slot0}"

    def test_image_list_has_version(self, mcumgr):
        """Primary image has a version string in major.minor.patch format."""
        resp = mcumgr.image_list()
        version = resp["images"][0].get("version", "")
        assert re.match(r"^\d+\.\d+\.\d+", version), (
            f"Version '{version}' doesn't match major.minor.patch format"
        )


@pytest.mark.hardware
@pytest.mark.dfu
class TestEcho:
    """MCUmgr echo command."""

    def test_echo_hello(self, mcumgr):
        """Basic echo roundtrip."""
        assert mcumgr.echo("hello") == "hello"

    def test_echo_long_string(self, mcumgr):
        """Echo a longer multi-word string."""
        msg = "the quick brown fox jumps over the lazy dog"
        assert mcumgr.echo(msg) == msg


@pytest.mark.hardware
@pytest.mark.dfu
@pytest.mark.slow
class TestUpload:
    """MCUmgr image upload and reset."""

    @pytest.mark.timeout(120)
    def test_upload_image(self, mcumgr, firmware_bin_path):
        """Upload signed binary, verify slot 1 shows new image."""
        # Record pre-upload state
        before = mcumgr.image_list()
        slot0_hash_before = before["images"][0].get("hash")

        mcumgr.upload(firmware_bin_path)

        # Verify slot 1 has an image
        after = mcumgr.image_list()
        images = after["images"]
        assert len(images) >= 2, (
            f"Expected 2 image entries after upload, got {len(images)}"
        )
        slot1 = images[1]
        assert slot1.get("bootable"), f"Slot 1 not bootable: {slot1}"

    @pytest.mark.timeout(120)
    def test_upload_then_reset(self, mcumgr, vid, pid, firmware_bin_path):
        """Upload -> mark test -> reset -> device re-enumerates and swaps."""
        mcumgr.upload(firmware_bin_path)

        # Mark slot 1 for test swap
        resp = mcumgr.image_list()
        assert len(resp["images"]) >= 2, "No slot 1 image after upload"
        slot0_hash = resp["images"][0].get("hash")
        slot1_hash = resp["images"][1].get("hash")
        assert slot1_hash, "No hash for slot 1 image"

        if slot0_hash == slot1_hash:
            pytest.skip(
                "Uploaded firmware has same hash as running image — "
                "nothing to swap. Use a different firmware version."
            )

        mcumgr.test_image(slot1_hash)

        # Verify pending
        resp2 = mcumgr.image_list()
        assert resp2["images"][1].get("pending"), "Slot 1 not pending after test"

        # Reset
        mcumgr.reset()

        # Device should disappear then reappear
        gone = wait_for_device_gone(vid, pid, timeout_s=10)
        assert gone, "Device did not disconnect after reset"

        found = wait_for_device(vid, pid, timeout_s=30)
        assert found, "Device did not re-enumerate within 30s after reset"

        # Allow USB stack to settle
        time.sleep(2)

        # Verify new image is running
        resp3 = mcumgr.image_list()
        slot0 = resp3["images"][0]
        assert slot0.get("active"), "Slot 0 not active after swap"
        assert slot0.get("confirmed"), "Slot 0 not confirmed after swap"
        assert slot0.get("hash") == slot1_hash, "Slot 0 hash doesn't match uploaded image"
        logger.info("Post-swap version: %s", slot0.get("version"))
