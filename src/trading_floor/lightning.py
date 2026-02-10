from __future__ import annotations

from contextlib import contextmanager
from typing import Dict, Optional


class LightningTracer:
    """
    Minimal Agent Lightning wiring.
    - Uses OtelTracer + LightningStore (in-memory by default)
    - Emits operation spans and reward annotations
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = cfg.get("agent_lightning", {}).get("enabled", False)
        self._tracer = None
        self._store = None
        self._store_uri = cfg.get("agent_lightning", {}).get("store_uri", "")
        self._reward_keys = cfg.get("agent_lightning", {}).get("reward_keys", [])

        if not self.enabled:
            return

        try:
            from agentlightning.tracer import OtelTracer
            from agentlightning.store import InMemoryLightningStore, LightningStoreClient
        except Exception:
            # If dependency missing, disable gracefully.
            self.enabled = False
            return

        if isinstance(self._store_uri, str) and self._store_uri.startswith("http"):
            self._store = LightningStoreClient(self._store_uri)
        else:
            self._store = InMemoryLightningStore()

        self._tracer = OtelTracer()
        self._tracer.init_worker(0, store=self._store)

    @contextmanager
    def run_context(self, name: str, input_payload: Optional[Dict] = None):
        if not self.enabled or self._tracer is None or self._store is None:
            yield
            return

        # Lazily import helpers to avoid hard dependency at import time
        from agentlightning.tracer import set_active_tracer, clear_active_tracer

        rollout_id = None
        attempt_id = None
        try:
            if hasattr(self._store, "start_rollout"):
                # start_rollout is async; run it synchronously for now
                import asyncio

                rollout = asyncio.run(self._store.start_rollout(input=input_payload or {}))
                rollout_id = rollout.rollout_id
                attempt_id = rollout.attempt.attempt_id
        except Exception:
            pass

        with self._tracer.lifespan(self._store):
            set_active_tracer(self._tracer)
            try:
                # Use private sync context to keep workflow synchronous
                with self._tracer._trace_context_sync(name=name, rollout_id=rollout_id, attempt_id=attempt_id):
                    yield
            finally:
                clear_active_tracer()

    def emit_span(self, name: str, payload: Dict):
        if not self.enabled:
            return
        from agentlightning import operation

        with operation(name, attributes=payload):
            pass

    def emit_reward(self, rewards: Dict):
        if not self.enabled:
            return
        from agentlightning import emit_reward

        if not rewards:
            return
        # Choose primary key deterministically (first configured key or first dict key)
        primary_key = None
        if self._reward_keys:
            primary_key = self._reward_keys[0]
        else:
            primary_key = next(iter(rewards.keys()))
        emit_reward(rewards, primary_key=primary_key)
