"""SocialState is a view, never a second writer of affection or emotion."""

from __future__ import annotations
import time

from qunbot.contracts.ontology.objects import edge, entity, ref, stable_id


class SocialProjector:
    def __init__(self, graph, people, mood):
        self.graph, self.people, self.mood = graph, people, mood

    async def observe(self, event, bot_reply):
        if not event.group_id:
            return
        scope = event.scope
        stamp = int(time.time() * 1000)
        agent_id = stable_id("agent", scope, "bot")
        agent = self.graph.get(scope, agent_id) or entity(
            "AgentIdentity",
            scope,
            agent_id,
            stamp,
            deployment_id="qunbot",
            persona_version="owner-persona-v1",
        )
        person = self.graph.get(scope, stable_id("person", scope, event.user_id))
        if person is None:
            person = entity(
                "Person",
                scope,
                stable_id("person", scope, event.user_id),
                event.timestamp * 1000,
                platform="qq",
                account_ref=event.user_id,
            )
        profile = self.people.profile(event.group_id, event.user_id)
        emotion = (
            self.mood.current(scope).values()
            if self.mood is not None
            else {
                k: 50
                for k in ("valence", "energy", "stress", "interest", "sociability")
            }
        )
        identity = stable_id("social", scope, person["id"])
        current = self.graph.get(scope, identity)
        state = entity(
            "SocialState",
            scope,
            identity,
            current["created_at"] if current else stamp,
            agent_ref=ref(agent),
            subject_ref=ref(person),
            relationship_stage=profile.get("stage", "unknown"),
            affection=profile.get("affection", 0),
            emotion=emotion,
            revision=(current["revision"] + 1 if current else 1),
            decayed_at=stamp,
        )
        state["updated_at"] = stamp
        self.graph.write(
            scope,
            [agent, person, state],
            [edge("state_for", state, person, stamp)],
            reason="authoritative-social-view",
        )
