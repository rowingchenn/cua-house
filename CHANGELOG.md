# Changelog

## [0.1.0] - 2026-04-05

### Added
- Initial extraction from agenthle env_server as standalone project
- uv monorepo with packages/common, packages/client, packages/server
- QEMU snapshot-based VM pool (savevm/loadvm for fast revert)
- QMP client for QEMU machine protocol
- Docker+QEMU local runtime with overlay management
- GCP VM runtime with boot image and data disk support
- NTFS ACL-based task data isolation (aligned across local and GCP)
- E: drive mapping via Samba share for VM pool
- FastAPI control plane with reverse proxy
- Batch/task/lease scheduling with heartbeat TTL
- Event-based observability (JSONL events.jsonl)
