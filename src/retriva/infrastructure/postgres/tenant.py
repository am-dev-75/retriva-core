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

"""Tenant context for the shared PostgreSQL platform.

Every tenant-scoped table is protected by forced Row-Level Security
policies keyed on ``app.current_tenant`` (the CRM store convention;
ADR-029).  Repositories set the context with ``SET LOCAL`` inside
their transaction — the setting lives exactly as long as the
transaction and can never leak across requests.

Fail-closed (Constitution §32): a missing or empty tenant context
refuses the operation instead of widening access.  ``SET LOCAL`` on a
custom GUC is permitted for any role; the RLS policy evaluates to
NULL (no rows) when the context is unset, so a role cannot bypass
isolation by simply not setting it.
"""

from __future__ import annotations

from typing import Optional

from psycopg2 import sql

#: The custom GUC every tenant-RLS policy keys on.
TENANT_GUC = "app.current_tenant"


class TenantContextMissing(RuntimeError):
    """No trusted server-side tenant context was provided."""


def validate_tenant_id(tenant_id: Optional[str]) -> str:
    """Validate and return the tenant identifier.

    Fail-closed: empty/missing values raise.  Bounded charset
    (lowercase identifier with dashes/underscores, max 64 chars) so
    the value is safe as a bound parameter and in bounded logs.
    """
    if tenant_id is None:
        raise TenantContextMissing(
            "tenant context is required (trusted server-side "
            "resolution must set it before repository access)")
    value = str(tenant_id).strip()
    if not value:
        raise TenantContextMissing(
            "tenant context is required (trusted server-side "
            "resolution must set it before repository access)")
    if len(value) > 64:
        raise TenantContextMissing("tenant identifier exceeds 64 chars")
    allowed = set(
        "abcdefghijklmnopqrstuvwxyz0123456789-_.")
    if any(char not in allowed for char in value):
        raise TenantContextMissing(
            "tenant identifier contains unsupported characters")
    return value


def set_tenant_context(cursor, tenant_id: Optional[str]) -> str:
    """Set ``app.current_tenant`` for the CURRENT transaction
    (``SET LOCAL``); validates and returns the tenant id.  Must be
    called inside a transaction block; the setting resets at commit/
    rollback."""
    value = validate_tenant_id(tenant_id)
    cursor.execute(
        sql.SQL("SELECT set_config({}, %s, true)").format(
            sql.Literal(TENANT_GUC)),
        (value,))
    return value


def clear_tenant_context(cursor) -> None:
    """Reset the tenant context for the current transaction."""
    cursor.execute(
        sql.SQL("SELECT set_config({}, '', true)").format(
            sql.Literal(TENANT_GUC)))
