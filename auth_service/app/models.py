"""SQL statements and table definitions used by the auth service.

Tables (see sql/init.sql):
    users            (id TEXT PRIMARY KEY, username TEXT, role_id INTEGER REFERENCES roles(id))
    roles            (id SERIAL PRIMARY KEY, name TEXT UNIQUE)
    role_permissions (role_id INTEGER REFERENCES roles(id), permission TEXT,
                      PRIMARY KEY (role_id, permission))
"""

from __future__ import annotations

USERS_TABLE = "users"
ROLES_TABLE = "roles"
ROLE_PERMISSIONS_TABLE = "role_permissions"

# Explicit index required by the benchmark (users.id is already the primary key).
CREATE_INDEX_ROLE_PERMISSIONS_ROLE_ID = (
    "CREATE INDEX IF NOT EXISTS idx_role_permissions_role_id "
    f"ON {ROLE_PERMISSIONS_TABLE} (role_id)"
)

SELECT_ROLE_ID_BY_USER = f"SELECT role_id FROM {USERS_TABLE} WHERE id = $1"
SELECT_ROLE_ID_BY_NAME = f"SELECT id FROM {ROLES_TABLE} WHERE name = $1"
SELECT_PERMISSIONS_BY_ROLE = (
    f"SELECT permission FROM {ROLE_PERMISSIONS_TABLE} WHERE role_id = $1"
)
UPDATE_USER_ROLE = f"UPDATE {USERS_TABLE} SET role_id = $1 WHERE id = $2"
