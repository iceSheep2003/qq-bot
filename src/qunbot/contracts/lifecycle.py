"""Explicit state/event contracts. Pure checks, no persistence or side effects."""

from qunbot.contracts.schema import ContractError, fingerprint

_RULES = {
    "execution": {
        ("planned", "authorize"): "executing",
        ("planned", "deny"): "denied",
        ("planned", "cancel"): "cancelled",
        ("executing", "confirmed_success"): "succeeded",
        ("executing", "confirmed_failure"): "failed",
        ("executing", "uncertain_result"): "unknown",
        ("unknown", "confirmed_success"): "succeeded",
        ("unknown", "confirmed_failure"): "failed",
    },
    "proposal": {
        ("proposed", "published"): "awaiting_consent",
        ("proposed", "cancel"): "cancelled",
        ("proposed", "expire"): "expired",
        ("awaiting_consent", "qualified_approval"): "approved",
        ("awaiting_consent", "reject"): "rejected",
        ("awaiting_consent", "expire"): "expired",
        ("awaiting_consent", "cancel"): "cancelled",
        ("approved", "authorize"): "executing",
        ("approved", "cancel"): "cancelled",
        ("approved", "confirmed_failure"): "failed",
        ("executing", "confirmed_success"): "succeeded",
        ("executing", "confirmed_failure"): "failed",
        ("executing", "uncertain_result"): "unknown",
        ("unknown", "confirmed_success"): "succeeded",
        ("unknown", "confirmed_failure"): "failed",
    },
    "commitment": {
        ("open", "confirmed_fulfillment"): "fulfilled",
        ("open", "cancel"): "cancelled",
        ("open", "expire"): "expired",
        ("open", "dispute"): "disputed",
        ("disputed", "confirmed_fulfillment"): "fulfilled",
        ("disputed", "cancel"): "cancelled",
    },
}


def next_state(kind: str, current: str, event: str) -> str:
    target = _RULES.get(kind, {}).get((current, event))
    if target is None:
        raise ContractError("Illegal lifecycle event or state")
    return target


def specification() -> dict:
    return {
        "version": 1,
        "lifecycles": {
            kind: [
                {"from": old, "event": event, "to": new}
                for (old, event), new in sorted(rules.items())
            ]
            for kind, rules in sorted(_RULES.items())
        },
    }


def specification_hash() -> str:
    return fingerprint(specification())
