"""Agent configuration, execution and transactional conversation memory."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from importlib.resources import files
from pathlib import Path

import anyio
from groq import AsyncGroq
from pydantic_ai import Agent, RunContext, ToolOutput
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.providers.groq import GroqProvider

from .links import LinkGuard
from .observability import TurnObservation
from .owner import OWNER
from .tools.profile import (
    LinkTopic,
    render_experience,
    render_links,
    render_notes,
    render_online_presence,
)


def _configured_model() -> str:
    """Read the Groq model; reject unsupported configuration before serving turns."""
    model = os.environ.get("LLM_MODEL", "groq/openai/gpt-oss-120b")
    if not model.startswith("groq/"):
        raise ValueError("LLM_MODEL must use the groq/ prefix")
    return model


MODEL = _configured_model()


def _reasoning_effort(model: str) -> str | None:
    """How hard gpt-oss thinks before answering; `low` by default.

    Hidden reasoning is billed as output and runs before the first spoken word, so the default
    (`medium`) added hundreds of tokens and about a second to one-sentence answers. Other
    models reject the parameter, so it is only sent to gpt-oss. `none` leaves it to Groq.
    """
    value = os.environ.get("LLM_REASONING_EFFORT", "low").strip().lower()
    if "gpt-oss" not in model or value in ("", "none", "default"):
        return None
    if value not in ("low", "medium", "high"):
        raise ValueError("LLM_REASONING_EFFORT must be low, medium, high or none")
    return value


def _model_settings() -> dict:
    effort = _reasoning_effort(MODEL)
    return {"extra_body": {"reasoning_effort": effort}} if effort else {}


# Whole-request deadline for one Groq call (connect + time to a complete stream).
_GROQ_TIMEOUT_SECONDS = float(os.environ.get("GROQ_TIMEOUT_SECONDS", "10"))

# Cap stored turns per session to avoid unbounded memory growth. Counts messages
# (user + assistant), so 40 ≈ 20 exchanges.
_MAX_HISTORY = 40

# Only the latest messages go back to the model. Replayed link lists and long answers
# inflate every request and burn the per-minute token budget, and a screening rep needs
# little more than the last exchange to resolve "that" or "the first one".
_MODEL_HISTORY = 3

# Cap the number of live conversations so many one-off visitors cannot grow the
# in-memory store without limit. Oldest conversations are evicted first.
_MAX_CONVERSATIONS = 1000

_RESOURCE_ROOT = files("hireme_agent")

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_PATH = _RESOURCE_ROOT.joinpath("prompts/system.md")
_AUTHORED_PATHS = ("prompts/system.md",)
_SCHEDULING_LINE = (
    f"To schedule a call or share {OWNER.pronouns.possessive} calendar, booking link, "
    "or availability, call show_booking_link."
)
_NOTES_LINE = (
    f"Call get_notes for {OWNER.pronouns.possessive} own notes on "
    f"{', '.join(OWNER.notes)}; repeat them, never extrapolate."
)
# Plain replies get the disclosure from the prompt. After a tool call the model often
# forgets it, so the first answer is checked and prefixed in code.
_DISCLOSURE = f"I'm {OWNER.short_name}'s AI representative."
_UNRESOLVED_PLACEHOLDER = re.compile(
    r"(?:⟨[^⟩]+⟩|\{\{[^}]+\}\}|\{[a-z_]+\}|\b(?:TODO|TBD)\b)", re.IGNORECASE
)
# Owner details the authored prompt refers to as {name}, {possessive} and so on.
_OWNER_FIELDS = {
    "name": OWNER.name,
    "short_name": OWNER.short_name,
    "subject": OWNER.pronouns.subject,
    "object": OWNER.pronouns.object,
    "possessive": OWNER.pronouns.possessive,
    "contact_link": OWNER.contact_link,
    "contact_url": OWNER.contact.url,
}
_OWNER_FIELD = re.compile(r"(?<!\{)\{(" + "|".join(_OWNER_FIELDS) + r")\}(?!\})")


def _apply_disclosure(text: str, first_answer: bool) -> str:
    """Enforce the disclosure in code: present on a first answer, absent afterwards."""
    body = text.replace("\u2019", "'").lstrip()
    has_prefix = body.startswith(_DISCLOSURE)
    if first_answer:
        return text if has_prefix else _DISCLOSURE + " " + text
    if has_prefix:
        return text.lstrip()[len(_DISCLOSURE) :].lstrip()
    return text


def _could_become_disclosure(head: str) -> bool:
    """True while the text so far is still a (possibly empty) prefix of the disclosure."""
    body = head.replace("\u2019", "'").lstrip()
    return _DISCLOSURE.startswith(body) and body != _DISCLOSURE and len(body) < len(_DISCLOSURE)


def _load_system_prompt(path: Path = _SYSTEM_PROMPT_PATH) -> str:
    """Load authored instructions or knowledge, rejecting unsafe deployment states."""
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"system prompt is missing or unreadable: {path}") from exc
    if not prompt:
        raise RuntimeError(f"system prompt is empty: {path}")
    prompt = _OWNER_FIELD.sub(lambda match: _OWNER_FIELDS[match.group(1)], prompt)
    placeholder = _UNRESOLVED_PLACEHOLDER.search(prompt)
    if placeholder:
        raise RuntimeError(
            f"system prompt contains unresolved placeholder {placeholder.group(0)!r}: {path}"
        )
    return prompt


def _compose_prompt() -> str:
    return "\n\n".join(
        _load_system_prompt(_RESOURCE_ROOT.joinpath(path)) for path in _AUTHORED_PATHS
    )


# Configuration and authored resources are loaded once at import, with no network I/O.
_BOOKING_LINK = OWNER.booking_url
_SYSTEM_PROMPT = _compose_prompt()
if _BOOKING_LINK is not None:
    _SYSTEM_PROMPT += " " + _SCHEDULING_LINE
if OWNER.notes:
    _SYSTEM_PROMPT += " " + _NOTES_LINE
# The version covers everything the model can be told: the prompt and every tool result.
_PROMPT_VERSION = hashlib.sha256(
    "\n\n".join(
        [_SYSTEM_PROMPT, render_experience(), render_online_presence(), render_notes()]
    ).encode("utf-8")
).hexdigest()[:12]


_conversations: dict[str, list[dict[str, str]]] = {}

# Ids this server handed out whose first turn has not been committed (it failed, or answered
# with the busy reply). The client sends the id back, so a failed first turn must not make the
# next turn look like a stranger and fork the call. Only server-issued ids are accepted.
_issued: dict[str, None] = {}


_agent: Agent | None = None


def _create_model() -> GroqModel:
    """Groq client with a bounded wait: one short retry, then fail so the turn can answer.

    Groq's per-minute token limits return 429 with a `retry-after` of a few seconds. The
    SDK default (two silent retries with growing backoff) turned that into 24-30s of dead
    air on a call. One retry honours a short `retry-after`; a second failure surfaces as an
    error the API turns into a spoken "busy" reply, and the status lands in telemetry.
    """
    client = AsyncGroq(
        api_key=os.environ.get("GROQ_API_KEY"),
        timeout=_GROQ_TIMEOUT_SECONDS,
        max_retries=1,
    )
    return GroqModel(MODEL.removeprefix("groq/"), provider=GroqProvider(groq_client=client))


def _create_agent(observation: TurnObservation) -> Agent:
    """Construct once, translating the legacy prefix internally to direct Groq."""
    global _agent
    if _agent is None:
        # A text reply ends the run. get_online_presence and show_booking_link are output
        # functions with deterministic replies, so a turn that calls one costs a single model
        # request instead of a second one to narrate the tool result.
        output_type: list = [
            ToolOutput(
                get_online_presence,
                name="get_online_presence",
                description=_ONLINE_PRESENCE_DESCRIPTION,
            )
        ]
        if _BOOKING_LINK is not None:
            output_type.append(
                ToolOutput(
                    show_booking_link, name="show_booking_link", description=_BOOKING_DESCRIPTION
                )
            )
        output_type.append(str)
        _agent = Agent(
            _create_model(),
            system_prompt=_SYSTEM_PROMPT,
            output_type=output_type,
            retries=0,
            model_settings=_model_settings(),
        )
        _agent.tool(get_experience, description=_EXPERIENCE_DESCRIPTION)
        if OWNER.notes:
            _agent.tool(get_notes, description=_NOTES_DESCRIPTION)
    return _agent


def _wire_messages(messages: list[ModelRequest | ModelResponse]) -> list[dict[str, str]]:
    """What the model was sent, as plain chat messages, tool calls and results included."""
    wire: list[dict[str, str]] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                wire.append({"role": "system", "content": part.content})
            elif isinstance(part, UserPromptPart):
                content = part.content if isinstance(part.content, str) else str(part.content)
                wire.append({"role": "user", "content": content})
            elif isinstance(part, TextPart):
                wire.append({"role": "assistant", "content": part.content})
            elif isinstance(part, ToolCallPart):
                wire.append(
                    {
                        "role": "assistant",
                        "content": f"tool call: {part.tool_name}({part.args_as_json_str()})",
                    }
                )
            elif isinstance(part, ToolReturnPart):
                wire.append({"role": "tool", "content": part.model_response_str()})
    return wire


async def _run_tokens(
    text: str, conversation_id: str, observation: TurnObservation, deliver_raw
) -> None:
    # Every URL the rep may show; anything else in a reply was invented.
    guard = LinkGuard(
        {link.url for link in OWNER.links} | OWNER.note_urls | {_BOOKING_LINK} - {None}
    )

    async def deliver(delta: str) -> None:
        if safe := guard.feed(delta):
            await deliver_raw(safe)

    window = _conversations.get(conversation_id, [])[-_MODEL_HISTORY:]
    history: list[ModelRequest | ModelResponse] = []
    for message in window:
        if message["role"] == "user":
            history.append(ModelRequest(parts=[UserPromptPart(message["content"])]))
        else:
            history.append(ModelResponse(parts=[TextPart(message["content"])]))
    if history:
        # A resumed run does not add the agent's system prompt, so supply it explicitly.
        if isinstance(history[0], ModelRequest):
            history[0].parts.insert(0, SystemPromptPart(_SYSTEM_PROMPT))
        else:
            history.insert(0, ModelRequest(parts=[SystemPromptPart(_SYSTEM_PROMPT)]))
    streamed = False
    first_answer = not _conversations.get(conversation_id)
    tool_turn = False  # a function tool ran, so the reply is composed after the call
    head = ""  # buffers the start of an answer until its disclosure prefix is decided
    head_done = False

    async def deliver_text(delta: str) -> None:
        nonlocal streamed, head, head_done
        streamed = True
        # A first answer after a tool call, or any later answer, may carry a wrong prefix.
        if not (tool_turn or not first_answer) or head_done:
            await deliver(delta)
            return
        head += delta
        if _could_become_disclosure(head):
            return  # too short to tell yet; hold only while it still matches
        head_done = True
        if fixed := _apply_disclosure(head, first_answer):
            await deliver(fixed)

    try:
        agent = _create_agent(observation)
        async with agent.iter(text, message_history=history, deps=observation) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    properties = {
                        "$ai_provider": "groq",
                        "$ai_model": MODEL,
                        **({"$ai_effort": effort} if (effort := _reasoning_effort(MODEL)) else {}),
                        # The request exactly as sent: history, the new message and, on
                        # the second request of a tool turn, the tool call and its result.
                        "$ai_input": _wire_messages([*run.ctx.state.message_history, node.request]),
                    }
                    with observation.generation(properties):
                        async with node.stream(run.ctx) as stream:
                            try:
                                async for event in stream:
                                    if isinstance(event, PartStartEvent) and isinstance(
                                        event.part, TextPart
                                    ):
                                        if event.part.content:
                                            await deliver_text(event.part.content)
                                    elif isinstance(event, PartDeltaEvent) and isinstance(
                                        event.delta, TextPartDelta
                                    ):
                                        if event.delta.content_delta:
                                            await deliver_text(event.delta.content_delta)
                            except Exception as exc:
                                if observation.error is None:
                                    observation.error = exc
                                raise
                            finally:
                                response = stream.response
                                properties["$ai_output_choices"] = [
                                    {"role": "assistant", "content": part.content}
                                    for part in response.parts
                                    if isinstance(part, TextPart)
                                ]
                                called = [
                                    part.tool_name
                                    for part in response.parts
                                    if isinstance(part, ToolCallPart)
                                ]
                                if called:
                                    properties["$ai_tools_called"] = ",".join(called)
                                    tool_turn = True
                                usage = response.usage
                                properties["$ai_input_tokens"] = usage.input_tokens
                                properties["$ai_output_tokens"] = usage.output_tokens
                                if usage.cache_read_tokens:
                                    properties["$ai_cache_read_input_tokens"] = (
                                        usage.cache_read_tokens
                                    )
                                # Pricing uses the framework's bundled catalog and never
                                # blocks generation or performs a per-turn catalog fetch.
                                try:
                                    cost = response.cost()
                                    properties["$ai_total_cost_usd"] = float(cost.total_price)
                                except Exception:
                                    pass  # unavailable model pricing remains omitted
                # Iteration executes tools and continues through subsequent model requests.
        # An output function (the booking link) ends the run without streaming text.
        if head and not head_done:
            if fixed := _apply_disclosure(head, first_answer):
                await deliver(fixed)
        if not streamed and run.result is not None and isinstance(run.result.output, str):
            await deliver(_apply_disclosure(run.result.output, first_answer))
        if rest := guard.flush():
            await deliver_raw(rest)
        observation.unverified_links += guard.dropped
    except BaseException as exc:
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        if (
            not isinstance(exc, (GeneratorExit, asyncio.CancelledError))
            and observation.error is None
        ):
            observation.error = exc
        raise


async def _model_tokens(
    text: str, conversation_id: str, observation: TurnObservation
) -> AsyncIterator[str]:
    # The framework graph owns task-bound cancel scopes. Keep it in one task even
    # when an ASGI response resumes/closes the public iterator from another task.
    queue: asyncio.Queue[str | BaseException | None] = asyncio.Queue(maxsize=1)
    acknowledged = asyncio.Event()

    async def produce():
        try:

            async def deliver(delta):
                observation.output_state = (observation.output_state or "") + delta
                acknowledged.clear()
                await queue.put(delta)
                await acknowledged.wait()  # speech/delivery provides backpressure

            await _run_tokens(text, conversation_id, observation, deliver)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
                exc = exc.exceptions[0]
            if observation.error is None:
                observation.error = exc
            await queue.put(exc)
        else:
            await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
            acknowledged.set()
    finally:
        if not task.done():
            task.cancel()
        with anyio.CancelScope(shield=True):
            try:
                await task
            except asyncio.CancelledError:
                pass


def _history(conversation_id: str) -> list[dict[str, str]]:
    if conversation_id not in _conversations:
        while len(_conversations) >= _MAX_CONVERSATIONS:
            _conversations.pop(next(iter(_conversations)))
        _conversations[conversation_id] = []
    return _conversations[conversation_id]


def _commit(conversation_id: str, text: str, reply: str) -> None:
    history = _history(conversation_id)
    history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
    del history[:-_MAX_HISTORY]


class AgentTurn(AsyncIterator[str]):
    """Pending text exchange; delivery explicitly commits only after success."""

    def __init__(self, text: str, conversation_id: str, observation: TurnObservation):
        self.conversation_id = conversation_id
        self._text = text
        self._parts: list[str] = []
        self._complete = False
        self._committed = False
        observation.input_state = text
        self._tokens = self._stream(observation)

    async def _stream(self, observation: TurnObservation) -> AsyncIterator[str]:
        async with aclosing(_model_tokens(self._text, self.conversation_id, observation)) as tokens:
            async for delta in tokens:
                self._parts.append(delta)
                yield delta
        self._complete = True

    def __aiter__(self) -> AgentTurn:
        return self

    async def __anext__(self) -> str:
        return await anext(self._tokens)

    def commit(self) -> None:
        if not self._complete:
            raise RuntimeError("cannot commit an incomplete agent turn")
        if not self._committed:
            _commit(self.conversation_id, self._text, "".join(self._parts).strip())
            self._committed = True

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._tokens.aclose()


def prepare_conversation(conversation_id: str | None = None) -> str:
    """Resolve identity without allocating conversation memory for an unsuccessful turn."""
    if conversation_id and (conversation_id in _conversations or conversation_id in _issued):
        return conversation_id
    issued = uuid.uuid4().hex
    _issued[issued] = None
    while len(_issued) > _MAX_CONVERSATIONS:
        _issued.pop(next(iter(_issued)))
    return issued


def greeting() -> str:
    """What the rep says when a call starts: who it is, what it can do, then the floor."""
    his = OWNER.pronouns.possessive
    # Name only the kinds of evidence this owner actually has links for.
    topics = {link.topic for link in OWNER.links}
    kinds = [topic for topic in ("papers", "code") if topic in topics]
    shared = ", ".join(kinds) + " and links" if kinds else "links"
    offers = f"walk you through {his} experience, share {his} {shared}"
    if _BOOKING_LINK is not None:
        offers += ", or help you book a call"
    return f"Hi, {_DISCLOSURE} I can {offers}. How can I help?"


def commit_greeting(conversation_id: str) -> str:
    """Record the greeting as the call's first assistant message and return it.

    The disclosure has now been made, so later answers are not "first answers" and the model
    is never asked to repeat it.
    """
    text = greeting()
    _history(conversation_id).append({"role": "assistant", "content": text})
    return text


def start_text(text: str, conversation_id: str, observation: TurnObservation) -> AgentTurn:
    return AgentTurn(text, conversation_id, observation)


PROMPT_VERSION = _PROMPT_VERSION


def calendar_enabled() -> bool:
    """Report whether the owner file shares a booking link."""
    return _BOOKING_LINK is not None


async def initialize() -> None:
    """Validate request settings before the API lifespan starts serving turns."""
    _reasoning_effort(MODEL)


async def shutdown() -> None:
    """Kept for the API lifespan; nothing is held open between turns."""


_HIS, _HIM, _HE = OWNER.pronouns.possessive, OWNER.pronouns.object, OWNER.pronouns.subject
# Tool descriptions name the owner, so they are built here rather than kept in docstrings.
_EXPERIENCE_DESCRIPTION = (
    f"{OWNER.short_name}'s career timeline, most recent first: each role with what {_HE} did "
    f"there. Call for any question about {_HIS} experience, "
    f"background, roles, what {_HE} has built or achieved, or why to hire {_HIM}."
)
_ONLINE_PRESENCE_DESCRIPTION = (
    f"Final reply with {_HIS} public links, for any question about what is online, evidence, "
    "proof, papers, publications, code, articles, or any link.\n\n"
    f"Pick the narrowest topic: highlights ({_HIS} best evidence in one short answer; use it "
    'for evidence, proof, or "what is online"), profile (public profile), code (repositories '
    "and implementations), papers (research papers and thesis slides), articles (writing), or "
    "all only when asked for everything. Ends the turn with the verified links; write no "
    "answer of your own."
)
_NOTES_DESCRIPTION = (
    f"{OWNER.short_name}'s own notes, in {_HIS} words, on: {', '.join(OWNER.notes)}. Call for "
    "any question those topics cover."
)
_BOOKING_DESCRIPTION = (
    f"Final reply for any request to schedule, book, or meet, for {_HIS} calendar or booking "
    f"link, or for availability.\n\nEnds the turn with {OWNER.short_name}'s verified booking "
    "link; write no answer of your own."
)


async def get_experience(ctx: RunContext[TurnObservation]) -> str:
    """The owner's career timeline; described to the model by _EXPERIENCE_DESCRIPTION."""
    result = render_experience()
    with ctx.deps.span(
        "Experience lookup", **{"$ai_tool_name": "get_experience", "$ai_output_state": result}
    ):
        return result


