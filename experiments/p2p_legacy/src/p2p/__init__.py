"""P2P Blitter Fabric — gossip mesh, QoS-tiered routing, compute passthrough."""
from p2p.designation import (
    Designation, Load, Proximity, SpawnEnvelope,
    envelope_from_mapping, spawn_compatible, surface_capability_tags,
    ACCESSIBILITY_RANK, CONTROLLED_SUBSYSTEMS, EXECUTION_SURFACES, DEFAULT_CAPABILITIES,
)
from p2p.gossip import GossipMesh, annotate_received, MIN_SANE_UNIX_MS
from p2p.lease import LeaseState, LeaseProtocolError, LeaseIdentity
from p2p.routing import WorkSpec, best_route, route, RouteCandidate, QoS_WEIGHTS
from p2p.gateway import Gateway
