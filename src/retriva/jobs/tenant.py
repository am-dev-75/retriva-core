# Copyright (C) 2026 Retriva.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""Tenant resolution and trust model for Core ingestion (Spec 025
§3.12; ADR-030 Decision 6).

No unauthenticated caller may select an arbitrary tenant.  Trust
models (explicit deployment posture, Constitution §36):

- ``fixed`` (normal development posture): a fixed, explicit,
  server-configured tenant (``RETRIVA_JOBS_DEFAULT_TENANT``) that is
  MANDATORY when no authenticated resolver is enabled, validated at
  startup, applied server-side, never silently overridable by
  ordinary request input, and represented in logs only through safe
  configuration-mode information (mode names, never the tenant
  value).
- ``gateway_header``: a tenant header is honored ONLY when a trusted
  authenticated gateway strips any untrusted external copy of the
  header and sets it server-side; a missing header fails closed.
- development-only override (``RETRIVA_JOBS_TENANT_HEADER_OVERRIDE``):
  only with resolver ``fixed``, only for loopback/trusted clients,
  with a prominent startup warning; ignored automatically when an
  authenticated trusted resolver is active.

Repository and service layers fail closed when trusted server-side
tenant context has not been established.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from retriva.infrastructure.postgres.tenant import (
    validate_tenant_id,
)
from retriva.jobs.config import JobsSettings
from retriva.jobs.errors import TenantContextMissingError
from retriva.logger import get_logger

_log = get_logger(__name__)

RESOLVER_FIXED = "fixed"
RESOLVER_GATEWAY_HEADER = "gateway_header"

_MODE_FIXED = "fixed"
_MODE_GATEWAY_HEADER = "gateway_header"
_MODE_DEV_OVERRIDE = "dev_override"

_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1", "::1", "testclient", "localhost"})


@dataclass(frozen=True)
class TenantResolution:
    """The server-resolved tenant context (never exposes the tenant
    value in logs; ``mode`` carries safe configuration-mode info)."""

    tenant_id: str
    mode: str


class TenantResolver:
    """Startup-validated tenant resolution for request paths.

    Construction performs the startup validation (mandatory fixed
    tenant when no authenticated resolver is enabled; prominent
    warning for the constrained development override).
    """

    def __init__(self, settings: Optional[JobsSettings] = None) -> None:
        self._settings = settings or JobsSettings()
        resolver = self._settings.tenant_resolver
        if resolver not in (RESOLVER_FIXED, RESOLVER_GATEWAY_HEADER):
            raise TenantContextMissingError(
                "RETRIVA_JOBS_TENANT_RESOLVER must be 'fixed' or "
                f"'gateway_header' (got {resolver!r})")
        self._resolver = resolver
        self._override = False
        if resolver == RESOLVER_FIXED:
            if not self._settings.default_tenant:
                raise TenantContextMissingError(
                    "RETRIVA_JOBS_DEFAULT_TENANT is mandatory when no "
                    "authenticated tenant resolver is enabled "
                    "(resolver 'fixed')")
            validate_tenant_id(self._settings.default_tenant)
            if self._settings.tenant_header_override:
                # Prominent startup warning (safe mode information
                # only; never the tenant value).
                _log.warning(
                    "DURABLE-JOBS TENANT OVERRIDE ENABLED: request "
                    "header tenant override is active for the fixed "
                    "resolver, constrained to loopback/trusted "
                    "clients. This is a development-only posture and "
                    "must never be enabled where untrusted callers "
                    "can reach the ingestion API.")
                self._override = True
        else:
            if self._settings.tenant_header_override:
                _log.info(
                    "durable-jobs tenant header override ignored: an "
                    "authenticated trusted resolver (gateway_header) "
                    "is active")

    # -- request-time resolution ---------------------------------------------

    def resolve(self, client_host: Optional[str],
                header_value: Optional[str]) -> TenantResolution:
        """Resolve the server-side tenant for a request.

        Ordinary request input never silently selects a tenant: with
        the fixed resolver only a loopback client may supply the
        clearly named development override header; with
        ``gateway_header`` the trusted-gateway header is mandatory
        (absent → fail closed).
        """
        if self._resolver == RESOLVER_GATEWAY_HEADER:
            if not header_value:
                raise TenantContextMissingError(
                    "no trusted tenant context on the request "
                    "(the trusted gateway must set it; untrusted "
                    "callers cannot select tenants)")
            return TenantResolution(
                tenant_id=validate_tenant_id(header_value),
                mode=_MODE_GATEWAY_HEADER)
        # Fixed resolver: normal posture is the configured tenant.
        if (self._override and header_value
                and client_host in _LOOPBACK_HOSTS):
            return TenantResolution(
                tenant_id=validate_tenant_id(header_value),
                mode=_MODE_DEV_OVERRIDE)
        if header_value and not self._override:
            # An ordinary request input can never silently select a
            # tenant; the fixed posture ignores it entirely.
            _log.info(
                "tenant header ignored under the fixed resolver "
                "(tenant_resolution_mode=fixed)")
        return TenantResolution(
            tenant_id=validate_tenant_id(
                self._settings.default_tenant),
            mode=_MODE_FIXED)


_resolver: Optional[TenantResolver] = None


def tenant_resolver(settings: Optional[JobsSettings] = None
                    ) -> TenantResolver:
    """Process-wide resolver (constructed once; startup validation)."""
    global _resolver
    if _resolver is None:
        _resolver = TenantResolver(settings)
    return _resolver


def reset_tenant_resolver() -> None:
    """Test hook."""
    global _resolver
    _resolver = None
