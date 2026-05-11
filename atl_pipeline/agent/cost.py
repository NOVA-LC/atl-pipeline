"""Token + dollar budget tracker for the v3 agent.

Hard caps are non-negotiable: if a planned call would breach the per-lead or
pipeline-wide cap, the tracker raises BudgetExceeded and the orchestrator
catches it, saves partial state, and exits cleanly.

Prices in USD per 1M tokens (Anthropic Jan 2026 pricing).
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# USD per 1M tokens. Cached at module import; update here on price changes.
PRICING = {
    'claude-haiku-4-5-20251001': {'input': 1.00, 'output': 5.00},
    'claude-haiku-4-5': {'input': 1.00, 'output': 5.00},
    'claude-sonnet-4-6-20251001': {'input': 3.00, 'output': 15.00},
    'claude-sonnet-4-6': {'input': 3.00, 'output': 15.00},
    'claude-opus-4-7': {'input': 15.00, 'output': 75.00},
}


def _price(model: str) -> dict:
    if model in PRICING:
        return PRICING[model]
    # Fallback by family name (handles future minor versions)
    for k, v in PRICING.items():
        if model.startswith(k.rsplit('-', 1)[0]):
            return v
    return PRICING['claude-haiku-4-5-20251001']


def estimate_call_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return USD cost for a single Anthropic call."""
    p = _price(model)
    return (input_tokens / 1_000_000) * p['input'] + (output_tokens / 1_000_000) * p['output']


class BudgetExceeded(Exception):
    """Raised when a tracked call would push us past a hard cap."""
    def __init__(self, scope: str, spent_cents: int, cap_cents: int, attempted_cents: int):
        super().__init__(
            f'budget exceeded ({scope}): spent ${spent_cents/100:.4f} '
            f'+ ${attempted_cents/100:.4f} attempted > cap ${cap_cents/100:.4f}'
        )
        self.scope = scope
        self.spent_cents = spent_cents
        self.cap_cents = cap_cents
        self.attempted_cents = attempted_cents


@dataclass
class CostTracker:
    """Tracks per-lead and pipeline-wide spend.

    Pipeline-wide state persists across runs in a small JSON file on the
    persistent volume (so a daily cron's first run remembers what the previous
    cron spent that calendar day).
    """
    per_lead_cap_cents: int = 15           # $0.15 default
    daily_cap_cents: int = 1000            # $10.00 default
    per_lead_spent_cents: float = 0.0
    daily_spent_cents: float = 0.0
    daily_state_path: Optional[Path] = None
    calls: list = field(default_factory=list)  # tool/model call log

    def __post_init__(self):
        if self.daily_state_path is None:
            base = Path(os.environ.get('PIPELINE_DB_PATH', '/data/pipeline.db')).parent
            self.daily_state_path = base / 'agent_daily_spend.json'
        self._load_daily()

    def _load_daily(self):
        try:
            data = json.loads(self.daily_state_path.read_text())
            if data.get('date') == time.strftime('%Y-%m-%d'):
                self.daily_spent_cents = float(data.get('spent_cents', 0))
        except Exception:
            pass

    def _save_daily(self):
        try:
            self.daily_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.daily_state_path.write_text(json.dumps({
                'date': time.strftime('%Y-%m-%d'),
                'spent_cents': self.daily_spent_cents,
            }))
        except Exception:
            pass

    def check_can_afford(self, model: str, est_input: int, est_output: int) -> None:
        """Raise BudgetExceeded if this call would breach either cap."""
        cost_usd = estimate_call_cost(model, est_input, est_output)
        cost_cents = cost_usd * 100
        if self.per_lead_spent_cents + cost_cents > self.per_lead_cap_cents:
            raise BudgetExceeded(
                'per_lead',
                int(self.per_lead_spent_cents),
                self.per_lead_cap_cents,
                int(cost_cents),
            )
        if self.daily_spent_cents + cost_cents > self.daily_cap_cents:
            raise BudgetExceeded(
                'daily',
                int(self.daily_spent_cents),
                self.daily_cap_cents,
                int(cost_cents),
            )

    def record_call(self, model: str, input_tokens: int, output_tokens: int, label: str = '') -> float:
        """Record an actual call's spend. Returns the call cost in USD."""
        cost_usd = estimate_call_cost(model, input_tokens, output_tokens)
        cost_cents = cost_usd * 100
        self.per_lead_spent_cents += cost_cents
        self.daily_spent_cents += cost_cents
        self.calls.append({
            'ts': time.strftime('%H:%M:%S'),
            'model': model,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cost_usd': round(cost_usd, 6),
            'label': label,
        })
        self._save_daily()
        return cost_usd

    def reset_per_lead(self):
        """Call at the start of each lead so the per-lead cap is fresh."""
        self.per_lead_spent_cents = 0.0
        self.calls = []

    def summary(self) -> dict:
        return {
            'per_lead_spent_cents': round(self.per_lead_spent_cents, 4),
            'per_lead_cap_cents': self.per_lead_cap_cents,
            'daily_spent_cents': round(self.daily_spent_cents, 4),
            'daily_cap_cents': self.daily_cap_cents,
            'n_calls': len(self.calls),
            'calls': self.calls,
        }


def quick_estimate(model: str, expected_input: int, max_output: int) -> int:
    """Conservative pre-call cost estimate in cents. Used to decide whether to
    even ATTEMPT a call; the actual cost is recorded after."""
    cost_usd = estimate_call_cost(model, expected_input, max_output)
    return int(cost_usd * 100) + 1  # +1 cent safety margin
