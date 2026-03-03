# Copyright (c) 2026 Finalmouse LLC
# SPDX-License-Identifier: Apache-2.0
#
# Edge-case and robustness tests for mcumgr-client HID transport.

import pytest


@pytest.mark.hardware
class TestRobustness:
    """Transport-level stress tests."""

    def test_rapid_image_list(self, mcumgr):
        """10 consecutive image_list() calls all succeed."""
        for i in range(10):
            resp = mcumgr.image_list()
            assert "images" in resp, f"Call {i+1} failed: {resp}"
            assert len(resp["images"]) >= 1, f"Call {i+1}: no images"

    def test_rapid_echo(self, mcumgr):
        """20 consecutive echo calls all succeed."""
        for i in range(20):
            msg = f"ping-{i}"
            result = mcumgr.echo(msg)
            assert result == msg, f"Echo {i}: expected '{msg}', got '{result}'"
