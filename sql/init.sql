-- ============================================================================
-- authz-benchmark: PostgreSQL initialization script
-- Executed automatically on first container start (postgres docker-entrypoint)
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE roles (
    id   SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE users (
    id       TEXT PRIMARY KEY,
    username TEXT,
    role_id  INTEGER REFERENCES roles(id)
);

CREATE TABLE role_permissions (
    role_id    INTEGER REFERENCES roles(id),
    permission TEXT NOT NULL,
    PRIMARY KEY (role_id, permission)
);

-- Explicit index on role_permissions.role_id (required by the benchmark)
CREATE INDEX idx_role_permissions_role_id ON role_permissions (role_id);

-- ---------------------------------------------------------------------------
-- Sample data
-- ---------------------------------------------------------------------------

INSERT INTO roles (name) VALUES ('admin'), ('editor'), ('viewer');

-- viewer: res_001..res_050 : read
INSERT INTO role_permissions (role_id, permission)
SELECT (SELECT id FROM roles WHERE name = 'viewer'),
       'res_' || LPAD(g::TEXT, 3, '0') || ':read'
FROM generate_series(1, 50) AS g;

-- editor: res_001..res_050 : read, write
INSERT INTO role_permissions (role_id, permission)
SELECT (SELECT id FROM roles WHERE name = 'editor'),
       'res_' || LPAD(g::TEXT, 3, '0') || ':' || a.action
FROM generate_series(1, 50) AS g
CROSS JOIN (VALUES ('read'), ('write')) AS a(action);

-- admin: res_001..res_050 : read, write, delete
INSERT INTO role_permissions (role_id, permission)
SELECT (SELECT id FROM roles WHERE name = 'admin'),
       'res_' || LPAD(g::TEXT, 3, '0') || ':' || a.action
FROM generate_series(1, 50) AS g
CROSS JOIN (VALUES ('read'), ('write'), ('delete')) AS a(action);

-- users: user_001..user_010 admin, user_011..user_040 editor, user_041..user_100 viewer
INSERT INTO users (id, username, role_id)
SELECT 'user_' || LPAD(g::TEXT, 3, '0'),
       'user_' || LPAD(g::TEXT, 3, '0'),
       (SELECT id FROM roles
        WHERE name = CASE
            WHEN g <= 10 THEN 'admin'
            WHEN g <= 40 THEN 'editor'
            ELSE 'viewer'
        END)
FROM generate_series(1, 100) AS g;
