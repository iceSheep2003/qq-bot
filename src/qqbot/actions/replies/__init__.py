"""Reply composition, modality planning and delivery orchestration."""

from qqbot.actions.replies.dispatcher import ReplyDispatcher
from qqbot.actions.replies.models import DeliveryResult, ReplyDraft, ReplyPlan
from qqbot.actions.replies.parser import REPLY_PROTOCOL, parse_reply_draft
from qqbot.actions.replies.planner import DefaultReplyPlanner, ReplyCapabilities
from qqbot.actions.replies.qq_faces import QQFacePolicy
from qqbot.actions.replies.pacing import HumanizedPacer
from qqbot.actions.replies.media_policy import CasualMediaPolicy
from qqbot.actions.replies.segmentation import TextSegmenter

__all__ = [
    "DefaultReplyPlanner", "DeliveryResult", "REPLY_PROTOCOL",
    "HumanizedPacer", "CasualMediaPolicy", "ReplyCapabilities", "QQFacePolicy", "ReplyDispatcher", "ReplyDraft", "ReplyPlan",
    "TextSegmenter", "parse_reply_draft",
]
