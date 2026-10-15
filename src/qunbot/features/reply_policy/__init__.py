"""Optional reply decision policy — the Group Chat Plus "read the room" idea.

The core rule is and stays *group messages are answered only when @-ed*. This
package is the deployer's opt-in replacement for that one decision, and the
loader contract is the same as every other feature package::

    register(host, config, model) -> None   # sets host.reply_policy
    validate() -> dict                      # startup check summary

Nothing here is enabled by default. With no environment set, ``register``
returns before touching the host, ``FeatureHost.reply_policy`` stays ``None``
and ``ConversationService`` applies its built-in rule — so "off" is byte-for-byte
the old behaviour, and one variable (``BOT_REPLY_POLICY_ENABLED=false``) or one
mode change (``BOT_REPLY_POLICY_MODE=mention_only``) goes back to it.

A group member cannot enable, disable or retune this from chat: decisions read
only the environment (frozen at startup) and the messages handed in by the
service. There is no tool, no observer and no context contribution in this
package, and it adds nothing to the Agent's stable prefix.

Two policies live here:

``MentionOnlyPolicy``   the old rule, expressed as a policy object so the two
                        can be replayed against each other.
``RoomReadingPolicy``   a deterministic heuristic over the recent transcript
                        and the @ state. It can abstain, and says why.

``replay.py`` scores either one against a labelled message sequence offline,
without a gateway or a model.
"""

from __future__ import annotations

from .config import ReplyPolicyConfig
from .policy import Decision, MentionOnlyPolicy, RoomReadingPolicy
from .adaptive import AdaptiveReplyPolicy


def build_policy(config: ReplyPolicyConfig):
    """The configured policy, or ``None`` meaning "keep the built-in rule"."""
    if not config.enabled:
        return None
    if config.mode == "room":
        return RoomReadingPolicy(config)
    return MentionOnlyPolicy(config)


def register(host, _config, _model) -> None:
    """Install the configured policy on the feature host, or leave it alone.

    Disabled is the default and the safe state: this returns without importing
    or building anything, so a deployment that has not opted in pays nothing.
    """
    policy = build_policy(ReplyPolicyConfig.from_env())
    if policy is not None:
        if isinstance(policy, RoomReadingPolicy):
            adaptive = AdaptiveReplyPolicy(policy.config)
            host.reply_policy = adaptive
            host.binders.append(adaptive.bind)
        else:
            host.reply_policy = policy


def validate() -> dict:
    """Startup summary. Never raises for a valid mode, never enables anything."""
    config = ReplyPolicyConfig.from_env()
    return {
        "enabled": config.enabled,
        "mode": config.mode,
        "threshold": config.threshold,
        # False means "no policy is installed and the built-in @-only rule
        # applies" — the state every deployment starts in.
        "installed": config.enabled,
    }


__all__ = [
    "Decision",
    "AdaptiveReplyPolicy",
    "MentionOnlyPolicy",
    "ReplyPolicyConfig",
    "RoomReadingPolicy",
    "build_policy",
    "register",
    "validate",
]
