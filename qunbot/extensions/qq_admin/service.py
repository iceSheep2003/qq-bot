from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from ...domain import MessageEvent
from .config import QQAdminConfig

log = logging.getLogger(__name__)


@dataclass
class TitleProposal:
    group_id: str
    target_id: str
    title: str
    reason: str
    expires_at: float
    prompt_message_id: str = ""
    voters: set[str] = field(default_factory=set)


class QQAdminService:
    def __init__(self, config: QQAdminConfig):
        self.config = config
        self.runtime = None
        # Proposals are intentionally short-lived process state. A restart
        # cancels them rather than accidentally granting an old vote later.
        self._title_proposals: dict[str, TitleProposal] = {}

    def bind(self, runtime) -> None:
        self.runtime = runtime

    def _ready(self, event: MessageEvent, *, operator: bool = False):
        if self.runtime is None or not event.group_id:
            raise ValueError("QQ group administration is unavailable here")
        if event.group_id not in self.runtime.policy.allowed_groups:
            raise ValueError("group is not allowlisted")
        if operator and event.user_id not in self.config.operator_ids:
            raise ValueError("only a locally configured operator may request this action")
        return self.runtime.sender

    async def list_group_files(self, _args: dict, event: MessageEvent) -> str:
        gateway = self._ready(event)
        data = await gateway.call("get_group_root_files", {"group_id": int(event.group_id)})
        files = list(data.get("files") or ()) if isinstance(data, dict) else []
        folders = list(data.get("folders") or ()) if isinstance(data, dict) else []
        # NapCat exposes folders through a separate action. Scan only one level
        # and cap both fan-out and result size: a question about资料 must not
        # become an unbounded series of OneBot calls in a large archive.
        for folder in folders[:10]:
            folder_id = str(folder.get("folder_id") or folder.get("id") or "")
            if not folder_id:
                continue
            nested = await gateway.call("get_group_files_by_folder", {
                "group_id": int(event.group_id), "folder_id": folder_id,
            })
            folder_name = str(folder.get("folder_name") or folder.get("name") or "")[:80]
            for item in (nested.get("files") or ())[:20] if isinstance(nested, dict) else ():
                copied = dict(item)
                copied["_folder"] = folder_name
                files.append(copied)
            if len(files) >= 50:
                break
        result = []
        for item in files[:50]:
            result.append({
                "name": str(item.get("file_name") or item.get("name") or "")[:120],
                "id": str(item.get("file_id") or item.get("id") or "")[:100],
                "size": int(item.get("file_size") or item.get("size") or 0),
                "folder": str(item.get("_folder") or "")[:80],
            })
        return json.dumps(result, ensure_ascii=False)

    async def mute_member(self, args: dict, event: MessageEvent) -> str:
        gateway = self._ready(event, operator=True)
        target = str(args.get("user_id") or "")
        duration = int(args.get("duration") or 0)
        if not target.isdigit():
            raise ValueError("target must be a numeric QQ member")
        if not 0 <= duration <= self.config.max_mute_seconds:
            raise ValueError(f"duration must be 0..{self.config.max_mute_seconds} seconds")
        member = await gateway.call("get_group_member_info", {
            "group_id": int(event.group_id), "user_id": int(target), "no_cache": False,
        })
        if str(member.get("role") or "member") in {"owner", "admin"}:
            raise ValueError("owners and administrators cannot be muted by this tool")
        await gateway.call("set_group_ban", {
            "group_id": int(event.group_id), "user_id": int(target), "duration": duration,
        })
        return "已解除禁言" if duration == 0 else f"已禁言 {duration} 秒"

    async def set_essence(self, args: dict, event: MessageEvent) -> str:
        gateway = self._ready(event, operator=True)
        message_id = str(args.get("message_id") or "")
        permitted = {event.platform_message_id, event.reply_to_message_id} - {""}
        if message_id not in permitted:
            raise ValueError("only the current or explicitly replied-to message may be changed")
        enable = bool(args.get("enable", True))
        await gateway.call(
            "set_essence_msg" if enable else "delete_essence_msg",
            {"message_id": int(message_id)},
        )
        return "已设为精华" if enable else "已取消精华"

    async def propose_title(self, args: dict, event: MessageEvent) -> str:
        gateway = self._ready(event)
        if not self.config.title_enabled:
            raise ValueError("group title proposals are disabled")
        target = str(args.get("user_id") or "").strip()
        title = re.sub(r"[\r\n\t]", "", str(args.get("title") or "")).strip()
        reason = re.sub(r"[\r\n\t]", " ", str(args.get("reason") or "")).strip()[:80]
        if not target.isdigit() or target == event.user_id:
            raise ValueError("target must be another numeric QQ member")
        if not title or len(title) > self.config.title_max_chars:
            raise ValueError(
                f"title must be 1..{self.config.title_max_chars} characters"
            )
        if any(char in title for char in "[]{}<>/\\"):
            raise ValueError("title contains unsupported punctuation")
        member = await gateway.call("get_group_member_info", {
            "group_id": int(event.group_id), "user_id": int(target), "no_cache": False,
        })
        nickname = str(member.get("card") or member.get("nickname") or target)[:40]
        proposal = TitleProposal(
            group_id=event.group_id,
            target_id=target,
            title=title,
            reason=reason,
            expires_at=time.time() + self.config.title_proposal_seconds,
        )
        # One live proposal per group keeps replies unambiguous.
        self._title_proposals[event.group_id] = proposal
        text = (
            f"我提议给 {nickname} 一个群头衔「{title}」"
            + (f"，理由：{reason}" if reason else "")
            + "。本人回复这条消息“同意头衔”即可通过；本人回复“拒绝头衔”会取消。"
            f"其他群友回复这条消息“赞成头衔”，满 {self.config.title_vote_threshold} 人也会通过。"
        )
        sent = await gateway.send(
            group_id=event.group_id, user_id=None, text=text,
            at_user=target, allowed_at={target},
        )
        proposal.prompt_message_id = str((sent or {}).get("message_id") or "")
        return "头衔提案已发出；等待本人同意或群友投票，不要直接宣称已经授予"

    async def handle_raw(self, raw: dict) -> bool:
        if self.runtime is None:
            return False
        post_type = raw.get("post_type")
        if post_type == "request" and raw.get("request_type") == "group":
            return await self._review_join(raw)
        if (
            post_type == "notice"
            and raw.get("notice_type") == "group_increase"
        ):
            return await self._welcome(raw)
        if post_type == "message" and raw.get("message_type") == "group":
            return await self._handle_title_vote(raw)
        return False

    @staticmethod
    def _message_text(raw: dict) -> str:
        parts = raw.get("message")
        if not isinstance(parts, list):
            return str(raw.get("raw_message") or "").strip()
        return "".join(
            str(part.get("data", {}).get("text") or "")
            for part in parts if part.get("type") == "text"
        ).strip()

    @staticmethod
    def _reply_id(raw: dict) -> str:
        parts = raw.get("message")
        if not isinstance(parts, list):
            return ""
        for part in parts:
            if part.get("type") == "reply":
                return str(part.get("data", {}).get("id") or "")
        return ""

    async def _handle_title_vote(self, raw: dict) -> bool:
        group_id = str(raw.get("group_id") or "")
        proposal = self._title_proposals.get(group_id)
        if proposal is None:
            return False
        if time.time() >= proposal.expires_at:
            self._title_proposals.pop(group_id, None)
            return False
        # Votes must be explicit replies to the bot's proposal. If NapCat did
        # not return a message id, fail closed: no ambient "同意" can grant it.
        if not proposal.prompt_message_id or self._reply_id(raw) != proposal.prompt_message_id:
            return False
        text = self._message_text(raw).replace(" ", "")
        voter = str(raw.get("user_id") or "")
        if not voter or voter == str(raw.get("self_id") or ""):
            return False
        if voter == proposal.target_id and text == "拒绝头衔":
            self._title_proposals.pop(group_id, None)
            await self.runtime.sender.send(
                group_id=group_id, user_id=None, text="本人拒绝了这个头衔，提案已取消。",
            )
            return True
        accepted_by_target = voter == proposal.target_id and text == "同意头衔"
        if not accepted_by_target and text != "赞成头衔":
            return False
        if not accepted_by_target:
            if voter == proposal.target_id:
                return True
            proposal.voters.add(voter)
            if len(proposal.voters) < self.config.title_vote_threshold:
                await self.runtime.sender.send(
                    group_id=group_id, user_id=None,
                    text=f"已记录一票（{len(proposal.voters)}/{self.config.title_vote_threshold}）。",
                )
                return True
        # Re-check membership immediately before the external mutation.
        await self.runtime.sender.call("get_group_member_info", {
            "group_id": int(group_id), "user_id": int(proposal.target_id),
            "no_cache": True,
        })
        basis = "本人同意" if accepted_by_target else f"{len(proposal.voters)} 位群友赞成"
        try:
            await self.runtime.sender.call("set_group_special_title", {
                "group_id": int(group_id), "user_id": int(proposal.target_id),
                "special_title": proposal.title, "duration": -1,
            })
        except Exception:
            self._title_proposals.pop(group_id, None)
            log.exception("Title proposal passed but OneBot rejected the grant")
            await self.runtime.sender.send(
                group_id=group_id, user_id=None,
                text=f"提案已通过（{basis}），但 QQ 拒绝授予头衔；请确认机器人账号是群主。",
            )
            return True
        self._title_proposals.pop(group_id, None)
        await self.runtime.sender.send(
            group_id=group_id, user_id=None,
            text=f"提案通过（{basis}），头衔「{proposal.title}」已授予。",
            at_user=proposal.target_id, allowed_at={proposal.target_id},
        )
        return True

    async def _review_join(self, raw: dict) -> bool:
        group_id = str(raw.get("group_id") or "")
        if group_id not in self.runtime.policy.allowed_groups:
            return True
        if not self.config.join_review_enabled:
            return True
        comment = str(raw.get("comment") or "")
        reject = any(word in comment for word in self.config.join_reject_keywords)
        approve = any(word in comment for word in self.config.join_approve_keywords)
        if not reject and not approve and not self.config.join_default_approve:
            log.info("Join request left pending group=%s user=%s", group_id, raw.get("user_id"))
            return True
        accepted = approve and not reject or (
            self.config.join_default_approve and not reject
        )
        await self.runtime.sender.call("set_group_add_request", {
            "flag": str(raw.get("flag") or ""),
            "sub_type": str(raw.get("sub_type") or "add"),
            "approve": accepted,
            "reason": "" if accepted else "申请信息未通过自动审核",
        })
        log.info("Join request decided group=%s user=%s approve=%s", group_id, raw.get("user_id"), accepted)
        return True

    async def _welcome(self, raw: dict) -> bool:
        group_id = str(raw.get("group_id") or "")
        user_id = str(raw.get("user_id") or "")
        if group_id not in self.runtime.policy.allowed_groups:
            return True
        if not self.config.welcome_enabled or user_id == str(raw.get("self_id") or ""):
            return True
        nickname = user_id
        try:
            member = await self.runtime.sender.call("get_group_member_info", {
                "group_id": int(group_id), "user_id": int(user_id), "no_cache": False,
            })
            nickname = str(member.get("card") or member.get("nickname") or user_id)
        except Exception:
            log.exception("Could not read new member profile")
        event = MessageEvent(
            event_id=f"welcome:{group_id}:{user_id}:{int(time.time())}",
            scope=f"group:{group_id}", group_id=group_id, user_id=user_id,
            nickname=nickname,
            text=("新成员刚加入群聊。请写一句自然、简短、有一点个性但不过度熟络的欢迎词，"
                  f"称呼对方为 {nickname}，并顺带提示可以查看群公告和群文件。"),
            image_urls=(), at_bot=False, at_users=(), timestamp=int(time.time()),
        )
        try:
            reply = await self.runtime.agent.reply(event, proactive=True)
            draft = getattr(reply, "draft", None)
            clean = await self.runtime.send_draft(group_id, None, draft, event=event) if draft else ""
            if clean:
                return True
        except Exception:
            log.exception("Personalized welcome failed; no message sent")
        return True
