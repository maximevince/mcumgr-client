// Copyright © 2023-2024 Vouch.io LLC
// SPDX-License-Identifier: Apache-2.0
//
// MCUmgr SMP transport over USB HID vendor reports.
//
// Each HID report carries a 1-byte length prefix followed by SMP data:
//   [ReportID:1] [Length:1] [SMP data:62 (zero-padded)]
// Fragments raw SMP packets into length-prefixed HID OUT reports (report ID 3)
// and reassembles HID IN reports (report ID 4) into SMP responses.

use anyhow::Error;
use hidapi::{HidApi, HidDevice};
use log::{debug, info, warn};
use num::FromPrimitive;
use serde_cbor::Value;

use crate::nmp_hdr::{NmpGroup, NmpHdr, NmpOp};
use crate::transfer::Transport;

const SMP_HDR_SIZE: usize = 8;
const REPORT_ID_OUT: u8 = 3;
const REPORT_ID_IN: u8 = 4;
const HID_REPORT_SIZE: usize = 64; // Full USB HID report
const FRAME_HDR_SIZE: usize = 1;   // Length byte at start of each report
const PAYLOAD_SIZE: usize = HID_REPORT_SIZE - 1 - FRAME_HDR_SIZE; // 62 bytes (SMP data per report)

/// Connection specs for USB HID transport.
#[derive(Debug, Clone)]
pub struct HidSpecs {
    pub vid: u16,
    pub pid: u16,
    pub timeout_ms: u32,
    pub mtu: usize,
}

impl Default for HidSpecs {
    fn default() -> Self {
        Self {
            vid: 0x0000,
            pid: 0x0000,
            timeout_ms: 5000,
            mtu: 2048,
        }
    }
}

/// SMP transport over USB HID.
pub struct HidTransport {
    device: HidDevice,
    specs: HidSpecs,
    seq: u8,
}

impl HidTransport {
    /// Open a HID device by VID/PID.
    pub fn new(specs: &HidSpecs) -> Result<Self, Error> {
        let api = HidApi::new().map_err(|e| {
            anyhow::anyhow!("Failed to init HID API: {e}")
        })?;

        let device = api.open(specs.vid, specs.pid).map_err(|e| {
            anyhow::anyhow!(
                "Failed to open HID device {:04x}:{:04x}: {e}",
                specs.vid, specs.pid
            )
        })?;

        device
            .set_blocking_mode(true)
            .map_err(|e| {
                anyhow::anyhow!("Failed to set blocking mode: {e}")
            })?;

        info!(
            "Opened HID device {:04x}:{:04x}",
            specs.vid, specs.pid
        );

        Ok(Self {
            device,
            specs: specs.clone(),
            seq: 0,
        })
    }

    fn next_seq(&mut self) -> u8 {
        let seq = self.seq;
        self.seq = self.seq.wrapping_add(1);
        seq
    }

    /// Build an 8-byte SMP header.
    fn build_header(&mut self, op: NmpOp, group: u16, id: u8, body_len: u16) -> [u8; SMP_HDR_SIZE] {
        let seq = self.next_seq();
        let mut hdr = [0u8; SMP_HDR_SIZE];
        hdr[0] = op as u8;              // op
        hdr[1] = 0;                     // flags
        hdr[2] = (body_len >> 8) as u8; // len (big-endian)
        hdr[3] = body_len as u8;
        hdr[4] = (group >> 8) as u8;    // group (big-endian)
        hdr[5] = group as u8;
        hdr[6] = seq;                   // seq
        hdr[7] = id;                    // id
        hdr
    }

    /// Parse an SMP header from raw bytes.
    fn parse_header(data: &[u8]) -> Result<NmpHdr, Error> {
        if data.len() < SMP_HDR_SIZE {
            return Err(anyhow::anyhow!(
                "SMP header too short: {} bytes", data.len()
            ));
        }
        Ok(NmpHdr {
            op: NmpOp::from_u8(data[0])
                .ok_or_else(|| anyhow::anyhow!("Invalid op: {}", data[0]))?,
            flags: data[1],
            len: u16::from_be_bytes([data[2], data[3]]),
            group: NmpGroup::from_u16(u16::from_be_bytes([data[4], data[5]]))
                .ok_or_else(|| anyhow::anyhow!("Unknown group: {}", u16::from_be_bytes([data[4], data[5]])))?,
            seq: data[6],
            id: data[7],
        })
    }

    /// Fragment and send raw SMP bytes as length-prefixed HID OUT reports (ID 3).
    fn send_smp(&self, data: &[u8]) -> Result<(), Error> {
        let mut offset = 0;

        while offset < data.len() {
            let chunk_end = std::cmp::min(offset + PAYLOAD_SIZE, data.len());
            let chunk = &data[offset..chunk_end];

            // Build report: [report_id, length, smp_data..., zero_padding...]
            let mut report = vec![REPORT_ID_OUT, chunk.len() as u8];
            report.extend_from_slice(chunk);
            report.resize(HID_REPORT_SIZE, 0); // zero-pad to full report size

            let written = self.device.write(&report).map_err(|e| {
                anyhow::anyhow!("HID write failed: {e}")
            })?;

            debug!("TX: offset={}, chunk={}, written={}", offset, chunk.len(), written);
            offset = chunk_end;
        }

        Ok(())
    }

