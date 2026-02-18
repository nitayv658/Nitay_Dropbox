# Dropbox-sim: Database schema and API specs

What I added:

- SQL schema: schema/sql/schema.sql — PostgreSQL schema covering users, devices, folders, files, file_versions, blocks, and sharing tables. It includes comments about block refcounting and GC.
- Protobuf: proto/internal.proto — gRPC service definitions for BlockServer, MetadataService, SyncService, and SharingService.
- OpenAPI: openapi/metadata_openapi.yaml — REST spec for Metadata endpoints: `create_directory`, `delete_file`, `get_file_history`.

Next steps:

- If you approve the schema and API specs, I'll scaffold the Block Server implementation (Go with goroutines is recommended for concurrency). I can also generate database migration files and a minimal Docker Compose for Postgres + Redis + local S3 mock.
