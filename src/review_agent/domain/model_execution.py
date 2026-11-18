"""What admission fixes for one model execution: its window and the account's quota verdict."""

from dataclasses import dataclass
from datetime import datetime


# The shortest wait before a waiting account's quota is checked again.
QUOTA_RECHECK_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ModelWindow:
    """What one session of the model can hold.

    ``result_chars`` bounds one tool result and ``turn_chars`` all tool results
    of one model response; above either, the engine replaces results with a
    preview the reviewer cannot open. ``compaction_tokens`` is where the
    configured compression threshold starts summarizing the session.
    """

    context_tokens: int
    result_chars: int
    turn_chars: int
    compaction_tokens: int


@dataclass(frozen=True, slots=True)
class QuotaCheck:
    """One observation of whether the account may start work, and when to look again."""

    observed_at: datetime
    allowed: bool
    retry_at: datetime
