"use client";

import { useEffect, useRef } from "react";

export const VOICE_TIMING = {
  speechConfirmationMs: 150,
  silenceCompletionMs: 900,
  minimumSpeechMs: 300,
  silentRotationMs: 5_000,
  maximumUtteranceMs: 30_000,
} as const;

const SPEECH_RMS = 0.035;
// A burst under the minimum length still counts when it is this loud: it is a short word
// ("yes", "thanks", "bye"), not noise. Transcription decides; silence comes back as a 400.
const SHORT_WORD_RMS = 0.1;
const REARM_AFTER_DELIVERY_MS = 1_000;
// Three silent rotations (~15s) without any speech while "listening" is worth reporting.
const IDLE_REPORT_ROTATIONS = 3;

type Options = {
  enabled: boolean;
  muted: boolean;
  onUtterance: (audio: Blob) => void;
  onError: (message: string) => void;
  /** Detector diagnostics (why an utterance was dropped, how long we heard nothing). */
  onEvent?: (name: string, props: Record<string, unknown>) => void;
};

export type VoiceActivity = {
  start: () => Promise<void>;
  stop: () => void;
  isStarted: () => boolean;
};

const round = (value: number) => Math.round(value * 1000) / 1000;

type RecorderRun = {
  recorder: MediaRecorder;
  finish: (deliver: boolean) => void;
};

