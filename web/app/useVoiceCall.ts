"use client";

import { useEffect, useRef, useState } from "react";
import { usePostHog } from "posthog-js/react";

import { useAudioQueue } from "./voiceCall/useAudioQueue";
import { useVoiceActivity } from "./voiceCall/useVoiceActivity";
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
  | { type: "done"; conversation_id: string; trace_id: string }
  | { type: "error"; text: string };

async function readSSE(
  response: Response,
  signal: AbortSignal,
  onEvent: (event: TurnEvent) => void,
): Promise<void> {
  if (!response.ok) throw new Error(`Turn failed (${response.status})`);
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
  const request = useRef<AbortController | null>(null);
  const traces = useRef<string[]>([]);
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

  function capture(event: string, props?: Record<string, unknown>) {
    try {
      if (props) posthog?.capture(event, props);
      else posthog?.capture(event);
    } catch { /* Analytics must never interrupt the call. */ }
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
    if (event.trace_id && !traces.current.includes(event.trace_id)) {
      traces.current.push(event.trace_id);
    }
    audio.markDone();
  }

  const audio = useAudioQueue({
    onPlay: () => {
      if (mounted.current && active.current && phaseRef.current !== "error") transition("speaking");
    },
    onDrain: () => {
      greetingPlayback.current = false;
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
        audio.reset();
        if (mounted.current && active.current && phaseRef.current !== "error") {
          transition("listening");
        }
        return;
      }
      fail("(couldn't play the reply — tap to try again)");
    },
  });

  const voice = useVoiceActivity({
    enabled: phase === "listening",
    muted,
    onUtterance: (blob) => {
      if (
        !mounted.current || !active.current || mutedRef.current || phaseRef.current !== "listening"
      ) return;
      transition("processing");
      void sendAudio(blob);
    },
    onError: (message) => fail(`(${message})`),
    onEvent: (name, props) => capture(name, { ...props, conversation_id: conversationId.current }),
  });

  function interrupt() {
    greetingPlayback.current = false;
    ++generation.current;
    request.current?.abort();
    request.current = null;
    audio.reset();
  }

  function fail(message: string) {
    if (!mounted.current || !active.current) return;
    interrupt();
    transition("error");
    pushRep(message);
  }

  function activate() {
    if (!mounted.current) return;
    skipFeedback();
    if (phaseRef.current === "speaking" || phaseRef.current === "processing") {
      interrupt();
      transition("listening");
    } else if (phaseRef.current === "idle" || phaseRef.current === "error") {
      if (voice.isStarted()) transition("listening");
      else {
        interrupt();
        active.current = true;
        setStartedAt(Date.now());
        setElapsedSeconds(0);
        capture("voice_call_started");
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
    active.current = false;
    interrupt();
    voice.stop();
    if (
      conversationId.current && answeredTurns.current > 0 && feedbackConfig() &&
      analyticsAvailable(posthog) && identity.current?.distinctId && identity.current?.sessionId
    ) {
      try {
        const snapshot = new CallFeedback({
          conversationId: conversationId.current,
          traceIds: [...traces.current], ...identity.current,
        });
        feedbackRef.current = snapshot;
        submitted.current = false;
        setFeedbackStatus("ready");
        setFeedback(snapshot);
        try { snapshot.shown(posthog); } catch { setFeedbackStatus("error"); }
      } catch { /* Feedback availability must never prevent cleanup. */ }
    }
    traces.current = [];
    answeredTurns.current = 0;
    identity.current = null;
    capture("voice_call_ended", { conversation_id: conversationId.current });
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
    const started = Date.now();
    let firstAudioMs: number | null = null;
    const listenIfWaiting = () => {
      if (isCurrent() && phaseRef.current === "processing") transition("listening");
    };
    try {
      identity.current ??= browserIdentity(posthog);
      const response = await fetch(`${API_URL}/greeting`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          distinct_id: identity.current.distinctId,
          session_id: identity.current.sessionId,
        }),
        signal: controller.signal,
      });
      if (!isCurrent()) return;
      await readSSE(response, controller.signal, (event) => {
        if (!isCurrent()) return;
        if (event.type === "sentence") {
          appendRepSentence(event.text ?? "");
          if (event.audio) {
            firstAudioMs ??= Date.now() - started;
            greetingPlayback.current = true;
            audio.enqueue(event.audio);
          }
        } else if (event.type === "done") {
          recordDone(event);
        } else if (event.type === "error") {
          throw new Error(event.text ?? "greeting failed");
        }
      });
      if (!isCurrent()) return;
      capture("voice_call_greeting_played", {
        latency_ms: firstAudioMs ?? Date.now() - started,
        conversation_id: conversationId.current,
        had_audio: firstAudioMs !== null,
      });
    } catch {
      if (isCurrent()) {
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

  async function sendAudio(blob: Blob) {
    interrupt();
    const turnGeneration = generation.current;
    const controller = new AbortController();
    request.current = controller;
    const isCurrent = () => mounted.current && active.current &&
      turnGeneration === generation.current && !controller.signal.aborted;
    capture("chat_message_sent", { mode: "voice", conversation_id: conversationId.current });
    setMessages((current) => [...current, { role: "me", text: PENDING_CAPTION }]);
    const started = Date.now();
    let firstAudioMs: number | null = null;
    let hadAudio = false;
    try {
      const form = new FormData();
      form.append("audio", blob, "turn.webm");
      if (conversationId.current) form.append("conversation_id", conversationId.current);
      identity.current ??= browserIdentity(posthog);
      const distinctId = identity.current.distinctId;
      if (distinctId) form.append("distinct_id", distinctId);
      const sessionId = identity.current.sessionId;
      if (sessionId) form.append("session_id", sessionId);
      const response = await fetch(`${API_URL}/turn`, {
        method: "POST", body: form, signal: controller.signal,
      });
      if (!isCurrent()) return;
      if (response.status === 400) {
        // Nothing intelligible in the recording (or an empty upload): that is not a failure of
        // the call, so go back to listening instead of stopping detection behind an error.
        capture("voice_utterance_not_heard", { conversation_id: conversationId.current });
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
          setMessages((current) => {
            const callerIndex = current.findLastIndex((message) => message.role === "me");
            return current.map((message, index) =>
              index === callerIndex ? { role: "me", text: `🎤 ${event.text ?? ""}` } : message);
          });
        } else if (event.type === "sentence") {
          appendRepSentence(event.text ?? "");
          if (event.audio) {
            if (firstAudioMs === null) firstAudioMs = Date.now() - started;
            hadAudio = true;
            audio.enqueue(event.audio);
          }
        } else if (event.type === "error") {
          audio.reset();
          transition("error");
          pushRep("(the reply failed — tap to try again)");
        } else if (event.type === "done") {
          answeredTurns.current += 1;
          recordDone(event);
        }
      });
      if (!isCurrent()) return;
      capture("chat_reply_received", {
        mode: "voice",
        latency_ms: firstAudioMs ?? Date.now() - started,
        conversation_id: conversationId.current,
        had_audio: hadAudio,
      });
    } catch (error) {
      if (!isCurrent()) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      fail("(connection error — is the API running?)");
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
