import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useAudioQueue } from "./useAudioQueue";

type FakeAudioInstance = {
  src: string;
  onended: (() => void) | null;
  onpause: (() => void) | null;
  onerror: (() => void) | null;
  play: ReturnType<typeof vi.fn>;
  pause: ReturnType<typeof vi.fn>;
};

let audioInstances: FakeAudioInstance[];

beforeEach(() => {
  audioInstances = [];

  (global as Record<string, unknown>).Audio = class {
    onended: (() => void) | null = null;
    onpause: (() => void) | null = null;
    onerror: (() => void) | null = null;
    play = vi.fn();
    pause = vi.fn();

    constructor(public src: string) {
      audioInstances.push(this);
    }
  };
});

describe("useAudioQueue", () => {
  it("does not advance to the next clip when current playback is paused", () => {
    const onPlay = vi.fn();
    const { result } = renderHook(() => useAudioQueue({ onPlay, onDrain: vi.fn() }));

    act(() => {
      result.current.enqueue("first");
      result.current.enqueue("second");
    });

    expect(audioInstances).toHaveLength(1);
    expect(audioInstances[0].src).toContain("first");

    act(() => audioInstances[0].onpause?.());

    expect(audioInstances).toHaveLength(1);
    expect(onPlay).toHaveBeenCalledTimes(1);
  });

  it("starts exactly one subsequent clip for each ended event, in arrival order", () => {
    const onPlay = vi.fn();
    const { result } = renderHook(() => useAudioQueue({ onPlay, onDrain: vi.fn() }));

    act(() => {
      result.current.enqueue("first");
      result.current.enqueue("second");
      result.current.enqueue("third");
    });

    expect(audioInstances.map((audio) => audio.src)).toEqual([
      "data:audio/mpeg;base64,first",
    ]);

    act(() => audioInstances[0].onended?.());
    expect(audioInstances.map((audio) => audio.src)).toEqual([
      "data:audio/mpeg;base64,first",
      "data:audio/mpeg;base64,second",
    ]);

    act(() => audioInstances[1].onended?.());
    expect(audioInstances.map((audio) => audio.src)).toEqual([
      "data:audio/mpeg;base64,first",
      "data:audio/mpeg;base64,second",
      "data:audio/mpeg;base64,third",
    ]);
    expect(onPlay).toHaveBeenCalledTimes(3);
  });

  it("ignores duplicate completion callbacks from a stale audio instance", () => {
    const onPlay = vi.fn();
    const { result } = renderHook(() => useAudioQueue({ onPlay, onDrain: vi.fn() }));

    act(() => {
      result.current.enqueue("first");
      result.current.enqueue("second");
      result.current.enqueue("third");
    });

    const staleCompletion = audioInstances[0].onended;
    act(() => staleCompletion?.());
    expect(audioInstances).toHaveLength(2);

    act(() => staleCompletion?.());

    expect(audioInstances).toHaveLength(2);
    expect(audioInstances[1].src).toContain("second");
    expect(onPlay).toHaveBeenCalledTimes(2);
  });

  it("drains only after the final queued clip completes", () => {
    const onDrain = vi.fn();
    const { result } = renderHook(() => useAudioQueue({ onPlay: vi.fn(), onDrain }));

    act(() => {
      result.current.enqueue("first");
      result.current.enqueue("second");
      result.current.markDone();
    });
    expect(onDrain).not.toHaveBeenCalled();

    act(() => audioInstances[0].onended?.());
    expect(onDrain).not.toHaveBeenCalled();

    act(() => audioInstances[1].onended?.());
    expect(onDrain).toHaveBeenCalledTimes(1);
  });

  it("ignores an old play rejection after reset and newer playback", async () => {
    let reject!: (error: unknown) => void;
    const pending = new Promise<void>((_, fail) => { reject = fail; });
    const onError = vi.fn();
    const onDrain = vi.fn();
    let first = true;
    (global as Record<string, unknown>).Audio = class {
      onended = null; onpause = null; onerror = null;
      play = vi.fn(() => { if (first) { first = false; return pending; } });
      pause = vi.fn();
      constructor(public src: string) { audioInstances.push(this); }
    };
    const { result } = renderHook(() => useAudioQueue({ onPlay: vi.fn(), onDrain, onError }));
    act(() => result.current.enqueue("old"));
    const staleEnd = audioInstances[0].onended;
    act(() => { result.current.reset(); result.current.enqueue("new"); });
    await act(async () => { staleEnd?.(); reject(new Error("cancelled old clip")); });
    expect(onError).not.toHaveBeenCalled();
    expect(onDrain).not.toHaveBeenCalled();
    expect(audioInstances[1].pause).not.toHaveBeenCalled();
  });

  it("stops playback on unmount and ignores stale completion and decode failure", () => {
    const onDrain = vi.fn();
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useAudioQueue({ onPlay: vi.fn(), onDrain, onError }));
    act(() => result.current.enqueue("audio"));
    const ended = audioInstances[0].onended;
    const failed = audioInstances[0].onerror;
    unmount();
    act(() => { ended?.(); failed?.(); });
    expect(audioInstances[0].pause).toHaveBeenCalledTimes(1);
    expect(onDrain).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

});
