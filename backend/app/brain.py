"""One brain, two channels: every turn (typed, spoken, or an app event) runs here.

Per turn:
  1. code computes signals and the director's note
  2. Claude streams a reply and calls save_onboarding_info when it learns something
  3. code validates each call, updates state, and emits UI events

Tool results are normally deferred to the start of the next user message, so a
turn costs one model call. A follow-up call happens only when the model saved
something without saying anything, or when a value was rejected and it needs to
recover right away.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from . import orchestrator as orch
from .config import Settings
from .prompts import SAVE_TOOL, SYSTEM_PROMPT
from .state import OnboardingState, Turn

log = logging.getLogger("persona.brain")

MAX_ROUNDS = 3
FALLBACK_REPLY = "Sorry, I lost my train of thought for a second. Mind saying that again?"


def _block_to_dict(block: Any) -> dict[str, Any] | None:
    t = block.type
    if t == "text":
        return {"type": "text", "text": block.text} if block.text else None
    if t == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if t == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if t == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    return block.model_dump(mode="json", exclude_none=True)


class Brain:
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None):
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic()

    def _request_kwargs(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.settings.llm_model,
            "max_tokens": 2048,
            # Static prefix (tools + system) is cached; the top-level breakpoint
            # also caches the append-only history up to the newest message.
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "tools": [SAVE_TOOL],
            "messages": list(messages),
            "cache_control": {"type": "ephemeral"},
        }
        if self.settings.llm_model.startswith("claude-haiku-4-5"):
            # Haiku 4.5 has no adaptive thinking or effort; omitting both means no thinking.
            return kwargs
        if self.settings.llm_thinking == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        else:
            kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": self.settings.llm_effort}
        return kwargs

    async def run_turn(
        self,
        state: OnboardingState,
        *,
        user_text: str | None = None,
        event_text: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one turn. Yields {"type": "delta"|"ui"|"done", ...} events for the client."""
        channel = state.channel
        if user_text is not None:
            orch.before_user_turn(state, user_text)
            state.transcript.append(Turn(role="user", text=user_text, channel=channel))
            body = f"[{channel}] {user_text}"
        else:
            body = f"[event: {event_text}]"

        value_moment_due = orch.value_moment_due(state)
        note = orch.directors_note(state, channel=channel, user_text=user_text or "")

        # Remember where we were so an API failure leaves the history valid.
        rollback_len = len(state.messages)
        rollback_pending = list(state.pending_tool_results)

        state.messages.append(
            {"role": "user", "content": state.pending_tool_results + [{"type": "text", "text": f"{body}\n\n{note}"}]}
        )
        state.pending_tool_results = []

        reply_parts: list[str] = []
        ui_events: list[dict[str, Any]] = []
        progressed = False
        t0 = time.perf_counter()
        ttft: float | None = None
        usage: dict[str, int] = {}

        try:
            for round_no in range(MAX_ROUNDS):
                round_text: list[str] = []
                async with self.client.messages.stream(**self._request_kwargs(state.messages)) as stream:
                    async for event in stream:
                        if event.type == "text":
                            if ttft is None:
                                ttft = time.perf_counter() - t0
                            if not round_text and reply_parts:
                                yield {"type": "delta", "text": " "}
                            round_text.append(event.text)
                            yield {"type": "delta", "text": event.text}
                    final = await stream.get_final_message()

                u = final.usage
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    usage[k] = usage.get(k, 0) + (getattr(u, k, 0) or 0)

                blocks = [d for b in final.content if (d := _block_to_dict(b))]
                if final.stop_reason == "refusal":
                    blocks = [b for b in blocks if b["type"] == "text"]
                if not any(b["type"] in ("text", "tool_use") for b in blocks):
                    # Never leave an empty assistant turn in the history.
                    blocks = [{"type": "text", "text": FALLBACK_REPLY}]
                    yield {"type": "delta", "text": FALLBACK_REPLY}
                    round_text.append(FALLBACK_REPLY)
                state.messages.append({"role": "assistant", "content": blocks})
                reply_parts.append("".join(round_text))

                tool_uses = [b for b in blocks if b["type"] == "tool_use"]
                results: list[dict[str, Any]] = []
                any_rejected = False
                for tu in tool_uses:
                    res = orch.apply_tool_call(state, tu["input"])
                    progressed |= res.progressed
                    any_rejected |= bool(res.rejected)
                    ui_events.extend(res.ui)
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": res.tool_result_text()})

                spoke = bool("".join(round_text).strip())
                needs_followup = tool_uses and (not spoke or any_rejected) and final.stop_reason == "tool_use"
                if needs_followup and round_no < MAX_ROUNDS - 1:
                    follow_note = orch.directors_note(state, channel=channel)
                    instruction = (
                        "[event: saved. Now say your reply to the user.]"
                        if not spoke
                        else "[event: a value was rejected (see tool result). Add one short line to recover; don't repeat yourself.]"
                    )
                    state.messages.append(
                        {"role": "user", "content": results + [{"type": "text", "text": f"{instruction}\n\n{follow_note}"}]}
                    )
                    continue

                state.pending_tool_results = results
                break
        except (anthropic.APIError, ValueError) as e:
            log.exception("LLM turn failed: %s", e)
            del state.messages[rollback_len:]
            state.pending_tool_results = rollback_pending
            prefix = " " if reply_parts and any(reply_parts) else ""
            yield {"type": "delta", "text": prefix + FALLBACK_REPLY}
            reply_parts.append(FALLBACK_REPLY)

        reply = " ".join(p.strip() for p in reply_parts if p.strip())
        state.transcript.append(Turn(role="agent", text=reply, channel=channel))

        if value_moment_due:
            state.value_moment_done = True
        if orch.call_offer_due(state):
            state.call_status = "offered"
            ui_events.append({"type": "show_call_offer"})
        orch.after_turn(state, progressed)

        for ev in ui_events:
            yield {"type": "ui", "ui": ev}

        latency = {"ttft_ms": round(ttft * 1000) if ttft else None, "total_ms": round((time.perf_counter() - t0) * 1000)}
        log.info("turn %s channel=%s latency=%s usage=%s", state.session_id[:8], channel, latency, json.dumps(usage))
        yield {"type": "done", "reply": reply, "latency": latency, "usage": usage}
