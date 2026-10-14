"use client";

import { useEffect, useRef, useState } from "react";
import { usePostHog } from "posthog-js/react";

import { useAudioQueue } from "./voiceCall/useAudioQueue";
import { useVoiceActivity, type UtteranceTiming } from "./voiceCall/useVoiceActivity";
import {
  analyticsAvailable, browserIdentity, CallFeedback, feedbackConfig,
  type CallIdentity, type Rating,
} from "./voiceCall/feedback";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const PENDING_CAPTION = "🎤 …";

export type CallPhase = "idle" | "listening" | "processing" | "speaking" | "error";
export type CallMessage = { role: "me" | "rep"; text: string };
type VoiceCall = {
  phase: CallPhase;
  muted: boolean;
  messages: readonly CallMessage[];
  elapsedSeconds: number;
  activate: () => void;
  toggleMute: () => void;
  hangUp: () => void;
  feedback: CallFeedback | null;
  feedbackStatus: "ready" | "submitted" | "error";
  submitFeedback: (rating: Rating, comment: string) => void;
  skipFeedback: () => void;
};

// Wire parsing stays private to the call; presentation sees only call state/actions.
type TurnEvent =
  | { type: "transcript" | "token"; text: string }
  | { type: "sentence"; text: string; audio: string }
  | { type: "done"; conversation_id: string; trace_id: string; fallback?: boolean }
  | { type: "error"; text: string };

class TurnHttpError extends Error {
  constructor(readonly status: number) { super(`Turn failed (${status})`); }
}

// Analytics vocabulary for the call: start → progress (user spoke / AI replied) → failure → end.
type FailureReason =
  | "mic_denied" | "mic_lost" | "playback_failed" | "network_error" | "stt_failed"
  | "server_error" | "reply_failed" | "stream_ended" | "greeting_failed";
type FailureStage = "connecting" | "user_speaking" | "ai_replying";
type EndReason = "hang_up" | "after_error" | "page_closed";

/** One AI reply in progress: the greeting (turn 0) or the answer to user turn N. */
type Reply = {
  turnIndex: number;
  isGreeting: boolean;
  startedAt: number;
  speechMs: number | null;
  speechEndedAt: number | null;
  transcribed: boolean;
  firstAudioAt: number | null;
  firstPlayAt: number | null;
  sentences: number;
  fallback: boolean;
};

type CallStats = {
  startedAt: number;
  userTurns: number;
  notHeard: number;
  aiReplies: number;
  interruptions: number;
  failures: number;
  voiceToVoice: number[];
  endSent: boolean;
};

const newStats = (): CallStats => ({
  startedAt: Date.now(), userTurns: 0, notHeard: 0, aiReplies: 0, interruptions: 0,
  failures: 0, voiceToVoice: [], endSent: false,
});

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : Math.round((sorted[middle - 1] + sorted[middle]) / 2);
}

function failureReason(error: unknown): FailureReason {
  if (error instanceof TurnHttpError) return error.status === 502 ? "stt_failed" : "server_error";
  if (error instanceof TypeError) return "network_error";
  return "stream_ended";
}

async function readSSE(
  response: Response,
  signal: AbortSignal,
  onEvent: (event: TurnEvent) => void,
): Promise<void> {
  if (!response.ok) throw new TurnHttpError(response.status);
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Missing turn stream");
  const cancel = () => {
    void reader.cancel?.().catch(() => {});
  };
  signal.addEventListener("abort", cancel, { once: true });
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      if (signal.aborted) return;
      const { done, value } = await reader.read();
      if (signal.aborted) return;
      if (done) throw new Error("Turn stream ended before a terminal event");
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
      let separator: number;
      while ((separator = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        const data = frame.split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        if (signal.aborted) return;
        const event = JSON.parse(data) as TurnEvent;
        onEvent(event);
        if (event.type === "done" || event.type === "error") return;
      }
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    cancel();
    reader.releaseLock?.();
  }
}

