"""Multi-node cluster support for cua-house.

Master/worker topology: workers dial into master over a WebSocket and pull
task assignments. Master owns the global image catalog + desired pool state
and drives workers via PoolOp messages. See docs/cluster.md (TBD).
"""
