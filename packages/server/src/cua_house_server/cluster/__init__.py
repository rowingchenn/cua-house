"""Multi-node cluster support for cua-house.

Master/worker topology: workers dial master over a WebSocket; master
dispatches tasks one at a time via `AssignTask`. The worker provisions
a fresh VM per task, binds the lease, and returns a `TaskBound` with
URLs that clients reach directly. On task completion the worker tears
the VM down. No long-lived VM pool, no reconciler-driven pool state.
See docs/cluster.md.
"""
