"""Read-only previews and state-bound apply for explicit mod changes."""
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Callable

PREVIEW_LIFETIME_SECONDS = 600


@dataclass(frozen=True)
class ModChangePlan:
    summary: dict
    evidence: dict
    apply: Callable


class ModChangePreviews:
    def __init__(self, operations, key, builders):
        self.ops = operations
        self.key = key
        self.builders = builders

    def _build(self, action, arguments, before, issued):
        if not isinstance(action, str) or action not in self.builders:
            raise ValueError('Unsupported preview action')
        plan = self.builders[action](arguments, before)
        if self.ops.snapshot()['revision'] != before['revision']:
            raise ValueError('MO2 state changed during preview; inspect again')
        signed = {
            'action': action, 'arguments': arguments, 'revision': before['revision'],
            'summary': plan.summary, 'evidence': plan.evidence, 'issued': issued,
        }
        payload = json.dumps(signed, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
        signature = hmac.digest(self.key, payload, 'sha256').hex()
        return plan, f'{issued:016x}.{signature}'

    def preview(self, params):
        before = self.ops.snapshot()
        action = params.get('action')
        arguments = {key: value for key, value in params.items() if key != 'action'}
        plan, token = self._build(action, arguments, before, int(time.monotonic()))
        blocked = bool(self.ops.running)
        return {
            'action': action, 'arguments': arguments, 'plan_token': None if blocked else token,
            'expected_profile': before['profile'], 'expected_revision': before['revision'],
            'expires_in_seconds': None if blocked else PREVIEW_LIFETIME_SECONDS,
            'blocked_reason': 'A game or MO2-launched tool is running' if blocked else None,
            **plan.summary,
        }

    def prepare_apply(self, action, params, before):
        token = params.get('plan_token')
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{16}\.[a-f0-9]{64}', token):
            raise ValueError('A valid preview plan_token is required; preview the exact change first')
        issued = int(token.split('.')[0], 16)
        if not 0 <= int(time.monotonic()) - issued < PREVIEW_LIFETIME_SECONDS:
            raise ValueError('The preview expired; preview the exact change again')
        arguments = {key: value for key, value in params.items()
                     if key not in {'expected_profile', 'expected_revision', 'plan_token'}}
        if action == 'rename_mod' and arguments.pop('acknowledge_shared_folder', None) is not True:
            raise ValueError('Acknowledge the shared mod folder and all-profile rename before applying')
        plan, expected = self._build(action, arguments, before, issued)
        if not hmac.compare_digest(token, expected):
            raise ValueError('The preview is stale or belongs to a different change; preview again')
        if self.ops.running:
            raise ValueError('Close the game or running MO2-launched tools before changing mods')
        return plan