/** Persistent microphone session with automatic speech/silence turn detection. */
export function useVoiceActivity({ enabled, muted, onUtterance, onError, onEvent }: Options): VoiceActivity {
  const stream = useRef<MediaStream | null>(null);
  const context = useRef<AudioContext | null>(null);
  const analyser = useRef<AnalyserNode | null>(null);
  const frame = useRef<number | null>(null);
  const run = useRef<RecorderRun | null>(null);
  const session = useRef(0);
  const enabledRef = useRef(enabled);
  const mutedRef = useRef(muted);
  const callbacks = useRef({ onUtterance, onError, onEvent });
  callbacks.current = { onUtterance, onError, onEvent };
  // Silence seen since the last speech, so a stalled loop is observable from the server side.
  const idle = useRef({ rotations: 0, peakRms: 0, reported: false });

  function report(name: string, props: Record<string, unknown>) {
    try {
      callbacks.current.onEvent?.(name, {
        ...props,
        visibility: typeof document === "undefined" ? undefined : document.visibilityState,
      });
    } catch { /* Diagnostics must never interrupt listening. */ }
  }

  function resetIdle() {
    idle.current = { rotations: 0, peakRms: 0, reported: false };
  }

  function cancelFrame() {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
  }

  function stopRecorder(deliver = false) {
    cancelFrame();
    const current = run.current;
    run.current = null;
    if (current && current.recorder.state !== "inactive") current.finish(deliver);
  }

  function setTrackEnabled() {
    const shouldCapture = enabledRef.current && !mutedRef.current;
    stream.current?.getAudioTracks().forEach((track) => {
      track.enabled = shouldCapture;
    });
  }

  function beginRecorder() {
    if (!stream.current || !enabledRef.current || mutedRef.current || run.current) return;
    const [track] = stream.current.getAudioTracks();
    if (!track || track.readyState === "ended") {
      // The mic went away (device switch / permission change): re-acquire it.
      void reacquire();
      return;
    }
    if (context.current?.state === "suspended") void context.current.resume().catch(() => {});
    if (!analyser.current) return;

    const activeSession = session.current;
    const recorder = new MediaRecorder(stream.current);
    const chunks: Blob[] = [];
    const samples = new Uint8Array(analyser.current.fftSize);
    let deliver = false;
    let failed = false;
    let recorderStartedAt: number | null = null;
    let soundStartedAt: number | null = null;
    let speechStartedAt: number | null = null;
    let lastSpeechAt: number | null = null;
    let peakRms = 0;

    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    };
    recorder.onstop = () => {
      if (session.current !== activeSession) return;
      const rearm = () => {
        if (session.current !== activeSession) return;
        if (!failed && enabledRef.current && !mutedRef.current && !run.current) beginRecorder();
      };
      let delivered = false;
      if (deliver) {
        const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
        if (blob.size > 0) {
          delivered = true;
          callbacks.current.onUtterance(blob);
        }
      }
      if (!delivered) {
        rearm();
        return;
      }
      // A delivered utterance normally flips the call to "processing", which disables us.
      // If it was rejected instead we are still enabled: re-arm so the detector never dies
      // while the call keeps showing "listening".
      window.setTimeout(rearm, REARM_AFTER_DELIVERY_MS);
    };
    recorder.onerror = () => {
      if (session.current !== activeSession || run.current?.recorder !== recorder) return;
      failed = true;
      stopRecorder(false);
      callbacks.current.onError("microphone recording failed");
    };
    const finish = (shouldDeliver: boolean) => {
      deliver = shouldDeliver;
      recorder.stop();
    };
    run.current = { recorder, finish };
    recorder.start();

    const sample = (now: number) => {
      if (session.current !== activeSession || run.current?.recorder !== recorder) return;
      recorderStartedAt ??= now;
      analyser.current!.getByteTimeDomainData(samples);
      let energy = 0;
      for (const value of samples) {
        const centered = (value - 128) / 128;
        energy += centered * centered;
      }
      const rms = Math.sqrt(energy / samples.length);
      if (rms > peakRms) peakRms = rms;
      const speaking = rms >= SPEECH_RMS;

      if (speaking) {
        soundStartedAt ??= now;
        if (now - soundStartedAt >= VOICE_TIMING.speechConfirmationMs) {
          speechStartedAt ??= soundStartedAt;
          lastSpeechAt = now;
        }
      } else {
        soundStartedAt = null;
      }

      const speechMs =
        speechStartedAt === null || lastSpeechAt === null ? 0 : lastSpeechAt - speechStartedAt;
      const silenceMs = lastSpeechAt === null ? 0 : now - lastSpeechAt;
      if (
        speechStartedAt !== null &&
        speechMs >= VOICE_TIMING.minimumSpeechMs &&
        silenceMs >= VOICE_TIMING.silenceCompletionMs
      ) {
        resetIdle();
        report("voice_utterance_captured", {
          reason: "silence", speech_ms: Math.round(speechMs), peak_rms: round(peakRms),
        });
        stopRecorder(true);
        return;
      }
      if (
        speechStartedAt !== null &&
        now - speechStartedAt >= VOICE_TIMING.maximumUtteranceMs
      ) {
        resetIdle();
        report("voice_utterance_captured", {
          reason: "max_length", speech_ms: Math.round(now - speechStartedAt),
          peak_rms: round(peakRms),
        });
        stopRecorder(true);
        return;
      }
      if (
        speechStartedAt !== null &&
        speechMs < VOICE_TIMING.minimumSpeechMs &&
        silenceMs >= VOICE_TIMING.silenceCompletionMs
      ) {
        resetIdle();
        const word = peakRms >= SHORT_WORD_RMS;
        report(word ? "voice_utterance_captured" : "voice_utterance_discarded", {
          reason: word ? "short_word" : "too_short",
          speech_ms: Math.round(speechMs), peak_rms: round(peakRms),
        });
        stopRecorder(word);
        return;
      }
      if (
        speechStartedAt === null &&
        now - recorderStartedAt >= VOICE_TIMING.silentRotationMs
      ) {
        const state = idle.current;
        state.rotations += 1;
        state.peakRms = Math.max(state.peakRms, peakRms);
        if (state.rotations >= IDLE_REPORT_ROTATIONS && !state.reported) {
          state.reported = true;
          report("voice_listening_idle", {
            silent_seconds: Math.round((state.rotations * VOICE_TIMING.silentRotationMs) / 1000),
            peak_rms: round(state.peakRms),
            speech_threshold: SPEECH_RMS,
          });
        }
        stopRecorder(false);
        return;
      }
      frame.current = requestAnimationFrame(sample);
    };
    frame.current = requestAnimationFrame(sample);
  }

  async function reacquire() {
    const failure = () => callbacks.current.onError("couldn't access the microphone");
    const activeSession = session.current;
    try {
      stream.current?.getTracks().forEach((track) => track.stop());
      const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (session.current !== activeSession) {
        mic.getTracks().forEach((track) => track.stop());
        return;
      }
      const audioContext = context.current;
      if (!audioContext) return;
      const meter = audioContext.createAnalyser();
      meter.fftSize = 2048;
      audioContext.createMediaStreamSource(mic).connect(meter);
      stream.current = mic;
      analyser.current = meter;
      watchTrack(mic);
      setTrackEnabled();
      beginRecorder();
    } catch {
      if (session.current === activeSession) {
        stop();
        failure();
      }
    }
  }

  function watchTrack(mic: MediaStream) {
    mic.getAudioTracks().forEach((track) => {
      track.onended = () => {
        if (stream.current === mic && enabledRef.current) {
          stopRecorder(false);
          void reacquire();
        }
      };
    });
  }

  async function start() {
    if (stream.current) {
      setTrackEnabled();
      beginRecorder();
      return;
    }
    const activeSession = ++session.current;
    try {
      const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (session.current !== activeSession) {
        mic.getTracks().forEach((track) => track.stop());
        return;
      }
      const AudioContextClass = window.AudioContext;
      const audioContext = new AudioContextClass();
      const source = audioContext.createMediaStreamSource(mic);
      const meter = audioContext.createAnalyser();
      meter.fftSize = 2048;
      source.connect(meter);
      stream.current = mic;
      context.current = audioContext;
      analyser.current = meter;
      watchTrack(mic);
      audioContext.onstatechange = () => {
        if (audioContext.state === "suspended" && enabledRef.current) {
          void audioContext.resume().catch(() => {});
        }
      };
      if (audioContext.state === "suspended") await audioContext.resume();
      setTrackEnabled();
      beginRecorder();
    } catch {
      if (session.current === activeSession) {
        stop();
        callbacks.current.onError("couldn't access the microphone");
      }
    }
  }

  function stop() {
    session.current += 1;
    stopRecorder(false);
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    analyser.current = null;
    const audioContext = context.current;
    context.current = null;
    if (audioContext && audioContext.state !== "closed") void audioContext.close();
  }

  useEffect(() => {
    enabledRef.current = enabled;
    mutedRef.current = muted;
    setTrackEnabled();
    if (enabled && !muted) beginRecorder();
    else {
      stopRecorder(false);
      resetIdle();
    }
  }, [enabled, muted]);

  // Frame sampling pauses in a hidden tab; record when that happens while we should listen.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisibility = () => {
      if (enabledRef.current && !mutedRef.current) report("voice_listening_visibility", {});
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => stop, []);

  return { start, stop, isStarted: () => stream.current !== null };
}
