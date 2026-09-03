"""Gossip mesh: peer table, advertisement propagation, timing diagnostics."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from p2p.designation import Designation, Load, Proximity, SpawnEnvelope, envelope_from_mapping


@dataclass
class PeerEntry:
    designation: Designation
    received_unix_ms: int = 0
    apparent_skew_ms: float = 0.0
    sequence_gap: int = 0
    duplicate: bool = False
    reordered: bool = False
    sender_clock_invalid: bool = False
    receiver_clock_invalid: bool = False
    time_anomaly: bool = False
    stale: bool = False
    retired: bool = False

    previous_seq: Optional[int] = None


MIN_SANE_UNIX_MS = 946_684_800_000  # 2000-01-01T00:00:00Z


def annotate_received(payload: dict, received_unix_ms: int, previous_seq: Optional[int]) -> dict:
    seq = payload["heartbeat_seq"]
    gap = 0
    reordered = False
    duplicate = False
    if previous_seq is not None:
        if seq > previous_seq:
            gap = max(0, seq - previous_seq - 1)
        elif seq == previous_seq:
            duplicate = True
        else:
            reordered = True

    sent_unix_ms = payload["sent_unix_ms"]
    apparent_skew_ms = received_unix_ms - sent_unix_ms
    sender_clock_invalid = sent_unix_ms < MIN_SANE_UNIX_MS
    receiver_clock_invalid = received_unix_ms < MIN_SANE_UNIX_MS
    if sender_clock_invalid and receiver_clock_invalid:
        time_anomaly_reason = "sender_and_receiver_clock_before_2000"
    elif sender_clock_invalid:
        time_anomaly_reason = "sender_clock_before_2000"
    elif receiver_clock_invalid:
        time_anomaly_reason = "receiver_clock_before_2000"
    else:
        time_anomaly_reason = None

    availability_expired = received_unix_ms > payload["availability_expires_unix_ms"]
    retire_at = payload.get("retire_at_unix_ms")
    retired = retire_at is not None and received_unix_ms >= retire_at

    enriched = dict(payload)
    enriched.update({
        "received_unix_ms": received_unix_ms,
        "apparent_skew_ms": apparent_skew_ms,
        "sender_clock_invalid": sender_clock_invalid,
        "receiver_clock_invalid": receiver_clock_invalid,
        "time_anomaly": sender_clock_invalid or receiver_clock_invalid,
        "time_anomaly_reason": time_anomaly_reason,
        "availability_expired": availability_expired,
        "retired": retired,
        "available_by_announcement": payload.get("status_ok") is True and not availability_expired and not retired,
        "sequence_gap": gap,
        "duplicate": duplicate,
        "reordered": reordered,
    })
    return enriched


class GossipMesh:
    """Local peer table and gossip propagation logic."""

    def __init__(self, node_id: str, ttl_ms: int = 3500):
        self.node_id = node_id
        self.ttl_ms = ttl_ms
        self.peers: Dict[str, PeerEntry] = {}
        self.received_at: Dict[str, float] = {}

    def ingest_advertise(self, payload: dict, received_unix_ms: float) -> PeerEntry:
        enriched = annotate_received(payload, received_unix_ms, self.peers.get(payload["node_id"]).designation.heartbeat_seq if payload["node_id"] in self.peers else None)
        nid = enriched["node_id"]
        if nid in self.peers:
            existing = self.peers[nid].designation.heartbeat_seq
            if enriched["heartbeat_seq"] <= existing:
                if enriched["heartbeat_seq"] == existing:
                    enriched["duplicate"] = True
                else:
                    enriched["reordered"] = True
                return self.peers[nid]

        designation = self._payload_to_designation(enriched)
        self.peers[nid] = PeerEntry(designation=designation)
        self._refresh_stale()
        return self.peers[nid]

    def ingest_heartbeat(self, payload: dict, received_unix_ms: float) -> PeerEntry:
        return self.ingest_advertise(payload, received_unix_ms)

    def ingest_lease_advertise(self, payload: dict, received_unix_ms: float) -> PeerEntry:
        entry = self.ingest_advertise(payload, received_unix_ms)
        entry.designation = self._update_lease(entry.designation, payload)
        return entry

    def _payload_to_designation(self, enriched: dict) -> Designation:
        hardware = enriched.get("hardware") or {}
        return Designation(
            node_id=enriched["node_id"],
            instance_id=enriched.get("instance_id", ""),
            capabilities=frozenset(enriched.get("slot_capabilities", [])),
            controlled_subsystems=frozenset(enriched.get("controlled_subsystems", [])),
            execution_surfaces=frozenset(enriched.get("execution_surfaces", [])),
            accessibility_profile=enriched.get("accessibility_profile", "unknown"),
            load=Load(
                active_compute=enriched.get("active_compute", 0),
                queued_compute=enriched.get("queued_compute", 0),
                max_concurrent_compute=enriched.get("max_concurrent_compute", 1),
            ),
            proximity=Proximity(
                rtt_ms=enriched.get("apparent_skew_ms", 0.0),
                hop_count=enriched.get("sequence_gap", 0),
            ),
            lifecycle=enriched.get("lifecycle", "persistent"),
            ephemeral=enriched.get("ephemeral", False),
            availability_ttl_ms=enriched.get("availability_ttl_ms", 3500),
            availability_expires_unix_ms=enriched.get("availability_expires_unix_ms", 0),
            retire_at_unix_ms=enriched.get("retire_at_unix_ms"),
            sent_unix_ms=enriched["sent_unix_ms"],
            heartbeat_seq=enriched["heartbeat_seq"],
            lease_epoch=enriched.get("lease_epoch", 0),
            lease_holder=enriched.get("lease_holder", ""),
            lease_active=enriched.get("lease_active", False),
            lease_expired=enriched.get("lease_expired", False),
            in_flight=enriched.get("in_flight", False),
            hardware=hardware.get("cpu_model", ""),
        )

    def _update_lease(self, d: Designation, payload: dict) -> Designation:
        return Designation(
            **{**d.__dict__,
               "lease_epoch": payload.get("lease_epoch", d.lease_epoch),
               "lease_holder": payload.get("lease_holder", d.lease_holder),
               "lease_active": payload.get("lease_active", d.lease_active),
               "lease_expired": payload.get("lease_expired", d.lease_expired),
               "in_flight": payload.get("in_flight", d.in_flight),
               "heartbeat_seq": payload.get("heartbeat_seq", d.heartbeat_seq),
               "availability_expires_unix_ms": payload.get("availability_expires_unix_ms", d.availability_expires_unix_ms),
               "sent_unix_ms": payload.get("sent_unix_ms", d.sent_unix_ms),
               }
        )

    def _refresh_stale(self) -> None:
        now = time.time_ns() // 1_000_000
        for nid, entry in list(self.peers.items()):
            d = entry.designation
            if now > d.availability_expires_unix_ms:
                entry.stale = True
            else:
                entry.stale = False
            retire_at = d.retire_at_unix_ms
            if retire_at is not None and now >= retire_at:
                entry.retired = True
            else:
                entry.retired = False

    def get_peer(self, node_id: str) -> Optional[PeerEntry]:
        return self.peers.get(node_id)

    def routable_peers(self) -> List[PeerEntry]:
        self._refresh_stale()
        return [e for e in self.peers.values() if not e.stale and not e.retired]

    def serialize_advertise(self) -> dict:
        d = Designation(
            node_id=self.node_id,
            instance_id=f"{self.node_id}:{int(time.time_ns()//1_000_000)}:0",
            availability_expires_unix_ms=(time.time_ns() // 1_000_000) + self.ttl_ms,
        )
        return {
            "type": "advertise",
            "schema": "mathpunch.p2p.gossip.v0",
            "node_id": d.node_id,
            "instance_id": d.instance_id,
            "capabilities": list(d.capabilities),
            "controlled_subsystems": list(d.controlled_subsystems),
            "execution_surfaces": list(d.execution_surfaces),
            "accessibility_profile": d.accessibility_profile,
            "spawn_semantics_version": 1,
            "load": {"active_compute": d.load.active_compute, "queued_compute": d.load.queued_compute, "max_concurrent_compute": d.load.max_concurrent_compute},
            "proximity": {"rtt_ms": d.proximity.rtt_ms, "hop_count": d.proximity.hop_count},
            "lifecycle": d.lifecycle,
            "ephemeral": d.ephemeral,
            "availability_ttl_ms": d.availability_ttl_ms,
            "availability_expires_unix_ms": d.availability_expires_unix_ms,
            "sent_unix_ms": d.sent_unix_ms,
            "heartbeat_seq": d.heartbeat_seq,
        }

    def serialize_lease_advertise(self, lease_epoch: int = 0, lease_holder: str = "", lease_active: bool = False, lease_expired: bool = False, in_flight: bool = False) -> dict:
        return {
            "type": "lease_advertise",
            "schema": "mathpunch.p2p.gossip.v0",
            "node_id": self.node_id,
            "lease_epoch": lease_epoch,
            "lease_holder": lease_holder,
            "lease_active": lease_active,
            "lease_expired": lease_expired,
            "in_flight": in_flight,
            "sent_unix_ms": time.time_ns() // 1_000_000,
            "heartbeat_seq": 0,
            "availability_expires_unix_ms": (time.time_ns() // 1_000_000) + self.ttl_ms,
        }

    def deserialize(self, raw: str) -> dict:
        return json.loads(raw.strip())
