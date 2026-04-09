from __future__ import annotations

from packages.domain.services.response_policy import (
    ActiveRevisionLike,
    ResponseDecision,
    decide_response,
)
from packages.domain.services.understanding import MessageUnderstanding


def decide_memory_aware_response(
    *,
    understanding: MessageUnderstanding,
    active_revision: ActiveRevisionLike | None,
    user_preference_profile: dict[str, object] | None,
    risk_profile: dict[str, object] | None,
    require_approval: bool,
) -> ResponseDecision:
    return decide_response(
        understanding,
        active_revision=active_revision,
        require_approval=require_approval,
        user_preference_profile=user_preference_profile,
        risk_profile=risk_profile,
    )
