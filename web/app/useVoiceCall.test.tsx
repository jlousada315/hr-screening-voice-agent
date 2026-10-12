import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useVoiceCall } from "./useVoiceCall";

const mocks = vi.hoisted(() => ({
  options: null as null | {
    enabled: boolean; muted: boolean;
    onUtterance: (audio: Blob) => void; onError: (message: string) => void;
    onEvent?: (name: string, props: Record<string, unknown>) => void;
  },
  started: false,
  start: vi.fn(async () => { mocks.started = true; }),
  stop: vi.fn(() => { mocks.started = false; }),
  capture: vi.fn((_event: string, _props?: Record<string, unknown>) => ({ uuid: "accepted" })),
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
let fetchMock: ReturnType<typeof vi.fn>;
let greetingMock: ReturnType<typeof vi.fn>;
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

async function utterance() {
  await act(async () => { mocks.options?.onUtterance(new Blob(["speech"], { type: "audio/webm" })); });
}

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

  it("preserves ordered unique traces and original identity after cleanup and submits once", async () => {
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
    expect(result.current.feedback?.snapshot).toEqual({
      conversationId: "thread", traceIds: ["one", "two"], distinctId: "person", sessionId: "visit",
    });
    expect((fetchMock.mock.calls[1][1].body as FormData).get("session_id")).toBe("visit");
    act(() => {
      result.current.submitFeedback("Yes, helpful", "Good call");
      result.current.submitFeedback("Yes, helpful", "duplicate");
      result.current.hangUp();
    });
    const sent = mocks.capture.mock.calls.filter(([event]) => event === "survey sent");
    expect(sent).toHaveLength(1);
    expect(sent[0][1]).toMatchObject({
      $survey_id: "survey", $survey_completed: true, $survey_response_rating: "Yes, helpful",
      $survey_response_comment: "Good call", $ai_trace_id: "two", ai_trace_ids: ["one", "two"],
      $ai_session_id: "thread", $session_id: "visit", distinct_id: "person",
    });
    expect(mocks.capture.mock.calls.filter(([event]) => event === "survey shown")).toHaveLength(1);
    const linked = mocks.capture.mock.calls.filter(([event]) => event === "voice_call_feedback_linked");
    expect(linked.map(([, props]) => props?.$ai_trace_id)).toEqual(["one", "two"]);
    expect(linked[0][1]?.$survey_submission_id).toBe(sent[0][1]?.$survey_submission_id);
    expect(result.current.feedbackStatus).toBe("submitted");
    await act(async () => result.current.activate());
    expect(result.current.feedback).toBeNull();
    fetchMock.mockResolvedValue(replyResponse("New", "new", "", "new-trace"));
    await utterance();
    expect((fetchMock.mock.calls[3][1].body as FormData).get("conversation_id")).toBeNull();
    act(() => result.current.hangUp());
    expect(result.current.feedback?.snapshot.traceIds).toEqual(["new-trace"]);
  });

  it("offers no feedback for a call that never got past the greeting", async () => {
    configure();
    greetingMock.mockResolvedValue(replyResponse("Hi.", "thread", "", "greeting"));
    const { result } = await setupCall();
    await waitFor(() => expect(mocks.capture).toHaveBeenCalledWith(
      "voice_call_greeting_played", expect.anything()));
    act(() => result.current.hangUp());
    expect(result.current.feedback).toBeNull();
    expect(mocks.capture.mock.calls.some(([event]) => event === "survey shown")).toBe(false);
  });

  it("retries only unaccepted events and freezes answers after partial capture failure", async () => {
    configure();
    fetchMock.mockResolvedValue(replyResponse("Reply", "thread", ""));
    const { result } = await setupCall(); await utterance();
    act(() => result.current.hangUp());
    mocks.capture.mockImplementation((event) => {
      if (event === "voice_call_feedback_linked") throw new Error("offline");
      return { uuid: "accepted" };
    });
    act(() => result.current.submitFeedback("No, not helpful", "Improve"));
    expect(result.current.feedbackStatus).toBe("error");
    mocks.capture.mockImplementation(() => ({ uuid: "accepted" }));
    act(() => result.current.submitFeedback("Yes, helpful", "changed"));
    expect(result.current.feedbackStatus).toBe("submitted");
    expect(mocks.capture.mock.calls.filter(([event]) => event === "survey sent")).toHaveLength(1);
    expect(mocks.capture.mock.calls.at(-1)?.[1]?.rating).toBe("No, not helpful");
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

  it("excludes interrupted completions and allows skip/restart after analytics exceptions", async () => {
    configure();
    const stream = streamingResponse();
    fetchMock.mockResolvedValueOnce(replyResponse("Reply", "thread", "", "completed"))
      .mockResolvedValueOnce(stream.response);
    const { result } = await setupCall(); await utterance(); await utterance();
    act(() => result.current.activate());
    mocks.capture.mockImplementation(() => { throw new Error("analytics down"); });
    act(() => result.current.hangUp());
    expect(result.current.feedback?.snapshot.traceIds).toEqual(["completed"]);
    expect(result.current.feedbackStatus).toBe("error");
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
    expect(mocks.capture).toHaveBeenCalledWith("voice_call_ended", { conversation_id: "thread" });
    act(() => vi.advanceTimersByTime(5_000));
    expect(result.current.elapsedSeconds).toBe(0);
    await act(async () => { result.current.activate(); });
    await utterance();
    expect((fetchMock.mock.calls[1][1].body as FormData).get("conversation_id")).toBeNull();
  });

  it("preserves analytics names/properties and measures first audio, rather than done", async () => {
    let now = 1_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const stream = streamingResponse();
    fetchMock.mockResolvedValue(stream.response);
    await setupCall();
    await utterance();
    now = 1_040;
    await act(async () => { stream.send({ type: "sentence", text: "Reply", audio: "first" }); });
    now = 1_300;
    await act(async () => { stream.send({ type: "done", conversation_id: "thread" }); });
    expect(mocks.capture.mock.calls).toEqual([
      ["voice_call_started"],
      ["chat_message_sent", { mode: "voice", conversation_id: null }],
      ["chat_reply_received", { mode: "voice", latency_ms: 40, conversation_id: "thread", had_audio: true }],
    ]);
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
    expect(mocks.capture.mock.calls.filter(([name]) => name === "chat_reply_received")).toHaveLength(1);
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
    expect(mocks.capture.mock.calls.filter(([name]) => name === "chat_reply_received")).toHaveLength(1);
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
    expect(audios[0].pause).toHaveBeenCalled();
    act(() => audios[0].onended?.());
    expect(result.current.phase).toBe("error");
    act(() => result.current.activate());
    expect(result.current.phase).toBe("listening");
    expect(mocks.start).toHaveBeenCalledTimes(1);
  });

  it.each(["network", "status", "missing body", "premature end", "invalid JSON"])("recovers from %s errors", async (kind) => {
    if (kind === "network") fetchMock.mockRejectedValue(new Error("offline"));
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
    expect(mocks.capture).toHaveBeenCalledWith("voice_utterance_not_heard", { conversation_id: null });
    fetchMock.mockResolvedValue(replyResponse());
    await utterance();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("forwards detector diagnostics to analytics with the conversation id", async () => {
    const { result } = await setupCall();
    act(() => mocks.options?.onEvent?.("voice_listening_idle", { peak_rms: 0.01 }));
    expect(mocks.capture).toHaveBeenCalledWith(
      "voice_listening_idle", { peak_rms: 0.01, conversation_id: null });
    expect(result.current.phase).toBe("listening");
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
    act(() => result.current.activate());
    expect(result.current.phase).toBe("listening");
  });

  it("retries permission acquisition after a microphone error, but reuses an existing stream", async () => {
    const { result } = await setupCall();
    act(() => { mocks.started = false; mocks.options?.onError("couldn't access the microphone"); });
    expect(result.current.phase).toBe("error");
    await act(async () => result.current.activate());
    expect(mocks.start).toHaveBeenCalledTimes(2);
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
      distinct_id: "person", session_id: "visit",
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
    expect(mocks.capture).toHaveBeenCalledWith(
      "voice_call_greeting_played",
      expect.objectContaining({ conversation_id: "call", had_audio: true }),
    );
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
  });

  it("lets the caller barge in over the greeting", async () => {
    const stream = greetingStream();
    greetingMock.mockResolvedValue(stream.response);
    const { result } = await setupCall();
    expect(result.current.phase).toBe("speaking");
    act(() => result.current.activate());
    expect(audios[0].pause).toHaveBeenCalled();
    expect(result.current.phase).toBe("listening");
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