/** Own the microphone session, streamed turns, playback, timing and analytics. */
export function useVoiceCall(): VoiceCall {
  const posthog = usePostHog();
  const [phase, setPhase] = useState<CallPhase>("idle");
  const [muted, setMuted] = useState(false);
  const [messages, setMessages] = useState<CallMessage[]>([]);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const phaseRef = useRef<CallPhase>("idle");
  const mutedRef = useRef(false);
  const active = useRef(false);
  const mounted = useRef(true);
  const generation = useRef(0);
  const conversationId = useRef<string | null>(null);
  const callId = useRef<string | null>(null);
  const turnIndex = useRef(0);
  const reply = useRef<Reply | null>(null);
  const stats = useRef<CallStats>(newStats());
  const request = useRef<AbortController | null>(null);
  // Replies completed for the caller; the greeting alone is not a call worth rating.
  const answeredTurns = useRef(0);
  const identity = useRef<CallIdentity | null>(null);
  const [feedback, setFeedback] = useState<CallFeedback | null>(null);
  const feedbackRef = useRef<CallFeedback | null>(null);
  const [feedbackStatus, setFeedbackStatus] = useState<"ready" | "submitted" | "error">("ready");
  const submitted = useRef(false);
  // The greeting is a courtesy: if the browser refuses to play it the call should still
  // start listening instead of opening on an error.
  const greetingPlayback = useRef(false);

  function capture(event: string, props: Record<string, unknown>, beacon = false) {
    try {
      if (beacon) posthog?.capture(event, props, { transport: "sendBeacon" });
      else posthog?.capture(event, props);
    } catch { /* Analytics must never interrupt the call. */ }
  }

  function callProps() {
    return { call_id: callId.current, conversation_id: conversationId.current };
  }

  /** Cache an identity only once PostHog has one, so an early turn can't pin an empty one. */
  function currentIdentity(): CallIdentity {
    if (!identity.current?.distinctId) identity.current = browserIdentity(posthog);
    return identity.current;
  }

  function startReply(fields: Pick<Reply, "turnIndex" | "isGreeting"> & Partial<Reply>) {
    reply.current = {
      startedAt: Date.now(), speechMs: null, speechEndedAt: null, transcribed: false,
      firstAudioAt: null, firstPlayAt: null, sentences: 0, fallback: false, ...fields,
    };
  }

  /** Progress: the caller's utterance was transcribed (heard) or rejected as silence. */
  function userSpoke(heard: boolean) {
    const current = reply.current;
    if (!current || current.isGreeting || current.transcribed) return;
    current.transcribed = true;
    stats.current.userTurns += 1;
    if (!heard) stats.current.notHeard += 1;
    capture("voice_call_user_spoke", {
      ...callProps(),
      turn_index: current.turnIndex,
      heard,
      speech_ms: current.speechMs,
      time_to_transcript_ms: heard ? Date.now() - current.startedAt : null,
    });
  }

  /** Progress: the AI reply finished playing, or the caller cut it short. */
  function aiReplied(interrupted: boolean) {
    const current = reply.current;
    if (!current) return;
    reply.current = null;
    const now = Date.now();
    const voiceToVoice = current.speechEndedAt !== null && current.firstPlayAt !== null
      ? current.firstPlayAt - current.speechEndedAt : null;
    stats.current.aiReplies += 1;
    if (interrupted) stats.current.interruptions += 1;
    if (voiceToVoice !== null) stats.current.voiceToVoice.push(voiceToVoice);
    capture("voice_call_ai_replied", {
      ...callProps(),
      turn_index: current.turnIndex,
      is_greeting: current.isGreeting,
      time_to_first_audio_ms:
        current.firstAudioAt === null ? null : current.firstAudioAt - current.startedAt,
      voice_to_voice_ms: voiceToVoice,
      reply_ms: current.firstPlayAt === null ? null : now - current.firstPlayAt,
      sentences: current.sentences,
      had_audio: current.firstAudioAt !== null,
      fallback_reply: current.fallback,
      interrupted,
      interrupted_after_ms: interrupted ? now - current.startedAt : null,
    });
  }

  /** Failure. `recoverable` means the call carried on by itself, with no tap needed. */
  function recordFailure(
    reason: FailureReason, recoverable: boolean, extra: Record<string, unknown> = {},
  ) {
    const current = reply.current;
    const stage: FailureStage =
      reason === "mic_denied" || reason === "mic_lost" || reason === "stt_failed" ? "user_speaking"
        : current?.isGreeting || reason === "greeting_failed" ? "connecting"
          : reason === "reply_failed" || reason === "playback_failed" || current?.transcribed
            ? "ai_replying" : "user_speaking";
    reply.current = null;
    stats.current.failures += 1;
    capture("voice_call_failed", {
      ...callProps(), turn_index: turnIndex.current, stage, reason, recoverable, ...extra,
    });
  }

  /** End: one summary per call, from hang-up or the page closing. */
  function recordEnd(endReason: EndReason, feedbackShown: boolean) {
    const call = stats.current;
    if (!callId.current || call.endSent) return;
    call.endSent = true;
    capture("voice_call_ended", {
      ...callProps(),
      end_reason: endReason,
      duration_seconds: Math.round((Date.now() - call.startedAt) / 1000),
      user_turns: call.userTurns,
      user_turns_not_heard: call.notHeard,
      ai_replies: call.aiReplies,
      interruptions: call.interruptions,
      failures: call.failures,
      voice_to_voice_p50_ms: median(call.voiceToVoice),
      voice_to_voice_max_ms: call.voiceToVoice.length ? Math.max(...call.voiceToVoice) : null,
      feedback_shown: feedbackShown,
    }, endReason === "page_closed");
  }

  function skipFeedback() {
    feedbackRef.current = null;
    setFeedback(null);
  }

  function submitFeedback(rating: Rating, comment: string) {
    const current = feedbackRef.current;
    if (!current || submitted.current || !posthog) return;
    submitted.current = true;
    try {
      if (!current.submit(posthog, rating, comment)) throw new Error("Capture unavailable");
      setFeedbackStatus("submitted");
    } catch {
      submitted.current = false;
      setFeedbackStatus("error");
    }
  }

  function transition(next: CallPhase) {
    phaseRef.current = next;
    setPhase(next);
  }

  function pushRep(text: string) {
    setMessages((current) => [...current, { role: "rep", text }]);
  }

  // Sentences of one reply are shown as a single caption.
  function appendRepSentence(text: string) {
    setMessages((current) => {
      const last = current[current.length - 1];
      if (last?.role === "rep") {
        return [...current.slice(0, -1), { role: "rep", text: `${last.text} ${text}`.trim() }];
      }
      return [...current, { role: "rep", text }];
    });
  }

  function recordDone(event: Extract<TurnEvent, { type: "done" }>) {
    conversationId.current = event.conversation_id ?? conversationId.current;
    if (reply.current && event.fallback) reply.current.fallback = true;
    audio.markDone();
  }

  const audio = useAudioQueue({
    onPlay: () => {
      if (reply.current) reply.current.firstPlayAt ??= Date.now();
      if (mounted.current && active.current && phaseRef.current !== "error") transition("speaking");
    },
    onDrain: () => {
      greetingPlayback.current = false;
      if (mounted.current && active.current) aiReplied(false);
      if (
        mounted.current && active.current &&
        (phaseRef.current === "speaking" || phaseRef.current === "processing")
      ) {
        transition("listening");
      }
    },
    onError: () => {
      if (greetingPlayback.current) {
        greetingPlayback.current = false;
        if (mounted.current && active.current) recordFailure("playback_failed", true);
        audio.reset();
        if (mounted.current && active.current && phaseRef.current !== "error") {
          transition("listening");
        }
        return;
      }
      fail("(couldn't play the reply — tap to try again)", "playback_failed");
    },
  });

  const voice = useVoiceActivity({
    enabled: phase === "listening",
    muted,
    onUtterance: (blob, timing) => {
      if (
        !mounted.current || !active.current || mutedRef.current || phaseRef.current !== "listening"
      ) return;
      transition("processing");
      void sendAudio(blob, timing);
    },
    onError: (message) =>
      fail(`(${message})`, /access/.test(message) ? "mic_denied" : "mic_lost"),
  });

  function interrupt() {
    greetingPlayback.current = false;
    ++generation.current;
    request.current?.abort();
    request.current = null;
    audio.reset();
  }

  function fail(message: string, reason: FailureReason, extra?: Record<string, unknown>) {
    if (!mounted.current || !active.current) return;
    recordFailure(reason, false, extra);
    interrupt();
    transition("error");
    pushRep(message);
  }

  function activate() {
    if (!mounted.current) return;
    skipFeedback();
    if (phaseRef.current === "speaking" || phaseRef.current === "processing") {
      aiReplied(true);
      interrupt();
      transition("listening");
    } else if (phaseRef.current === "idle" || phaseRef.current === "error") {
      if (voice.isStarted()) transition("listening");
      else {
        interrupt();
        active.current = true;
        setStartedAt(Date.now());
        setElapsedSeconds(0);
        // Resuming after an error (mic re-acquired) continues the same call.
        if (!callId.current) {
          callId.current = crypto.randomUUID();
          stats.current = newStats();
          turnIndex.current = 0;
          capture("voice_call_started", { call_id: callId.current });
        }
        // A call already in progress (mic re-acquired after an error) just listens again;
        // a new call opens with the rep's greeting, and listening begins when it ends.
        if (conversationId.current) transition("listening");
        else {
          transition("processing");
          void greet();
        }
        // VAD owns delayed acquisition and releases it if hang-up wins the race.
        void voice.start();
      }
    }
  }

  function toggleMute() {
    if (!active.current) return;
    mutedRef.current = !mutedRef.current;
    setMuted(mutedRef.current);
  }

  function hangUp() {
    if (!mounted.current) return;
    if (!active.current) return;
    const endReason: EndReason = phaseRef.current === "error" ? "after_error" : "hang_up";
    active.current = false;
    reply.current = null;
    interrupt();
    voice.stop();
    let feedbackShown = false;
    if (
      conversationId.current && callId.current && answeredTurns.current > 0 && feedbackConfig() &&
      analyticsAvailable(posthog) && identity.current?.distinctId && identity.current?.sessionId
    ) {
      try {
        const snapshot = new CallFeedback({
          conversationId: conversationId.current, callId: callId.current, ...identity.current,
        });
        feedbackRef.current = snapshot;
        submitted.current = false;
        setFeedbackStatus("ready");
        setFeedback(snapshot);
        feedbackShown = true;
      } catch { /* Feedback availability must never prevent cleanup. */ }
    }
    recordEnd(endReason, feedbackShown);
    answeredTurns.current = 0;
    identity.current = null;
    callId.current = null;
    conversationId.current = null;
    setMessages([]);
    setStartedAt(null);
    setElapsedSeconds(0);
    mutedRef.current = false;
    setMuted(false);
    transition("idle");
  }

  useEffect(() => {
    if (startedAt === null) return;
    const update = () => setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  // Closing the tab mid-call still ends the call in analytics; a beacon survives the unload.
  useEffect(() => {
    const onPageHide = () => {
      if (active.current) recordEnd("page_closed", false);
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      active.current = false;
      interrupt();
      voice.stop();
    };
  }, []);

  /** The rep speaks first. Any failure just starts the call listening, as before. */
  async function greet() {
    const turnGeneration = generation.current;
    const controller = new AbortController();
    request.current = controller;
    const isCurrent = () => mounted.current && active.current &&
      turnGeneration === generation.current && !controller.signal.aborted;
    const listenIfWaiting = () => {
      if (isCurrent() && phaseRef.current === "processing") transition("listening");
    };
    startReply({ turnIndex: 0, isGreeting: true });
    try {
      const identity = currentIdentity();
      const response = await fetch(`${API_URL}/greeting`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          distinct_id: identity.distinctId,
          session_id: identity.sessionId,
          call_id: callId.current,
        }),
        signal: controller.signal,
      });
      if (!isCurrent()) return;
      await readSSE(response, controller.signal, (event) => {
        if (!isCurrent()) return;
        if (event.type === "sentence") {
          appendRepSentence(event.text ?? "");
          if (reply.current) reply.current.sentences += 1;
          if (event.audio) {
            if (reply.current) reply.current.firstAudioAt ??= Date.now();
            greetingPlayback.current = true;
            audio.enqueue(event.audio);
          }
        } else if (event.type === "done") {
          recordDone(event);
        } else if (event.type === "error") {
          throw new Error(event.text ?? "greeting failed");
        }
      });
    } catch {
      if (isCurrent()) {
        recordFailure("greeting_failed", true);
        greetingPlayback.current = false;
        audio.reset();
        transition("listening");
      }
    } finally {
      if (request.current === controller) request.current = null;
      // Audio that is still queued starts listening on drain; otherwise listen now.
      listenIfWaiting();
    }
  }

  async function sendAudio(blob: Blob, timing?: UtteranceTiming) {
    interrupt();
    const turnGeneration = generation.current;
    const controller = new AbortController();
    request.current = controller;
    const isCurrent = () => mounted.current && active.current &&
      turnGeneration === generation.current && !controller.signal.aborted;
    turnIndex.current += 1;
    startReply({
      turnIndex: turnIndex.current, isGreeting: false,
      speechMs: timing?.speechMs ?? null, speechEndedAt: timing?.speechEndedAt ?? null,
    });
    setMessages((current) => [...current, { role: "me", text: PENDING_CAPTION }]);
    try {
      const form = new FormData();
      form.append("audio", blob, "turn.webm");
      if (conversationId.current) form.append("conversation_id", conversationId.current);
      const { distinctId, sessionId } = currentIdentity();
      if (distinctId) form.append("distinct_id", distinctId);
      if (sessionId) form.append("session_id", sessionId);
      if (callId.current) form.append("call_id", callId.current);
      form.append("turn_index", String(turnIndex.current));
      const response = await fetch(`${API_URL}/turn`, {
        method: "POST", body: form, signal: controller.signal,
      });
      if (!isCurrent()) return;
      if (response.status === 400) {
        // Nothing intelligible in the recording (or an empty upload): that is not a failure of
        // the call, so go back to listening instead of stopping detection behind an error.
        userSpoke(false);
        reply.current = null;
        setMessages((current) => {
          const index = current.findLastIndex(
            (message) => message.role === "me" && message.text === PENDING_CAPTION,
          );
          return index === -1 ? current : current.filter((_, i) => i !== index);
        });
        transition("listening");
        return;
      }
      await readSSE(response, controller.signal, (event) => {
        if (!isCurrent()) return;
        if (event.type === "transcript") {
          userSpoke(true);
          setMessages((current) => {
            const callerIndex = current.findLastIndex((message) => message.role === "me");
            return current.map((message, index) =>
              index === callerIndex ? { role: "me", text: `🎤 ${event.text ?? ""}` } : message);
          });
        } else if (event.type === "sentence") {
          appendRepSentence(event.text ?? "");
          if (reply.current) {
            reply.current.sentences += 1;
            if (event.audio) reply.current.firstAudioAt ??= Date.now();
          }
          if (event.audio) audio.enqueue(event.audio);
        } else if (event.type === "error") {
          recordFailure("reply_failed", false);
          audio.reset();
          transition("error");
          pushRep("(the reply failed — tap to try again)");
        } else if (event.type === "done") {
          userSpoke(true);
          answeredTurns.current += 1;
          recordDone(event);
        }
      });
    } catch (error) {
      if (!isCurrent()) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      fail(
        "(connection error — is the API running?)", failureReason(error),
        error instanceof TurnHttpError ? { http_status: error.status } : {},
      );
    } finally {
      if (request.current === controller) request.current = null;
    }
  }

  return {
    phase, muted, messages, elapsedSeconds, activate, toggleMute, hangUp,
    feedback: analyticsAvailable(posthog) ? feedback : null,
    feedbackStatus, submitFeedback, skipFeedback,
  };
}
