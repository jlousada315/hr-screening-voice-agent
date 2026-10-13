import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useVoiceActivity, VOICE_TIMING } from "./useVoiceActivity";

type FrameRequest = FrameRequestCallback;
let frames: FrameRequest[];
let level: "quiet" | "soft" | "speech";
let recorders: FakeMediaRecorder[];
let pendingStops: Array<() => void>;
let delayStops: boolean;
const track = { enabled: true, stop: vi.fn() };
const close = vi.fn(async () => undefined);
const resume = vi.fn(async () => undefined);
const getUserMedia = vi.fn();

class FakeMediaRecorder {
  state: RecordingState = "inactive";
  mimeType = "audio/webm";
  ondataavailable: ((event: BlobEvent) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(_stream: MediaStream) {
    recorders.push(this);
  }

  start() {
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    const complete = () => {
      this.ondataavailable?.({ data: new Blob(["audio"]) } as BlobEvent);
      this.onstop?.();
    };
    if (delayStops) pendingStops.push(complete);
    else complete();
  }
}

function runFrame(time: number) {
  const callback = frames.shift();
  if (!callback) throw new Error("no animation frame was scheduled");
  act(() => callback(time));
}

function setupHook(overrides: { enabled?: boolean; muted?: boolean } = {}) {
  const onUtterance = vi.fn();
  const onError = vi.fn();
  const onEvent = vi.fn();
  const initial = { enabled: true, muted: false, ...overrides };
  const hook = renderHook(
    ({ enabled, muted }) => useVoiceActivity({ enabled, muted, onUtterance, onError, onEvent }),
    { initialProps: initial },
  );
  return { ...hook, onUtterance, onError, onEvent };
}

beforeEach(() => {
  frames = [];
  level = "quiet";
  recorders = [];
  pendingStops = [];
  delayStops = false;
  track.enabled = true;
  track.stop.mockClear();
  close.mockClear();
  resume.mockClear();
  getUserMedia.mockReset().mockResolvedValue({
    getTracks: () => [track],
    getAudioTracks: () => [track],
  });

  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequest) => {
    frames.push(callback);
    return frames.length;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
  Object.defineProperty(global.navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  vi.stubGlobal(
    "AudioContext",
    class {
      state: AudioContextState = "suspended";
      createMediaStreamSource() {
        return { connect: vi.fn() };
      }
      createAnalyser() {
        return {
          fftSize: 0,
          getByteTimeDomainData: (data: Uint8Array) =>
            data.fill(level === "speech" ? 150 : level === "soft" ? 135 : 128),
        };
      }
      resume = resume;
      close = close;
    },
  );
});

describe("useVoiceActivity", () => {
  it("opens one microphone stream and keeps it across detection pauses", async () => {
    const { result, rerender } = setupHook();
    await act(() => result.current.start());
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(recorders).toHaveLength(1);

    rerender({ enabled: false, muted: false });
    expect(track.stop).not.toHaveBeenCalled();
    rerender({ enabled: true, muted: false });
    expect(recorders).toHaveLength(2);
    await act(() => result.current.start());
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  it("confirms speech, enforces minimum speech, and completes after silence", async () => {
    const { result, onUtterance } = setupHook();
    await act(() => result.current.start());
    level = "speech";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs);
    runFrame(VOICE_TIMING.minimumSpeechMs);
    level = "quiet";
    runFrame(VOICE_TIMING.minimumSpeechMs + VOICE_TIMING.silenceCompletionMs);

    expect(onUtterance).toHaveBeenCalledTimes(1);
    expect(onUtterance.mock.calls[0][0]).toBeInstanceOf(Blob);
  });

  it("ignores unconfirmed noise and rotates a silent recorder every five seconds", async () => {
    const { result, onUtterance } = setupHook();
    await act(() => result.current.start());
    level = "speech";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs - 1);
    level = "quiet";
    runFrame(VOICE_TIMING.silentRotationMs);

    expect(onUtterance).not.toHaveBeenCalled();
    expect(recorders).toHaveLength(2);
  });

  it("discards confirmed speech shorter than the minimum utterance", async () => {
    const { result, onUtterance } = setupHook();
    await act(() => result.current.start());
    level = "soft";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs);
    level = "quiet";
    runFrame(VOICE_TIMING.speechConfirmationMs + VOICE_TIMING.silenceCompletionMs);
    expect(onUtterance).not.toHaveBeenCalled();
    expect(recorders).toHaveLength(2);
  });

  it("reports a captured utterance with its speech length and level", async () => {
    const { result, onEvent } = setupHook();
    await act(() => result.current.start());
    level = "speech";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs);
    runFrame(VOICE_TIMING.minimumSpeechMs);
    level = "quiet";
    runFrame(VOICE_TIMING.minimumSpeechMs + VOICE_TIMING.silenceCompletionMs);
    expect(onEvent).toHaveBeenCalledWith("voice_utterance_captured", expect.objectContaining({
      reason: "silence", speech_ms: VOICE_TIMING.minimumSpeechMs, peak_rms: expect.any(Number),
    }));
  });

  it("still sends a short but loud burst, which is a word rather than noise", async () => {
    const { result, onUtterance, onEvent } = setupHook();
    await act(() => result.current.start());
    level = "speech";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs);
    level = "quiet";
    runFrame(VOICE_TIMING.speechConfirmationMs + VOICE_TIMING.silenceCompletionMs);
    expect(onUtterance).toHaveBeenCalledTimes(1);
    expect(onEvent).toHaveBeenCalledWith("voice_utterance_captured", expect.objectContaining({
      reason: "short_word",
    }));
  });

