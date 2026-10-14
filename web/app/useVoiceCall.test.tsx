import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { useVoiceCall } from "./useVoiceCall";

const mocks = vi.hoisted(() => ({
  options: null as null | {
    enabled: boolean; muted: boolean;
    onUtterance: (audio: Blob, timing?: { speechMs: number; speechEndedAt: number }) => void;
    onError: (message: string) => void;
    onEvent?: (name: string, props: Record<string, unknown>) => void;
  },
  started: false,
  start: vi.fn(async () => { mocks.started = true; }),
  stop: vi.fn(() => { mocks.started = false; }),
  capture: vi.fn((_event: string, _props?: Record<string, unknown>, _options?: Record<string, unknown>) => ({ uuid: "accepted" })),
  optedOut: vi.fn(() => false),
  distinct: vi.fn(() => "person"),
  session: vi.fn(() => "visit"),
}));

vi.mock("./voiceCall/useVoiceActivity", () => ({
  useVoiceActivity: (options: typeof mocks.options) => {
    mocks.options = options;
    return { start: mocks.start, stop: mocks.stop, isStarted: () => mocks.started };
  },
}));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: mocks.capture, get_distinct_id: mocks.distinct, get_session_id: mocks.session, has_opted_out_capturing: mocks.optedOut }),
}));

