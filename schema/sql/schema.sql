-- PostgreSQL schema for simplified Dropbox-like service
-- Handles users, devices, folders, files, versions, blocks, and sharing

-- Extensions required (pgcrypto for gen_random_uuid)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    display_name TEXT,
    password_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Devices (per-user client devices)
CREATE TABLE devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT,
    last_seen TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Folders (hierarchical)
CREATE TABLE folders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES folders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Files
CREATE TABLE files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    folder_id UUID REFERENCES folders(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    size BIGINT NOT NULL DEFAULT 0,
    current_version_id UUID,
    deleted BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Blocks (deduplicated content-addressed blocks)
CREATE TABLE blocks (
    hash TEXT PRIMARY KEY, -- hex-encoded SHA-256
    size INT NOT NULL,
    s3_key TEXT NOT NULL, -- location in S3 (or mocked fs)
    created_at TIMESTAMPTZ DEFAULT now(),
    ref_count BIGINT DEFAULT 0,
    CONSTRAINT blocks_ref_count_non_negative CHECK (ref_count >= 0)
);

-- File versioning
CREATE TABLE file_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    version_number BIGINT NOT NULL DEFAULT 1,
    created_by_device UUID REFERENCES devices(id),
    created_by_user UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    size BIGINT NOT NULL DEFAULT 0
);

-- Many-to-many: which blocks compose a file version (ordered)
CREATE TABLE file_version_blocks (
    file_version_id UUID NOT NULL REFERENCES file_versions(id) ON DELETE CASCADE,
    block_hash TEXT NOT NULL REFERENCES blocks(hash) ON DELETE RESTRICT,
    block_index INT NOT NULL,
    block_size INT NOT NULL,
    PRIMARY KEY (file_version_id, block_index)
);

-- Device cursors track the latest known version per (device, file)
CREATE TABLE device_cursors (
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    file_id UUID NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    last_seen_version UUID REFERENCES file_versions(id),
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (device_id, file_id)
);

-- Shared folders: many-to-many with permission scopes
CREATE TABLE folder_shares (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    folder_id UUID NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    grantee_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK (permission IN ('read','write','owner')),
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (folder_id, grantee_user_id)
);

-- Shareable links
CREATE TABLE share_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    folder_id UUID NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    permission TEXT NOT NULL CHECK (permission IN ('read','write')),
    expires_at TIMESTAMPTZ,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes for performance
CREATE INDEX idx_files_folder ON files(folder_id);
CREATE INDEX idx_file_versions_file ON file_versions(file_id);
CREATE INDEX idx_blocks_refcount ON blocks(ref_count);
CREATE INDEX idx_device_cursors_device ON device_cursors(device_id);

-- Note: Application-level logic should increment/decrement blocks.ref_count
-- when file versions are created/deleted to allow safe GC of unused blocks.

-- Additional production indexes
CREATE INDEX idx_files_deleted         ON files(deleted) WHERE deleted = FALSE;
CREATE INDEX idx_files_folder_active   ON files(folder_id) WHERE deleted = FALSE;
CREATE INDEX idx_fvb_version_hash      ON file_version_blocks(file_version_id, block_hash);
CREATE INDEX idx_folder_shares_grantee ON folder_shares(grantee_user_id);
CREATE INDEX idx_share_links_expires   ON share_links(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_device_cursors_file   ON device_cursors(file_id);