  it("reports why a too-short utterance was dropped", async () => {
    const { result, onEvent } = setupHook();
    await act(() => result.current.start());
    level = "soft";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs);
    level = "quiet";
    runFrame(VOICE_TIMING.speechConfirmationMs + VOICE_TIMING.silenceCompletionMs);
    expect(onEvent).toHaveBeenCalledWith("voice_utterance_discarded", expect.objectContaining({
      reason: "too_short",
    }));
  });

  it("reports sustained silence once, with the loudest level it heard", async () => {
    const { result, onEvent } = setupHook();
    await act(() => result.current.start());
    for (let rotation = 0; rotation < 4; rotation += 1) {
      const base = rotation * (VOICE_TIMING.silentRotationMs + 1_000);
      runFrame(base);
      runFrame(base + VOICE_TIMING.silentRotationMs);
    }
    const idle = onEvent.mock.calls.filter(([name]) => name === "voice_listening_idle");
    expect(idle).toHaveLength(1);
    expect(idle[0][1]).toMatchObject({ silent_seconds: 15, peak_rms: 0, speech_threshold: 0.035 });
  });

  it("reports when the tab visibility changes while it should be listening", async () => {
    const { result, onEvent } = setupHook();
    await act(() => result.current.start());
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(onEvent).toHaveBeenCalledWith(
      "voice_listening_visibility", expect.objectContaining({ visibility: "visible" }));
  });

  it("caps a continuous utterance at thirty seconds", async () => {
    const { result, onUtterance } = setupHook();
    await act(() => result.current.start());
    level = "speech";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs);
    runFrame(VOICE_TIMING.maximumUtteranceMs);
    expect(onUtterance).toHaveBeenCalledTimes(1);
  });

  it("mutes the microphone track and suspends recording", async () => {
    const { result, rerender } = setupHook();
    await act(() => result.current.start());
    rerender({ enabled: true, muted: true });
    expect(track.enabled).toBe(false);
    expect(recorders[0].state).toBe("inactive");
    rerender({ enabled: true, muted: false });
    expect(track.enabled).toBe(true);
    expect(recorders).toHaveLength(2);
  });

  it("guards recorder callbacks from a stopped session", async () => {
    delayStops = true;
    const { result, onUtterance } = setupHook();
    await act(() => result.current.start());
    level = "speech";
    runFrame(0);
    runFrame(VOICE_TIMING.speechConfirmationMs);
    runFrame(VOICE_TIMING.maximumUtteranceMs);
    act(() => result.current.stop());
    act(() => pendingStops.forEach((complete) => complete()));
    expect(onUtterance).not.toHaveBeenCalled();
  });

  it("stops tracks and closes AudioContext on hang-up", async () => {
    const { result } = setupHook();
    await act(() => result.current.start());
    act(() => result.current.stop());
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);
    expect(result.current.isStarted()).toBe(false);
  });

  it("reports permission errors and remains restartable", async () => {
    getUserMedia.mockRejectedValueOnce(new Error("denied"));
    const { result, onError } = setupHook();
    await act(() => result.current.start());
    expect(onError).toHaveBeenCalledWith("couldn't access the microphone");
    expect(result.current.isStarted()).toBe(false);
  });

  it("reports recorder failures without releasing the reusable stream", async () => {
    const { result, onError } = setupHook();
    await act(() => result.current.start());
    act(() => recorders[0].onerror?.(new Event("error")));
    expect(onError).toHaveBeenCalledWith("microphone recording failed");
    expect(result.current.isStarted()).toBe(true);
  });

  it("releases a microphone acquired after hang-up without creating an audio context", async () => {
    let acquire!: (stream: unknown) => void;
    getUserMedia.mockReturnValueOnce(new Promise((resolve) => { acquire = resolve; }));
    const { result, onError } = setupHook();
    let starting!: Promise<void>;
    act(() => { starting = result.current.start(); });
    act(() => result.current.stop());
    await act(async () => {
      acquire({ getTracks: () => [track], getAudioTracks: () => [track] });
      await starting;
    });
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(result.current.isStarted()).toBe(false);
    expect(recorders).toHaveLength(0);
    expect(resume).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("ignores an old permission rejection after unmount", async () => {
    let reject!: (error: unknown) => void;
    getUserMedia.mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
    const { result, unmount, onError } = setupHook();
    let starting!: Promise<void>;
    act(() => { starting = result.current.start(); });
    unmount();
    await act(async () => { reject(new Error("old permission failure")); await starting; });
    expect(onError).not.toHaveBeenCalled();
  });


  it("ignores a recorder error from an earlier turn on the same microphone session", async () => {
    const { result, rerender, onError } = setupHook();
    await act(() => result.current.start());
    const oldError = recorders[0].onerror;
    rerender({ enabled: false, muted: false });
    rerender({ enabled: true, muted: false });
    act(() => oldError?.(new Event("error")));
    expect(onError).not.toHaveBeenCalled();
    expect(recorders[1].state).toBe("recording");
    expect(track.stop).not.toHaveBeenCalled();
  });

});

