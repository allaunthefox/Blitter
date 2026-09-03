"""HTTP gateway with query routing and compute passthrough."""
from __future__ import annotations

import json
import os
import socketserver
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from p2p.designation import Designation
from p2p.gossip import GossipMesh
from p2p.lease import LeaseState
from p2p.routing import WorkSpec, best_route

DEFAULT_PORT = 8790
GOSSIP_PORT = 8792


class Gateway:
    def __init__(self, node_id: str = "p2p-node", gossip_peers: list = None):
        self.mesh = GossipMesh(node_id)
        self.lease_state = LeaseState()
        self.gossip_peers = gossip_peers or []
        self.server: ThreadingHTTPServer | None = None
        self.gossip_sock: socketserver.UDPServer | None = None
        self._gossip_thread: threading.Thread | None = None

    def start(self, port: int = DEFAULT_PORT) -> None:
        self._start_http(port)
        self._start_gossip()

    def _start_http(self, port: int) -> None:
        handler_class = _make_handler_class(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler_class)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _start_gossip(self) -> None:
        self.gossip_sock = socketserver.UDPServer(("0.0.0.0", GOSSIP_PORT), _GossipUDPHandler)
        self.gossip_sock.handle_request = self._handle_gossip_packet
        self._gossip_thread = threading.Thread(target=self.gossip_sock.serve_forever, daemon=True)
        self._gossip_thread.start()

    def _handle_gossip_packet(self, request: bytes, client_address) -> None:
        try:
            raw = request.decode("utf-8").strip()
            if not raw:
                return
            payload = self.mesh.deserialize(raw)
            now = time.time_ns() // 1_000_000
            ptype = payload.get("type")
            if ptype in ("advertise", "heartbeat"):
                self.mesh.ingest_advertise(payload, now)
            elif ptype == "lease_advertise":
                self.mesh.ingest_lease_advertise(payload, now)
            elif ptype == "query":
                self._handle_remote_query(payload, client_address)
        except Exception:
            pass

    def _handle_remote_query(self, payload: dict, client_address) -> None:
        requires = frozenset(payload.get("requires", []))
        requires_control = frozenset(payload.get("requires_control", []))
        acceptable_surfaces = frozenset(payload.get("acceptable_surfaces", []))
        qos_tier = payload.get("qos_tier", "default")
        max_results = payload.get("max_results", 1)
        query_id = payload.get("query_id", str(uuid.uuid4()))
        peers = self.mesh.routable_peers()
        work = WorkSpec(
            goal_id=payload.get("goal_id", "unknown"),
            work_id=payload.get("work_id", "unknown"),
            requires=requires,
            requires_control=requires_control,
            acceptable_surfaces=acceptable_surfaces,
            qos_tier=qos_tier,
        )
        candidates = best_route(work, peers)
        response = {
            "type": "response", "schema": "mathpunch.p2p.gossip.v0",
            "query_id": query_id,
            "nodes": [
                {
                    "node_id": c.node_id,
                    "score": round(c.score, 2),
                    "proximity": {"rtt_ms": c.proximity_rtt_ms, "hop_count": c.proximity_hop_count},
                    "load": {"active_compute": c.load_active, "queued_compute": c.load_queued},
                }
                for c in (candidates or [])[:max_results]
            ],
        }
        # send response back to requester
        import socket as _socket
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            sock.sendto(json.dumps(response).encode("utf-8"), client_address)
        finally:
            sock.close()

    def add_peer(self, peer_designation: Designation) -> None:
        self.mesh.peers[peer_designation.node_id] = type("PeerEntry", (), {"designation": peer_designation, "stale": False, "retired": False})()

    def get_status(self) -> dict:
        return {"gateway": "ok", "peers": len(self.mesh.peers), "lease": self.lease_state.status()}

    def query(self, work: WorkSpec, scope: str = "local") -> list:
        peers = self.mesh.routable_peers()
        result = best_route(work, peers)
        return [result] if result else []

    def compute(self, job: dict, *, holder: str = "worker", ttl_ms: int = 30000, expected_ms: int | None = None) -> dict:
        lease = self.lease_state.acquire(holder=holder, ttl_ms=ttl_ms, expected_ms=expected_ms)
        return lease

    def passthrough(self, job: dict, path: list, ttl: int, lease: dict, query_id: str) -> dict:
        if ttl <= 0:
            return {"ok": False, "error": "ttl_exceeded", "query_id": query_id}
        return {
            "ok": True, "query_id": query_id, "path": path,
            "lease": lease, "executor_node_id": path[-1] if path else self.mesh.node_id,
        }

    def shutdown(self) -> None:
        if self.server:
            self.server.shutdown()
        if self.gossip_sock:
            self.gossip_sock.shutdown()


def _make_handler_class(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, status: int, value: dict):
            data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send_json(200, {"ok": True, "gateway": "ok"})
            elif self.path == "/status":
                self._send_json(200, gateway.get_status())
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            if self.path == "/query":
                content_len = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(content_len))
                work = WorkSpec(
                    goal_id=body.get("goal_id", "g"), work_id=body.get("work_id", "w"),
                    requires=frozenset(body.get("requires", [])),
                    requires_control=frozenset(body.get("requires_control", [])),
                    acceptable_surfaces=frozenset(body.get("acceptable_surfaces", [])),
                    preference_weights=body.get("preference_weights", {}),
                    qos_tier=body.get("qos_tier", "default"),
                )
                candidates = gateway.query(work, body.get("scope", "local"))
                self._send_json(200, {
                    "ok": True,
                    "candidates": [
                        {"node_id": c.node_id, "score": round(c.score, 2),
                         "load": {"active": c.load_active, "queued": c.load_queued}}
                        for c in candidates
                    ] if candidates else [],
                })
            elif self.path == "/compute":
                content_len = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(content_len))
                result = gateway.compute(
                    body.get("job", {}),
                    holder=body.get("holder", "worker"),
                    ttl_ms=body.get("ttl_ms", 30000),
                    expected_ms=body.get("expected_ms"),
                )
                self._send_json(200, result)
            elif self.path == "/passthrough":
                content_len = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(content_len))
                result = gateway.passthrough(
                    body.get("job", {}),
                    path=body.get("path", []),
                    ttl=body.get("ttl", 10),
                    lease=body.get("lease", {}),
                    query_id=body.get("query_id", str(uuid.uuid4())),
                )
                self._send_json(200, result)
            else:
                self._send_json(404, {"ok": False, "error": "not_found"})

    return Handler


class _GossipUDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        pass

    def forward(self, gateway, data, client_address):
        pass
