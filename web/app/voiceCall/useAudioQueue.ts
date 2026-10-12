"use client";

import { useEffect, useRef } from "react";

type Callbacks = {
  onPlay: () => void; // a sentence started playing (caller → "speaking")
  onDrain: () => void; // queue emptied after the stream was marked done
  onError?: () => void; // current playback failed
};

export type AudioQueue = {
  /** Queue a sentence's audio (base64 mp3), starting playback if nothing is playing. */
  enqueue: (b64: string) => void;
  /** Stop playback and drop anything queued. */
  stop: () => void;
  /** Stop + clear and un-mark `done` for a fresh turn (barge-in / new call). */
  reset: () => void;
  /** No more sentences are coming; settle to drained if already idle. */
  markDone: () => void;
};

/**
 * Plays a stream of sentence audio clips back-to-back, in order. Sentences arrive one at
 * a time over the turn stream; this queues their (independent) mp3 blobs and plays the
 * next when the current one ends, so a multi-sentence reply sounds continuous and the
 * call state transitions cleanly (`onPlay` on the first clip, `onDrain` when the last one
 * finishes and the stream is done). A private supporting hook for useVoiceCall.
 */
export function useAudioQueue({ onPlay, onDrain, onError }: Callbacks): AudioQueue {
  const current = useRef<HTMLAudioElement | null>(null);
  const queue = useRef<string[]>([]);
  const done = useRef(false);
  // Latest callbacks, so the imperative loop never closes over a stale render.
  const cbs = useRef<Callbacks>({ onPlay, onDrain, onError });
  cbs.current = { onPlay, onDrain, onError };

  /** Detach handlers and stop the current clip so it never bounces the call state. */
  function detach() {
    const a = current.current;
    if (a) {
      a.onended = null;
      a.onpause = null;
      a.onerror = null;
      try {
        a.pause();
      } catch {
        /* no-op under test mocks */
      }
      current.current = null;
    }
  }

  /** Advance only when the audio instance that actually finished is still current. */
  function complete(audio: HTMLAudioElement) {
    if (current.current !== audio) return;
    audio.onended = null;
    audio.onpause = null;
    audio.onerror = null;
    current.current = null;
    playNext();
  }

  function playNext() {
    const next = queue.current.shift();
    if (next === undefined) {
      current.current = null;
      if (done.current) cbs.current.onDrain();
      return;
    }
    const audio = new Audio(`data:audio/mpeg;base64,${next}`);
    current.current = audio;
    audio.onended = () => complete(audio);
    const failed = () => {
      // A failure from a reset/interrupted clip must not affect a newer turn.
      if (current.current !== audio) return;
      detach();
      queue.current = [];
      done.current = false;
      cbs.current.onError?.();
    };
    audio.onerror = failed;
    cbs.current.onPlay();
    try {
      const played = audio.play(); // a Promise in browsers; undefined under test mocks
      if (played && typeof played.then === "function") played.catch(failed);
    } catch {
      failed();
    }
  }

  useEffect(() => () => {
    detach();
    queue.current = [];
    done.current = false;
  }, []);

  return {
    enqueue(b64: string) {
      queue.current.push(b64);
      if (!current.current) playNext();
    },
    stop() {
      detach();
      queue.current = [];
    },
    reset() {
      detach();
      queue.current = [];
      done.current = false;
    },
    markDone() {
      done.current = true;
      if (!current.current && queue.current.length === 0) cbs.current.onDrain();
    },
  };
}