describe("useVoiceActivity recovery", () => {
  it("re-arms when a delivered utterance is rejected and the call stays listening", async () => {
    const { result, onUtterance } = setupHook();
    await act(() => result.current.start());
    expect(recorders).toHaveLength(1);

    // Leave requestAnimationFrame to the stub above; frames are driven by runFrame.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      level = "speech";
      runFrame(0);
      runFrame(VOICE_TIMING.speechConfirmationMs);
      runFrame(VOICE_TIMING.minimumSpeechMs);
      level = "quiet";
      runFrame(VOICE_TIMING.minimumSpeechMs + VOICE_TIMING.silenceCompletionMs);
      expect(onUtterance).toHaveBeenCalledTimes(1);

      // The consumer ignored it (enabled never flipped), so the detector must not stay dead.
      act(() => {
        vi.advanceTimersByTime(1_000);
      });
      expect(recorders).toHaveLength(2);
      expect(recorders[1].state).toBe("recording");
    } finally {
      vi.useRealTimers();
    }
  });

  it("resumes a suspended audio context whenever it re-arms", async () => {
    const { result, rerender } = setupHook();
    await act(() => result.current.start());
    resume.mockClear();
    rerender({ enabled: false, muted: false });
    rerender({ enabled: true, muted: false });
    expect(resume).toHaveBeenCalled();
  });
});
