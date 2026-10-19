"""No service may trust `X-Forwarded-For` from an unrestricted peer.

THE DEFECT (F1 residual R1, 2026-09-06). `app/app.py::_rate_limit_subject` was hardened
to believe `X-Forwarded-For` only from a peer named in `TRUSTED_PROXY_IPS` (empty by
default). That fix sits ABOVE uvicorn, and `docker-compose.yml` passed
`--proxy-headers --forwarded-allow-ips "*"` to both pools — so uvicorn rewrote
`scope["client"]`, and therefore Flask's `request.remote_addr`, from ANY peer's
forwarded header before app.py ever looked. Measured on an instance with the
production flags: 20 requests with rotating XFF all passed a limiter that was already
tripped; with the rewrite off, the same run trips at the 13th.

WHY A TEST AND NOT JUST THE FIX. `"*"` reads as harmless — it was shipped with a
comment arguing it was safe because the port is loopback-bound. The argument is wrong
(loopback-bound is not nginx-only), and the next person to hit a "the app sees the
wrong client IP" bug will be tempted to widen it again. This is the guard that makes
that a deliberate act.

WHAT THE HOP ACTUALLY IS. nginx runs on the host and `proxy_pass`es to
`127.0.0.1:5001`, but the published port is served by docker-proxy, which re-originates
the connection onto the compose bridge — so the container sees the BRIDGE GATEWAY, not
`127.0.0.1`. Asserting `127.0.0.1` here would encode a value that is simply wrong for
this deployment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"

# Services that run the ASGI app behind the reverse proxy.
APP_SERVICES = ("app", "app-fc")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _forwarded_allow_ips(command) -> str | None:
    """The value of `--forwarded-allow-ips` in a service command, or None."""
    if not isinstance(command, list):
        return None
    for index, token in enumerate(command):
        if token == "--forwarded-allow-ips":
            return command[index + 1] if index + 1 < len(command) else ""
        if isinstance(token, str) and token.startswith("--forwarded-allow-ips="):
            return token.split("=", 1)[1]
    return None


def test_no_service_trusts_forwarded_headers_from_every_peer():
    """The load-bearing assertion: no `*`, anywhere, in any service."""
    services = _compose()["services"]
    offenders = {
        name: _forwarded_allow_ips(spec.get("command"))
        for name, spec in services.items()
        if _forwarded_allow_ips(spec.get("command")) is not None
    }
    assert offenders, "no service declares --forwarded-allow-ips at all; did the flag move?"
    for name, value in offenders.items():
        assert "*" not in str(value), (
            f"service {name!r} passes --forwarded-allow-ips {value!r}. A wildcard lets "
            "uvicorn rewrite remote_addr from ANY peer's X-Forwarded-For, below "
            "app.py::_rate_limit_subject's TRUSTED_PROXY_IPS gate, so any process that "
            "can reach the published port picks its own rate-limit bucket. Name the "
            "single reverse-proxy hop instead."
        )


def test_both_app_services_declare_the_flag_and_keep_proxy_headers():
    services = _compose()["services"]
    for name in APP_SERVICES:
        command = services[name]["command"]
        assert "--proxy-headers" in command, (
            f"{name}: --proxy-headers was dropped; the app would stop seeing the real "
            "client IP and https scheme from nginx entirely"
        )
        value = _forwarded_allow_ips(command)
        assert value, f"{name}: --forwarded-allow-ips is missing or empty"


def test_the_two_layers_name_the_same_hop():
    """uvicorn's trusted peer and the app's `TRUSTED_PROXY_IPS` must agree.

    Two layers disagreeing about who the proxy is is how one of them silently stops
    doing anything — which is exactly how the wildcard survived review.
    """
    services = _compose()["services"]
    for name in APP_SERVICES:
        spec = services[name]
        uvicorn_hop = _forwarded_allow_ips(spec["command"])
        app_hop = (spec.get("environment") or {}).get("TRUSTED_PROXY_IPS")
        assert app_hop is not None, (
            f"{name}: TRUSTED_PROXY_IPS is not set. app.py::_trusted_proxy_ips defaults "
            "to EMPTY, so the app-layer half of the XFF fix would be dormant."
        )
        assert app_hop == uvicorn_hop, (
            f"{name}: uvicorn trusts {uvicorn_hop!r} but the app trusts {app_hop!r}; "
            "they must name the same reverse-proxy hop"
        )


def test_the_hop_is_not_loopback_because_docker_proxy_re_originates():
    """A regression guard with a reason, not a taste.

    Setting the hop to `127.0.0.1` looks obviously right — nginx does `proxy_pass
    http://127.0.0.1:5001`. It is wrong here: the port is *published*, docker-proxy
    re-originates the connection onto the compose bridge, and the container's peer is
    the bridge gateway. A `127.0.0.1` hop would match nothing, uvicorn would silently
    stop resolving the real client, and every visitor would share one rate-limit
    bucket. If this deployment ever moves to `network_mode: host` or an nginx that
    lives on the compose network, delete this test in the same change.
    """
    services = _compose()["services"]
    for name in APP_SERVICES:
        spec = services[name]
        assert "network_mode" not in spec, (
            f"{name} now sets network_mode; the hop analysis in this file no longer "
            "applies — re-derive it and update the compose comment"
        )
        hop = _forwarded_allow_ips(spec["command"])
        assert "127.0.0.1" not in hop, (
            f"{name}: the hop is {hop!r}. Traffic reaches this container through "
            "docker-proxy, so its peer is the compose bridge gateway, never 127.0.0.1."
        )