async def get_notes(ctx: RunContext[TurnObservation]) -> str:
    """The owner's other sections; described to the model by _NOTES_DESCRIPTION."""
    result = render_notes()
    with ctx.deps.span(
        "Notes lookup", **{"$ai_tool_name": "get_notes", "$ai_output_state": result}
    ):
        return result


def get_online_presence(ctx: RunContext[TurnObservation], topic: LinkTopic = "all") -> str:
    """The owner's public links by topic; described by _ONLINE_PRESENCE_DESCRIPTION."""
    result = render_links(topic)
    with ctx.deps.span(
        "Online presence lookup",
        **{
            "$ai_tool_name": "get_online_presence",
            "$ai_input_state": f"topic={topic}",
            "$ai_output_state": result,
        },
    ):
        return result


def show_booking_link(ctx: RunContext[TurnObservation]) -> str:
    """The verified booking link; described to the model by _BOOKING_DESCRIPTION."""
    if _BOOKING_LINK is None:
        raise RuntimeError("Scheduling is disabled")
    reply = (
        f"You can pick a time on [{OWNER.short_name}'s calendar]({_BOOKING_LINK}) "
        f"and see {_HIS} availability there. I can't see availability or book for you."
    )
    with ctx.deps.span(
        "Booking link",
        **{
            "$ai_tool_name": "show_booking_link",
            "$ai_output_state": reply,
        },
    ):
        return reply