    /// Reassemble length-prefixed HID IN reports (ID 4) into a complete SMP response.
    fn recv_smp(&self) -> Result<Vec<u8>, Error> {
        let mut buf = Vec::new();
        let mut expected_total: Option<usize> = None;
        let timeout_ms = self.specs.timeout_ms as i32;

        loop {
            let mut read_buf = [0u8; HID_REPORT_SIZE];
            let n = self.device.read_timeout(&mut read_buf, timeout_ms).map_err(|e| {
                anyhow::anyhow!("HID read failed: {e}")
            })?;

            if n == 0 {
                return Err(anyhow::anyhow!(
                    "HID read timeout ({}ms), received {}/{} bytes",
                    self.specs.timeout_ms,
                    buf.len(),
                    expected_total.map_or("?".to_string(), |t| t.to_string()),
                ));
            }

            // Linux hidraw: read() may or may not include report ID.
            // If first byte is our IN report ID, strip it.
            let report_data = if n > 0 && read_buf[0] == REPORT_ID_IN {
                &read_buf[1..n]
            } else {
                &read_buf[..n]
            };

            if report_data.len() < FRAME_HDR_SIZE {
                continue;
            }

            let smp_len = report_data[0] as usize;
            if smp_len == 0 || smp_len > PAYLOAD_SIZE {
                warn!("RX invalid length byte: {}", smp_len);
                continue;
            }

            let data_end = std::cmp::min(FRAME_HDR_SIZE + smp_len, report_data.len());
            buf.extend_from_slice(&report_data[FRAME_HDR_SIZE..data_end]);

            // Parse SMP header once we have enough bytes
            if expected_total.is_none() && buf.len() >= SMP_HDR_SIZE {
                let hdr = Self::parse_header(&buf)?;
                expected_total = Some(SMP_HDR_SIZE + hdr.len as usize);
                debug!(
                    "RX SMP header: op={:?} group={:?} id={} len={} (total={})",
                    hdr.op, hdr.group, hdr.id, hdr.len,
                    expected_total.unwrap()
                );
            }

            if let Some(total) = expected_total {
                if buf.len() >= total {
                    buf.truncate(total);
                    debug!("RX complete: {} bytes", buf.len());
                    return Ok(buf);
                }
            }
        }
    }
}

impl Transport for HidTransport {
    fn transceive(
        &mut self,
        op: NmpOp,
        group: NmpGroup,
        id: u8,
        body: &[u8],
    ) -> Result<(NmpHdr, Value), Error> {
        let hdr = self.build_header(op, group as u16, id, body.len() as u16);

        // Assemble full SMP packet: header + CBOR body
        let mut packet = Vec::with_capacity(SMP_HDR_SIZE + body.len());
        packet.extend_from_slice(&hdr);
        packet.extend_from_slice(body);

        // Send and receive
        self.send_smp(&packet)?;
        let response = self.recv_smp()?;

        // Parse response
        let resp_hdr = Self::parse_header(&response)?;
        let cbor_data = &response[SMP_HDR_SIZE..SMP_HDR_SIZE + resp_hdr.len as usize];
        let resp_body: Value = serde_cbor::from_slice(cbor_data).map_err(|e| {
            anyhow::anyhow!("CBOR decode failed: {e}")
        })?;

        Ok((resp_hdr, resp_body))
    }

    fn set_timeout(&mut self, timeout_ms: u32) -> Result<(), Error> {
        self.specs.timeout_ms = timeout_ms;
        Ok(())
    }

    fn mtu(&self) -> usize {
        self.specs.mtu
    }

    fn linelength(&self) -> usize {
        // Not meaningful for HID — return MTU
        self.specs.mtu
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_header() {
        let raw: [u8; 8] = [1, 0, 0, 10, 0, 1, 42, 0];
        let hdr = HidTransport::parse_header(&raw).unwrap();
        assert_eq!(hdr.op, NmpOp::ReadRsp);
        assert_eq!(hdr.len, 10);
        assert_eq!(hdr.group, NmpGroup::Image);
        assert_eq!(hdr.seq, 42);
        assert_eq!(hdr.id, 0);
    }

    #[test]
    fn test_default_specs() {
        let specs = HidSpecs::default();
        assert_eq!(specs.vid, 0x0000);
        assert_eq!(specs.pid, 0x0000);
        assert_eq!(specs.timeout_ms, 5000);
    }

    #[test]
    fn test_header_roundtrip() {
        // Build a header manually and verify parse_header can read it
        let mut hdr = [0u8; SMP_HDR_SIZE];
        hdr[0] = NmpOp::Write as u8;       // op: Write
        hdr[1] = 0;                          // flags
        hdr[2] = 0;                          // len high
        hdr[3] = 3;                          // len low = 3
        hdr[4] = 0;                          // group high
        hdr[5] = NmpGroup::Image as u8;      // group low = 1
        hdr[6] = 7;                          // seq
        hdr[7] = 1;                          // id

        let parsed = HidTransport::parse_header(&hdr).unwrap();
        assert_eq!(parsed.op, NmpOp::Write);
        assert_eq!(parsed.len, 3);
        assert_eq!(parsed.group, NmpGroup::Image);
        assert_eq!(parsed.seq, 7);
        assert_eq!(parsed.id, 1);
    }
}