type FakeAudio = {
  src: string; onended: (() => void) | null;
  play: ReturnType<typeof vi.fn>; pause: ReturnType<typeof vi.fn>;
};
let audios: FakeAudio[];
let fetchMock: Mock<(...args: any[]) => any>;
let greetingMock: Mock<(...args: any[]) => any>;
let playResult: Promise<void> | undefined;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function streamingResponse() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const cancel = vi.fn();
  const stream = new ReadableStream<Uint8Array>({ start: (c) => { controller = c; }, cancel });
  return {
    response: new Response(stream), cancel,
    send: (event: Record<string, unknown>) => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`)),
    chunk: (text: string) => controller.enqueue(new TextEncoder().encode(text)),
    close: () => controller.close(),
  };
}

function replyResponse(text = "Reply.", conversationId = "conversation", audio = "QUJD", traceId = "trace") {
  const stream = streamingResponse();
  stream.send({ type: "transcript", text: "Question" });
  stream.send({ type: "sentence", text, audio });
  stream.send({ type: "done", conversation_id: conversationId, trace_id: traceId });
  return stream.response;
}

async function setupCall() {
  const hook = renderHook(() => useVoiceCall());
  await act(async () => { hook.result.current.activate(); });
  return hook;
}

async function utterance(timing?: { speechMs: number; speechEndedAt: number }) {
  await act(async () => { mocks.options?.onUtterance(new Blob(["speech"], { type: "audio/webm" }), timing); });
}

const captured = (name: string) =>
  mocks.capture.mock.calls.filter(([event]) => event === name).map(([, props]) => props);

beforeEach(() => {
  mocks.options = null;
  mocks.started = false;
  mocks.start.mockClear();
  mocks.stop.mockClear();
  mocks.capture.mockReset().mockReturnValue({ uuid: "accepted" });
  mocks.optedOut.mockReset().mockReturnValue(false);
  mocks.distinct.mockReset().mockReturnValue("person");
  mocks.session.mockReset().mockReturnValue("visit");
  audios = [];
  playResult = undefined;
  fetchMock = vi.fn();
  // Calls open with a greeting request; by default it fails so a call just starts listening.
  greetingMock = vi.fn().mockRejectedValue(new Error("no greeting"));
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) =>
    String(url).endsWith("/greeting") ? greetingMock(url, init) : fetchMock(url, init));
  vi.stubGlobal("Audio", class {
    onended = null;
    onpause = null;
    play = vi.fn(() => playResult);
    pause = vi.fn();
    constructor(public src: string) { audios.push(this); }
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.useRealTimers(); });

describe("end-of-call feedback", () => {
  function configure() {
    vi.stubEnv("NEXT_PUBLIC_POSTHOG_FEEDBACK_SURVEY_ID", "survey");
    vi.stubEnv("NEXT_PUBLIC_POSTHOG_FEEDBACK_RATING_QUESTION_ID", "rating");
    vi.stubEnv("NEXT_PUBLIC_POSTHOG_FEEDBACK_COMMENT_QUESTION_ID", "comment");
  }

  it("snapshots the call and original identity after cleanup and submits the 1–5 rating once", async () => {
    configure();
    fetchMock.mockResolvedValueOnce(replyResponse("One", "thread", "", "one"))
      .mockResolvedValueOnce(replyResponse("Two", "thread", "", "two"))
      .mockResolvedValueOnce(replyResponse("Duplicate", "thread", "", "two"));
    const { result } = await setupCall();
    await utterance();
    mocks.distinct.mockReturnValue("changed"); mocks.session.mockReturnValue("changed");
    await utterance(); await utterance();
    act(() => result.current.hangUp());
    expect(mocks.stop).toHaveBeenCalledOnce();
    expect(result.current.messages).toEqual([]);
    const callId = captured("voice_call_started")[0]?.call_id;
    expect(callId).toEqual(expect.any(String));
    expect(result.current.feedback?.snapshot).toEqual({
      conversationId: "thread", callId, distinctId: "person", sessionId: "visit",
    });
    expect(captured("voice_call_ended")).toEqual([expect.objectContaining({
      call_id: callId, conversation_id: "thread", feedback_shown: true,
    })]);
    expect((fetchMock.mock.calls[1][1].body as FormData).get("session_id")).toBe("visit");
    act(() => {
      result.current.submitFeedback(4, "Good call");
      result.current.submitFeedback(5, "duplicate");
      result.current.hangUp();
    });
    const sent = captured("survey sent");
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      $survey_id: "survey", $survey_completed: true, $survey_response_rating: 4,
      $survey_response_comment: "Good call", $ai_trace_id: "thread", call_id: callId,
      $ai_session_id: "thread", $session_id: "visit", distinct_id: "person",
    });
    expect(mocks.capture.mock.calls.at(-1)?.[0]).toBe("survey sent");
    expect(captured("survey shown")).toHaveLength(0);
    expect(captured("voice_call_feedback_linked")).toHaveLength(0);
    expect(result.current.feedbackStatus).toBe("submitted");
    await act(async () => result.current.activate());
    expect(result.current.feedback).toBeNull();
    fetchMock.mockResolvedValue(replyResponse("New", "new", "", "new-trace"));
    await utterance();
    expect((fetchMock.mock.calls[3][1].body as FormData).get("conversation_id")).toBeNull();
    act(() => result.current.hangUp());
    expect(result.current.feedback?.snapshot.conversationId).toBe("new");
    expect(result.current.feedback?.snapshot.callId).not.toBe(callId);
  });

  it("offers no feedback for a call that never got past the greeting", async () => {
    configure();
    greetingMock.mockResolvedValue(replyResponse("Hi.", "thread", "", "greeting"));
    const { result } = await setupCall();
    await waitFor(() => expect(captured("voice_call_ai_replied")).toEqual([
      expect.objectContaining({ is_greeting: true, turn_index: 0, had_audio: false })]));
    act(() => result.current.hangUp());
    expect(result.current.feedback).toBeNull();
    expect(captured("voice_call_ended")[0]).toMatchObject({ feedback_shown: false, ai_replies: 1 });
  });

  it("retries a failed send and freezes the first answers", async () => {
    configure();
    fetchMock.mockResolvedValue(replyResponse("Reply", "thread", ""));
    const { result } = await setupCall(); await utterance();
    act(() => result.current.hangUp());
    mocks.capture.mockImplementation(() => { throw new Error("offline"); });
    act(() => result.current.submitFeedback(2, "Improve"));
    expect(result.current.feedbackStatus).toBe("error");
    mocks.capture.mockImplementation(() => ({ uuid: "accepted" }));
    act(() => result.current.submitFeedback(5, "changed"));
    expect(result.current.feedbackStatus).toBe("submitted");
    expect(mocks.capture.mock.calls.at(-1)?.[1]).toMatchObject({
      $survey_response_rating: 2, $survey_response_comment: "Improve",
    });
  });

  it.each(["missing config", "opt out", "no completed turns", "identity error"])("hides feedback for %s", async (reason) => {
    if (reason !== "missing config") configure();
    if (reason === "opt out") mocks.optedOut.mockReturnValue(true);
    if (reason === "identity error") mocks.distinct.mockImplementation(() => { throw new Error("identity"); });
    fetchMock.mockResolvedValue(replyResponse("Reply", "thread", ""));
    const { result } = await setupCall();
    if (reason !== "no completed turns") await utterance();
    act(() => result.current.hangUp());
    expect(result.current.feedback).toBeNull();
    expect(result.current.phase).toBe("idle");
  });

  it("offers feedback after an interrupted turn and allows skip/restart after analytics exceptions", async () => {
    configure();
    const stream = streamingResponse();
    fetchMock.mockResolvedValueOnce(replyResponse("Reply", "thread", "", "completed"))
      .mockResolvedValueOnce(stream.response);
    const { result } = await setupCall(); await utterance(); await utterance();
    act(() => result.current.activate());
    mocks.capture.mockImplementation(() => { throw new Error("analytics down"); });
    act(() => result.current.hangUp());
    expect(result.current.feedback?.snapshot.conversationId).toBe("thread");
    expect(result.current.feedbackStatus).toBe("ready");
    act(() => result.current.skipFeedback());
    expect(result.current.feedback).toBeNull();
    await act(async () => result.current.activate());
    expect(result.current.phase).toBe("listening");
    fetchMock.mockResolvedValue(replyResponse("New", "new", ""));
    await utterance();
    expect(result.current.phase).toBe("listening");
  });
});

describe("call session", () => {
  it("starts with one action, pauses detection through processing/playback, and drains in order", async () => {
    const stream = streamingResponse();
    fetchMock.mockResolvedValue(stream.response);
    const { result } = await setupCall();
    expect(result.current.phase).toBe("listening");
    expect(mocks.start).toHaveBeenCalledTimes(1);
    expect(mocks.options?.enabled).toBe(true);
    await utterance();
    expect(result.current.phase).toBe("processing");
    expect(mocks.options?.enabled).toBe(false);
    await act(async () => {
      stream.send({ type: "transcript", text: "Tell me about him" });
      stream.send({ type: "sentence", text: "First **sentence**.", audio: "first" });
      stream.send({ type: "sentence", text: "Second sentence.", audio: "second" });
    });
    expect(result.current.phase).toBe("speaking");
    expect(audios).toHaveLength(1);
    expect(result.current.messages).toEqual([
      { role: "me", text: "🎤 Tell me about him" },
      { role: "rep", text: "First **sentence**. Second sentence." },
    ]);
    act(() => audios[0].onended?.());
    expect(audios[1].src).toBe("data:audio/mpeg;base64,second");
    act(() => audios[1].onended?.());
    expect(result.current.phase).toBe("speaking"); // still waiting for done
    await act(async () => { stream.send({ type: "done", conversation_id: "thread" }); });
    expect(result.current.phase).toBe("listening");
    expect(mocks.options?.enabled).toBe(true);
  });

  it("waits for final playback after done, and threads conversation/person/session IDs", async () => {
    fetchMock.mockResolvedValueOnce(replyResponse()).mockResolvedValueOnce(replyResponse("Second", "conversation", ""));
    const { result } = await setupCall();
    await utterance();
    expect(result.current.phase).toBe("speaking");
    act(() => audios[0].onended?.());
    expect(result.current.phase).toBe("listening");
    await utterance();
    const first = fetchMock.mock.calls[0][1].body as FormData;
    const second = fetchMock.mock.calls[1][1].body as FormData;
    expect(fetchMock.mock.calls[0][0]).toBe("http://localhost:8000/turn");
    expect(first.get("audio")).toBeInstanceOf(Blob);
    expect(first.get("conversation_id")).toBeNull();
    expect(second.get("conversation_id")).toBe("conversation");
    expect(second.get("distinct_id")).toBe("person");
    expect(second.get("session_id")).toBe("visit");
    expect(result.current.phase).toBe("listening");
  });

  it("mutes capture without stopping reply audio and ignores utterances while muted", async () => {
    fetchMock.mockResolvedValue(replyResponse());
    const { result } = await setupCall();
    act(() => result.current.toggleMute());
    expect(result.current.muted).toBe(true);
    expect(mocks.options?.muted).toBe(true);
    await utterance();
    expect(fetchMock).not.toHaveBeenCalled();
    act(() => result.current.toggleMute());
    await utterance();
    act(() => result.current.toggleMute());
    expect(result.current.phase).toBe("speaking");
    expect(audios[0].pause).not.toHaveBeenCalled();
  });

  it("rejects duplicate detected utterances before React renders the new phase", async () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    await setupCall();
    act(() => {
      const callback = mocks.options!.onUtterance;
      callback(new Blob(["one"]));
      callback(new Blob(["two"]));
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("updates elapsed time and clears the timer and conversation on hang-up", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    fetchMock.mockResolvedValue(replyResponse("Reply", "thread", ""));
    const { result } = await setupCall();
    await utterance();
    act(() => vi.advanceTimersByTime(65_000));
    expect(result.current.elapsedSeconds).toBe(65);
    act(() => { result.current.toggleMute(); result.current.hangUp(); });
    expect(result.current).toMatchObject({ phase: "idle", muted: false, messages: [], elapsedSeconds: 0 });
    expect(mocks.stop).toHaveBeenCalledTimes(1);
    expect(captured("voice_call_ended")).toEqual([expect.objectContaining({
      conversation_id: "thread", end_reason: "hang_up", duration_seconds: 65,
      user_turns: 1, ai_replies: 1, failures: 1, // the default greeting fails, recoverably
    })]);
    act(() => vi.advanceTimersByTime(5_000));
    expect(result.current.elapsedSeconds).toBe(0);
    await act(async () => { result.current.activate(); });
    await utterance();
    expect((fetchMock.mock.calls[1][1].body as FormData).get("conversation_id")).toBeNull();
  });

  it("captures start, progress, failure and end with turn timings", async () => {
    let now = 1_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const stream = streamingResponse();
    fetchMock.mockResolvedValue(stream.response);
    const { result } = await setupCall();
    await utterance({ speechMs: 700, speechEndedAt: 900 });
    const form = fetchMock.mock.calls[0][1].body as FormData;
    const callId = form.get("call_id");
    expect(form.get("turn_index")).toBe("1");
    now = 1_020;
    await act(async () => { stream.send({ type: "transcript", text: "Hi" }); });
    now = 1_040;
    await act(async () => { stream.send({ type: "sentence", text: "Reply", audio: "first" }); });
    now = 1_300;
    await act(async () => { stream.send({ type: "done", conversation_id: "thread" }); });
    expect(captured("voice_call_ai_replied")).toHaveLength(0); // still playing
    now = 1_500;
    act(() => audios[0].onended?.());
    now = 5_000;
    act(() => result.current.hangUp());
    expect(mocks.capture.mock.calls).toEqual([
      ["voice_call_started", { call_id: callId }],
      ["voice_call_failed", {
        call_id: callId, conversation_id: null, turn_index: 0, stage: "connecting",
        reason: "greeting_failed", recoverable: true,
      }],
      ["voice_call_user_spoke", {
        call_id: callId, conversation_id: null, turn_index: 1, heard: true, speech_ms: 700,
        time_to_transcript_ms: 20,
      }],
      ["voice_call_ai_replied", {
        call_id: callId, conversation_id: "thread", turn_index: 1, is_greeting: false,
        time_to_first_audio_ms: 40, voice_to_voice_ms: 140, reply_ms: 460, sentences: 1,
        had_audio: true, fallback_reply: false, interrupted: false, interrupted_after_ms: null,
      }],
      ["voice_call_ended", {
        call_id: callId, conversation_id: "thread", end_reason: "hang_up", duration_seconds: 4,
        user_turns: 1, user_turns_not_heard: 0, ai_replies: 1, interruptions: 0, failures: 1,
        voice_to_voice_p50_ms: 140, voice_to_voice_max_ms: 140, feedback_shown: false,
      }],
    ]);
  });

  it("ends the call with a beacon when the page closes, once", async () => {
    fetchMock.mockResolvedValue(replyResponse("Reply", "thread", ""));
    const { result } = await setupCall();
    await utterance();
    act(() => { window.dispatchEvent(new Event("pagehide")); });
    act(() => result.current.hangUp());
    const ended = mocks.capture.mock.calls.filter(([event]) => event === "voice_call_ended");
    expect(ended).toHaveLength(1);
    expect(ended[0][1]).toMatchObject({ end_reason: "page_closed", user_turns: 1 });
    expect(ended[0][2]).toEqual({ transport: "sendBeacon" });
  });
});

describe("recovery and stale work", () => {
  it("barge-in stops playback and ignores an old completion and play rejection", async () => {
    const playing = deferred<void>();
    playResult = playing.promise;
    fetchMock.mockResolvedValue(replyResponse());
    const { result } = await setupCall();
    await utterance();
    const staleEnd = audios[0].onended;
    act(() => result.current.activate());
    expect(audios[0].pause).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("listening");
    await act(async () => { staleEnd?.(); playing.reject(new Error("old playback")); });
    expect(result.current.phase).toBe("listening");
    expect(result.current.messages).toHaveLength(2);
    expect(captured("voice_call_ai_replied")).toEqual([expect.objectContaining({
      turn_index: 1, interrupted: true, interrupted_after_ms: expect.any(Number),
    })]);
    expect(captured("voice_call_failed").filter((props) => props?.reason === "playback_failed"))
      .toHaveLength(0);
  });

  it("ignores late fetch resolution after hang-up and a newer call", async () => {
    const old = deferred<Response>();
    fetchMock.mockReturnValueOnce(old.promise).mockResolvedValueOnce(replyResponse("New reply", "new", ""));
    const { result } = await setupCall();
    await utterance();
    const oldSignal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    act(() => result.current.hangUp());
    expect(oldSignal.aborted).toBe(true);
    await act(async () => { result.current.activate(); });
    await utterance();
    await act(async () => { old.resolve(replyResponse("Old reply", "old")); });
    expect(result.current.phase).toBe("listening");
    expect(result.current.messages).toEqual([{ role: "me", text: "🎤 Question" }, { role: "rep", text: "New reply" }]);
    expect(captured("voice_call_ai_replied")).toHaveLength(1);
  });

  it("ignores late frames from a reader that does not honor abort, without clearing the newer request", async () => {
    const read = deferred<ReadableStreamReadResult<Uint8Array>>();
    const reader = { read: vi.fn(() => read.promise), cancel: vi.fn(async () => {}), releaseLock: vi.fn() };
    const newer = deferred<Response>();
    fetchMock.mockResolvedValueOnce({ ok: true, body: { getReader: () => reader } }).mockReturnValueOnce(newer.promise);
    const { result } = await setupCall();
    await utterance();
    act(() => result.current.activate());
    await utterance();
    const newSignal = fetchMock.mock.calls[1][1].signal as AbortSignal;
    await act(async () => read.resolve({ done: false, value: new TextEncoder().encode(
      'data: {"type":"sentence","text":"stale","audio":"old"}\n\ndata: {"type":"done","conversation_id":"old"}\n\n',
    ) }));
    expect(result.current.phase).toBe("processing");
    expect(result.current.messages.some((m) => m.text === "stale")).toBe(false);
    expect(audios).toHaveLength(0);
    act(() => result.current.hangUp());
    expect(newSignal.aborted).toBe(true);
    expect(reader.cancel).toHaveBeenCalled();
    expect(reader.releaseLock).toHaveBeenCalled();
  });

  it("hang-up and unmount reset playback, abort pending requests, stop capture, and ignore callbacks", async () => {
    fetchMock.mockResolvedValueOnce(replyResponse()).mockReturnValueOnce(new Promise(() => {}));
    const { result, unmount } = await setupCall();
    await utterance();
    const end = audios[0].onended;
    act(() => result.current.hangUp());
    act(() => end?.());
    expect(result.current.phase).toBe("idle");
    expect(audios[0].pause).toHaveBeenCalled();
    await act(async () => { result.current.activate(); });
    await utterance();
    const signal = fetchMock.mock.calls[1][1].signal as AbortSignal;
    const error = mocks.options!.onError;
    unmount();
    expect(signal.aborted).toBe(true);
    expect(mocks.stop).toHaveBeenCalledTimes(2);
    act(() => error("late"));
    // A reply cut off by hanging up is not an interruption, and late errors aren't failures.
    expect(captured("voice_call_ai_replied")).toHaveLength(0);
    expect(captured("voice_call_failed").map((props) => props?.reason)).toEqual(["greeting_failed", "greeting_failed"]);
  });

  it("handles streaming errors without a later done/drain overriding the error, then resumes capture", async () => {
    const stream = streamingResponse();
    stream.send({ type: "sentence", text: "Partial", audio: "first" });
    stream.send({ type: "error", text: "failed" });
    stream.send({ type: "done", conversation_id: "invalid" });
    fetchMock.mockResolvedValue(stream.response);
    const { result } = await setupCall();
    await utterance();
    expect(result.current.phase).toBe("error");
    expect(result.current.messages.at(-1)?.text).toMatch(/reply failed/);
    expect(captured("voice_call_failed").at(-1)).toMatchObject({
      reason: "reply_failed", stage: "ai_replying", recoverable: false, turn_index: 1,
    });
    expect(audios[0].pause).toHaveBeenCalled();
    act(() => audios[0].onended?.());
    expect(result.current.phase).toBe("error");
    act(() => result.current.activate());
    expect(result.current.phase).toBe("listening");
    expect(mocks.start).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["network", { reason: "network_error", stage: "user_speaking" }],
    ["status", { reason: "stt_failed", stage: "user_speaking", http_status: 502 }],
    ["missing body", { reason: "stream_ended" }],
    ["premature end", { reason: "stream_ended" }],
    ["invalid JSON", { reason: "stream_ended" }],
  ])("recovers from %s errors", async (kind, failure) => {
    if (kind === "network") fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    else if (kind === "status") fetchMock.mockResolvedValue(new Response("failed", { status: 502 }));
    else if (kind === "missing body") fetchMock.mockResolvedValue(new Response(null));
    else {
      const stream = streamingResponse();
      if (kind === "invalid JSON") stream.chunk("data: invalid\n\n");
      else stream.close();
      fetchMock.mockResolvedValue(stream.response);
    }
    const { result } = await setupCall();
    await utterance();
    await waitFor(() => expect(result.current.phase).toBe("error"));
    expect(result.current.messages.at(-1)?.text).toMatch(/connection error/);
    expect(captured("voice_call_failed").at(-1)).toMatchObject({ ...failure, recoverable: false });
    act(() => result.current.activate());
    expect(result.current.phase).toBe("listening");
    expect(mocks.start).toHaveBeenCalledTimes(1);
  });

  it("returns to listening, without an error, when nothing intelligible was heard", async () => {
    fetchMock.mockResolvedValue(new Response("could not hear anything", { status: 400 }));
    const { result } = await setupCall();
    await utterance();
    await waitFor(() => expect(result.current.phase).toBe("listening"));
    expect(result.current.messages).toEqual([]);
    expect(captured("voice_call_user_spoke")).toEqual([expect.objectContaining({
      turn_index: 1, heard: false, time_to_transcript_ms: null,
    })]);
    expect(captured("voice_call_ai_replied")).toHaveLength(0);
    fetchMock.mockResolvedValue(replyResponse());
    await utterance();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps detector diagnostics out of analytics", async () => {
    await setupCall();
    expect(mocks.options?.onEvent).toBeUndefined();
  });

  it("reports current playback rejection and can recover", async () => {
    const playing = deferred<void>();
    playResult = playing.promise;
    fetchMock.mockResolvedValue(replyResponse());
    const { result } = await setupCall();
    await utterance();
    await act(async () => playing.reject(new Error("blocked")));
    expect(result.current.phase).toBe("error");
    expect(result.current.messages.at(-1)?.text).toMatch(/couldn't play/);
    expect(captured("voice_call_failed").at(-1)).toMatchObject({
      reason: "playback_failed", stage: "ai_replying",
    });
    act(() => result.current.activate());
    expect(result.current.phase).toBe("listening");
  });

  it("retries permission acquisition after a microphone error, but reuses an existing stream", async () => {
    const { result } = await setupCall();
    act(() => { mocks.started = false; mocks.options?.onError("couldn't access the microphone"); });
    expect(result.current.phase).toBe("error");
    expect(captured("voice_call_failed").at(-1)).toMatchObject({
      reason: "mic_denied", stage: "user_speaking", recoverable: false,
    });
    await act(async () => result.current.activate());
    expect(mocks.start).toHaveBeenCalledTimes(2);
    expect(captured("voice_call_started")).toHaveLength(1); // resuming is the same call
    act(() => mocks.options?.onError("recording failed"));
    act(() => result.current.activate());
    expect(mocks.start).toHaveBeenCalledTimes(2);
    expect(result.current.phase).toBe("listening");
  });

  it("parses split CRLF/UTF-8 frames and cancels/releases the reader after done", async () => {
    const stream = streamingResponse();
    fetchMock.mockResolvedValue(stream.response);
    const { result } = await setupCall();
    await utterance();
    await act(async () => {
      stream.chunk('data: {"type":"transcript","text":"Alex"}\r');
      stream.chunk('\n\r\ndata: {"type":"sentence","text":"Hello", "audio":""}\r\n\r\n');
      stream.chunk('data: {"type":"done","conversation_id":"id"}\r\n\r\n');
    });
    expect(result.current.messages[0].text).toBe("🎤 Alex");
    expect(result.current.phase).toBe("listening");
    expect(stream.cancel).toHaveBeenCalled();
  });
});

describe("call greeting", () => {
  it("ignores late greeting failure after hang-up and a newer call", async () => {
    const read = deferred<ReadableStreamReadResult<Uint8Array>>();
    const reader = { read: vi.fn(() => read.promise), cancel: vi.fn(async () => {}), releaseLock: vi.fn() };
    greetingMock.mockResolvedValueOnce({ ok: true, body: { getReader: () => reader } });
    const { result } = await setupCall();
    act(() => result.current.hangUp());
    expect(result.current.phase).toBe("idle");
    greetingMock.mockResolvedValueOnce(greetingStream().response);
    await act(async () => result.current.activate());
    expect(result.current.phase).toBe("speaking");
    await act(async () => read.reject(new Error("old greeting failed")));
    expect(result.current.phase).toBe("speaking");
    expect(audios[0].pause).not.toHaveBeenCalled();
    expect(result.current.messages).toEqual([{ role: "rep", text: "Hi, I'm Alex's AI representative." }]);
    act(() => audios[0].onended?.());
    expect(result.current.phase).toBe("listening");
  });

  it.each(["premature EOF", "stream error"])("stops greeting playback and listens after %s", async (kind) => {
    const stream = streamingResponse();
    greetingMock.mockResolvedValue(stream.response);
    const { result } = await setupCall();
    await act(async () => {
      stream.send({ type: "sentence", text: "Welcome", audio: "first" });
      stream.send({ type: "sentence", text: "More", audio: "queued" });
    });
    expect(result.current.phase).toBe("speaking");
    const staleEnd = audios[0].onended;
    await act(async () => {
      if (kind === "premature EOF") stream.close();
      else stream.send({ type: "error", text: "failed" });
    });
    expect(audios[0].pause).toHaveBeenCalledOnce();
    expect(result.current.phase).toBe("listening");
    expect(mocks.options?.enabled).toBe(true);
    act(() => staleEnd?.());
    expect(audios).toHaveLength(1);
    expect(result.current.phase).toBe("listening");
  });

  function greetingStream(audio = "R1JFRVQ=", conversationId = "call") {
    const stream = streamingResponse();
    stream.send({ type: "sentence", text: "Hi, I'm Alex's AI representative.", audio });
    stream.send({ type: "done", conversation_id: conversationId, trace_id: conversationId });
    return stream;
  }

  it("speaks first, keeps the mic idle until it ends, then listens on the same call", async () => {
    greetingMock.mockResolvedValue(greetingStream().response);
    const { result } = await setupCall();
    expect(greetingMock.mock.calls[0][0]).toBe("http://localhost:8000/greeting");
    expect(JSON.parse(greetingMock.mock.calls[0][1].body)).toEqual({
      distinct_id: "person", session_id: "visit", call_id: expect.any(String),
    });
    expect(mocks.start).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("speaking");
    expect(mocks.options?.enabled).toBe(false); // the rep never hears itself
    expect(result.current.messages).toEqual([
      { role: "rep", text: "Hi, I'm Alex's AI representative." },
    ]);
    expect(audios[0].src).toBe("data:audio/mpeg;base64,R1JFRVQ=");
    act(() => audios[0].onended?.());
    expect(result.current.phase).toBe("listening");
    expect(mocks.options?.enabled).toBe(true);

    fetchMock.mockResolvedValueOnce(replyResponse("Answer.", "call", ""));
    await utterance();
    const body = fetchMock.mock.calls[0][1].body as FormData;
    expect(body.get("conversation_id")).toBe("call");
    expect(captured("voice_call_ai_replied")[0]).toMatchObject({
      conversation_id: "call", turn_index: 0, is_greeting: true, had_audio: true,
      interrupted: false, voice_to_voice_ms: null,
    });
  });

  it("falls straight through to listening when the greeting has no audio", async () => {
    greetingMock.mockResolvedValue(greetingStream("").response);
    const { result } = await setupCall();
    expect(result.current.phase).toBe("listening");
    expect(result.current.messages).toHaveLength(1);
    expect(audios).toHaveLength(0);
  });

  it.each(["offline", "status"])("does not open on an error when the greeting fails (%s)", async (kind) => {
    if (kind === "offline") greetingMock.mockRejectedValue(new Error("offline"));
    else greetingMock.mockResolvedValue(new Response("down", { status: 502 }));
    const { result } = await setupCall();
    expect(result.current.phase).toBe("listening");
    expect(result.current.messages).toEqual([]);
  });

  it("starts listening when the browser refuses to play the greeting", async () => {
    const playing = deferred<void>();
    playResult = playing.promise;
    greetingMock.mockResolvedValue(greetingStream().response);
    const { result } = await setupCall();
    await act(async () => playing.reject(new Error("autoplay blocked")));
    expect(result.current.phase).toBe("listening");
    expect(result.current.messages.every((m) => !/couldn't play/.test(m.text))).toBe(true);
    expect(captured("voice_call_failed")).toEqual([expect.objectContaining({
      reason: "playback_failed", stage: "connecting", recoverable: true,
    })]);
  });

  it("lets the caller barge in over the greeting", async () => {
    const stream = greetingStream();
    greetingMock.mockResolvedValue(stream.response);
    const { result } = await setupCall();
    expect(result.current.phase).toBe("speaking");
    act(() => result.current.activate());
    expect(audios[0].pause).toHaveBeenCalled();
    expect(result.current.phase).toBe("listening");
    expect(captured("voice_call_ai_replied")).toEqual([expect.objectContaining({
      is_greeting: true, interrupted: true,
    })]);
  });

  it("aborts a greeting that is still loading when the caller hangs up", async () => {
    greetingMock.mockReturnValue(new Promise(() => {}));
    const { result } = await setupCall();
    expect(result.current.phase).toBe("processing");
    const signal = greetingMock.mock.calls[0][1].signal as AbortSignal;
    act(() => result.current.hangUp());
    expect(signal.aborted).toBe(true);
    expect(result.current.phase).toBe("idle");
  });

  it("does not greet again when resuming a call after an error", async () => {
    greetingMock.mockResolvedValue(greetingStream("").response);
    const { result } = await setupCall();
    expect(greetingMock).toHaveBeenCalledTimes(1);
    act(() => { mocks.started = false; mocks.options?.onError("couldn't access the microphone"); });
    expect(result.current.phase).toBe("error");
    await act(async () => result.current.activate());
    expect(greetingMock).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("listening");
  });
});
