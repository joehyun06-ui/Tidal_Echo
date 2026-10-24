"""Opt-in request accounting; no network, credentials, prompt or memory writes.

The application must explicitly inject a collector. There is deliberately no
environment switch or production singleton. Route profiles are trusted server
configuration, matched by exact endpoint/model/protocol, never inferred by name.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .usage_ledger import LedgerError, estimate, identifier, model_name, normalize, raw_json, stable_prefix
from .usage_ledger_journal import CollectionJournal

_binding = contextvars.ContextVar('ledger_binding', default=None)
_attempt = contextvars.ContextVar('ledger_attempt', default=None)
_deadline = contextvars.ContextVar('ledger_deadline', default=None)


@dataclass(frozen=True)
class CachePolicy:
    # "automatic" observes transparent provider caching without extra fields.
    kind: str = 'provider_default'
    ttl: str | None = None

    def __post_init__(self):
        allowed = {'provider_default': {None}, 'automatic': {None},
                   'openai_legacy': {None, 'in-memory', '24h'},
                   'openai_responses_implicit': {'30m', '12h'}}
        if self.kind not in allowed or self.ttl not in allowed[self.kind]:
            raise LedgerError('ledger_cache_policy_invalid')

    @property
    def name(self):
        return self.kind + ('-' + self.ttl if self.ttl else '')


@dataclass(frozen=True)
class RouteProfile:
    endpoint: str
    model: str
    protocol: str
    provider_id: str
    dialect: str
    revision: str
    cache: CachePolicy = CachePolicy()
    price: dict | None = None

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or len(self.endpoint) > 2048):
            raise LedgerError('ledger_route_invalid')
        model_name(self.model); identifier(self.provider_id); identifier(self.revision)
        allowed = {'chat': {'openai_chat', 'deepseek_chat', 'unknown'},
                   'responses': {'openai_responses', 'unknown'}}
        if self.protocol not in allowed or self.dialect not in allowed[self.protocol]:
            raise LedgerError('ledger_route_invalid')
        if not isinstance(self.cache, CachePolicy):
            raise LedgerError('ledger_cache_policy_invalid')
        if self.cache.kind.startswith('openai_') and self.dialect not in {'openai_chat', 'openai_responses'}:
            raise LedgerError('ledger_cache_policy_invalid')
        if self.cache.kind == 'openai_responses_implicit' and self.protocol != 'responses':
            raise LedgerError('ledger_cache_policy_invalid')
        # Validate and detach the immutable price version from caller mutations.
        price = json.loads(raw_json(self.price))
        estimate(normalize(None, self.dialect), price)
        object.__setattr__(self, 'price', price)


class ApiCollector:
    def __init__(self, ledger, profiles, *, fingerprint_key):
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32:
            raise LedgerError('ledger_prefix_invalid')
        self.ledger, self._key = ledger, fingerprint_key
        self._profiles = {}
        for profile in profiles:
            if not isinstance(profile, RouteProfile): raise LedgerError('ledger_route_invalid')
            key = (profile.endpoint.rstrip('/'), profile.model, profile.protocol)
            if key in self._profiles: raise LedgerError('ledger_route_duplicate')
            # Copy configuration, not route dictionaries containing API keys.
            self._profiles[key] = RouteProfile(**{**profile.__dict__, 'price': json.loads(raw_json(profile.price))})
        self.captured = self.dropped = self.unmatched = self.configuration_errors = 0
        self.journal_failed=0
        try:self.journal=CollectionJournal(ledger,'api')
        except Exception:self.journal=None;self.journal_failed=1

    def note(self,reason,**fields):
        try:self.journal.issue(reason,**fields)
        except Exception:self.journal_failed+=1

    def health(self):
        return {'captured': self.captured, 'dropped': self.dropped, 'unmatched': self.unmatched,
                'configuration_errors': self.configuration_errors,'journal_failed':self.journal_failed, 'complete': not (self.dropped or self.unmatched or self.journal_failed)}

    def begin(self, route, protocol, sid, turn):
        profile = self._profiles.get((route.get('url', '').rstrip('/'), route.get('model'), protocol))
        if profile is None:
            self.unmatched += 1
            self.note('route_unmatched',session_id=sid)
            return None
        return Attempt(self, profile, sid, turn)


class Attempt:
    def __init__(self, collector, profile, sid, turn):
        self.collector, self.profile = collector, profile
        self.started = time.monotonic()
        self.deadline = _deadline.get()
        self.raw = None; self.invalid = False; self.http_status = None; self.error = None
        self.fingerprint = None; self.cache_policy = profile.cache.name
        self.record = {'mode': 'api', 'provider_id': profile.provider_id, 'request_id': 'api-' + uuid.uuid4().hex,
                       'session_id': sid, 'turn_id': turn, 'model': profile.model,
                       'requested_model': profile.model, 'model_source': 'request',
                       'at': datetime.now(timezone.utc).isoformat(), 'dialect': profile.dialect,
                       'price': json.loads(raw_json(profile.price)), 'config_revision': profile.revision,
                       # HTTPX automatic retries are disabled. A fallback is a separate request.
                       'retry_count': 0}
        try:collector.journal.start(self.record)
        except Exception:collector.journal_failed+=1

    def usage(self, value):
        if value is None: return
        try:
            if not isinstance(value, dict): raise LedgerError('ledger_usage_invalid')
            self.raw = json.loads(raw_json(value))
            self.invalid = False
        except (ValueError, TypeError, RecursionError):
            # Never persist a complete response/error/credential object in place of usage.
            self.raw = None; self.invalid = True
            self.collector.note('invalid_usage',session_id=self.record['session_id'],request_id=self.record['request_id'],provider_id=self.profile.provider_id,at=self.record['at'])

    def body(self, body, responses):
        try:
            messages = body.get('input' if responses else 'messages', [])
            stable = []
            for message in messages:
                if not isinstance(message, dict) or message.get('role') not in {'system', 'developer'}: break
                stable.append(message)
            instructions = json.dumps({'provider': self.profile.provider_id, 'instructions': stable},
                                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            _, self.fingerprint = stable_prefix(instructions, body.get('tools', []), self.collector._key)
            policy = self.profile.cache
            extra = {}
            if policy.kind in {'openai_legacy', 'openai_responses_implicit'}:
                extra['prompt_cache_key'] = 'guiting-' + self.fingerprint
            if policy.kind == 'openai_legacy' and policy.ttl:
                extra['prompt_cache_retention'] = policy.ttl
            elif policy.kind == 'openai_responses_implicit':
                extra['prompt_cache_options'] = {'mode': 'implicit', 'ttl': policy.ttl}
            if any(key in body and body[key] != value for key, value in extra.items()):
                raise LedgerError('ledger_cache_configuration_conflict')
            # Preserve every prompt byte, list order and existing tool schema.
            return {**body, **extra} if extra else body
        except Exception:
            self.collector.configuration_errors += 1
            self.collector.note('configuration_unapplied',session_id=self.record['session_id'],request_id=self.record['request_id'],provider_id=self.profile.provider_id,at=self.record['at'])
            self.cache_policy = 'configuration_unapplied'; self.fingerprint = None
            return body

    async def finish(self, exc):
        if exc is not None: self.transport_error(exc)
        status = 'completed'
        if exc is not None:
            if isinstance(exc, asyncio.CancelledError):
                status = 'timeout' if self.deadline is not None and time.monotonic() >= self.deadline else 'interrupted'
            else: status = 'error'
        if self.error == 'timeout' or self.http_status == 408: status = 'timeout'
        if self.http_status == 429: status = 'rate_limited'
        payload = {**self.record, 'raw_usage': self.raw, 'usage_complete': status == 'completed' and bool(self.raw) and not self.invalid,
                   'status': status, 'latency_ms': max(0, round((time.monotonic()-self.started)*1000)),
                   'http_status': self.http_status, 'prefix_fingerprint': self.fingerprint, 'cache_policy': self.cache_policy}
        try:
            inserted=await asyncio.to_thread(self.collector.ledger.append, payload)
            self.collector.captured += int(inserted)
        except Exception:
            # Disk/validation failures must not change a model result or trigger inference retry.
            self.collector.dropped += 1
            self.collector.note('write_failed',session_id=self.record['session_id'],request_id=self.record['request_id'],provider_id=self.profile.provider_id,at=self.record['at'])

    def transport_error(self, exc):
        # Helpers may replace a transport exception with a sanitized route error.
        # Inspect types only; never persist exception messages, URLs or bodies.
        for _ in range(5):
            if exc is None: break
            if isinstance(exc, TimeoutError) or any(cls.__name__ == 'TimeoutException' and cls.__module__ == 'httpx' for cls in type(exc).__mro__):
                self.error = 'timeout'; break
            exc = exc.__cause__ or exc.__context__

    def model(self, value):
        if value is None: return
        try: model_name(value)
        except LedgerError: return
        self.record.update(model=value, model_source='provider')
        # A price for the requested model cannot silently price a rerouted model.
        if value != self.profile.model: self.record['price'] = None


@contextmanager
def capture_scope(collector, sid, turn):
    if collector is not None:
        identifier(sid); identifier(turn)
    token = _binding.set((collector, sid, turn) if collector is not None else None)
    try: yield
    finally: _binding.reset(token)


@contextmanager
def request_deadline(seconds):
    token = _deadline.set(time.monotonic()+seconds if _binding.get() else None)
    try: yield
    finally: _deadline.reset(token)


def observe_api(*, protocol=None, route_index=0):
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            binding = _binding.get()
            if binding is None: return await fn(*args, **kwargs)
            collector, sid, turn = binding
            attempt = None
            try:
                route = args[route_index] if len(args) > route_index else kwargs['route']
                kind = protocol or ('responses' if kwargs.get('responses') else 'chat')
                attempt = collector.begin(route, kind, sid, turn)
            except Exception:
                collector.dropped += 1;collector.note('collector_failure',session_id=sid)
            token = _attempt.set(attempt)
            error = None
            try:
                return await fn(*args, **kwargs)
            except BaseException as exc:
                error = exc
                raise
            finally:
                _attempt.reset(token)
                if attempt is not None: await attempt.finish(error)
        return wrapped
    return decorate


def prepare_body(body, *, responses=False):
    attempt = _attempt.get()
    return attempt.body(body, responses) if attempt else body


def usage(value):
    attempt = _attempt.get()
    if attempt: attempt.usage(value)


def response_status(value):
    attempt = _attempt.get()
    if attempt and type(value) is int and 100 <= value <= 599: attempt.http_status = value


def transport_error(exc):
    attempt = _attempt.get()
    if attempt: attempt.transport_error(exc)


def reported_model(value):
    attempt = _attempt.get()
    if attempt: attempt.model(value)
