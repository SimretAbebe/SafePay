import ipaddress
import socket
import urllib.parse
from typing import Callable

from django.conf import settings
from django.core.exceptions import ValidationError

# NOTE ON DNS REBINDING (TOCTOU):
# There is an unavoidable time-of-check to time-of-use (TOCTOU) window between
# validating a target URL's resolved IP address and the actual network connection
# made by the HTTP client (e.g. requests.post). A malicious actor controlling an
# authoritative DNS server could return a benign public IP during validate_webhook_url
# or is_target_safe, but resolve to an internal/private IP (such as 127.0.0.1 or
# 169.254.169.254) moments later during the actual HTTP connect call.
# To fully mitigate DNS rebinding in production deployments, outbound webhook requests
# should be dispatched through a dedicated egress proxy (e.g., Envoy or Smokescreen)
# that validates and pins the destination IP at connection time.


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    Returns True if the IP address is private, loopback, link-local, multicast,
    reserved, unspecified, or an IPv4-mapped equivalent of any of those.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _is_disallowed_ip(ip.ipv4_mapped)

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_webhook_url(url: str, resolver: Callable = None) -> None:
    if resolver is None:
        resolver = socket.getaddrinfo
    """
    Validates a webhook target URL to protect against SSRF.

    Rules:
    - Max length 500 characters.
    - Scheme must be https (http is permitted only if ALLOW_PRIVATE_WEBHOOK_TARGETS=True).
    - Hostname is required.
    - No credentials (username/password) in the URL.
    - Any port is permitted.
    - Resolves the hostname and rejects if ANY resolved address is private,
      loopback, link-local, multicast, reserved, unspecified, or an IPv4-mapped
      version of those.
    - Directly rejects IP-literal hosts within those disallowed ranges.
    - Rejects if DNS resolution fails.
    - When ALLOW_PRIVATE_WEBHOOK_TARGETS is True (dev only), permits http and
      private/loopback targets, but still requires a valid host.
    """
    if not url or not isinstance(url, str):
        raise ValidationError("URL must be a non-empty string.")
    if len(url) > 500:
        raise ValidationError("URL exceeds maximum length of 500 characters.")

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        raise ValidationError("Malformed URL.")

    allow_private = getattr(settings, "ALLOW_PRIVATE_WEBHOOK_TARGETS", False)
    allowed_schemes = ("https", "http") if allow_private else ("https",)

    if not parsed.scheme or parsed.scheme.lower() not in allowed_schemes:
        raise ValidationError("Scheme must be HTTPS (HTTP disallowed).")

    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ValidationError("Embedded credentials in URL are disallowed.")

    hostname = parsed.hostname
    if not hostname:
        raise ValidationError("Hostname is required.")

    # Check direct IP-literal host
    try:
        ip = ipaddress.ip_address(hostname)
        if not allow_private and _is_disallowed_ip(ip):
            raise ValidationError("Disallowed private, loopback, or cloud-metadata IP literal.")
    except ValueError:
        pass

    # Resolve hostname
    try:
        addrinfo = resolver(hostname, None)
    except Exception:
        raise ValidationError("Hostname resolution failed.")

    if not addrinfo:
        raise ValidationError("No DNS records found for host.")

    if not allow_private:
        for entry in addrinfo:
            sockaddr = entry[4]
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                raise ValidationError("Invalid resolved IP address.")
            if _is_disallowed_ip(ip):
                raise ValidationError("Resolves to a disallowed private, loopback, or reserved IP address.")


def is_target_safe(url: str) -> bool:
    """
    Wraps validate_webhook_url and returns True if safe, False otherwise.
    Used at delivery time before dispatching HTTP requests.
    """
    try:
        validate_webhook_url(url)
        return True
    except (ValidationError, Exception):
        return False
