import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Page from "./page";
import type { CallMessage, CallPhase } from "./useVoiceCall";

const call = vi.hoisted(() => ({
  phase: "idle" as CallPhase,
  muted: false,
  messages: [] as CallMessage[],
  elapsedSeconds: 0,
  activate: vi.fn(), toggleMute: vi.fn(), hangUp: vi.fn(),
}));
vi.mock("./useVoiceCall", () => ({ useVoiceCall: () => call }));
beforeEach(() => {
  call.phase = "idle"; call.muted = false; call.messages = []; call.elapsedSeconds = 0;
  call.activate.mockClear(); call.toggleMute.mockClear(); call.hangUp.mockClear();
});
afterEach(cleanup);

describe("call presentation", () => {
  it("discloses the AI call and microphone permission before activation", async () => {
    const user = userEvent.setup();
    render(<Page />);
    expect(screen.getByRole("heading", { name: "Alex Rivera" })).toBeInTheDocument();
    expect(screen.getByText("AI screening call")).toBeInTheDocument();
    expect(screen.getByText("Your browser will ask for microphone access.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start call" }));
    expect(call.activate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Hang up" })).toBeNull();
  });

  it("renders accessible controls, call duration and listening status", async () => {
    call.phase = "listening"; call.elapsedSeconds = 65;
    const user = userEvent.setup();
    render(<Page />);
    expect(screen.getByText("AI Screening Rep")).toBeInTheDocument();
    expect(screen.getByLabelText("Call duration 01 minutes 05 seconds")).toHaveTextContent("01:05");
    expect(screen.getByRole("button", { name: "Listening for speech" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Listening");
    expect(screen.getByRole("region", { name: "Latest exchange" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mute microphone" }));
    expect(call.toggleMute).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Hang up" }));
    expect(call.hangUp).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["processing", "Thinking", "Interrupt and listen"],
    ["speaking", "Speaking", "Interrupt and listen"],
    ["error", "Something went wrong", "Resume listening"],
  ] as const)("renders %s status with its accessible action", async (phase, status, action) => {
    call.phase = phase;
    const user = userEvent.setup();
    render(<Page />);
    expect(screen.getByRole("status")).toHaveTextContent(status);
    await user.click(screen.getByRole("button", { name: action }));
    expect(call.activate).toHaveBeenCalledTimes(1);
  });

  it("shows only the latest exchange and preserves Markdown captions", () => {
    call.phase = "speaking";
    call.messages = [
      { role: "me", text: "🎤 old question" }, { role: "rep", text: "Old reply" },
      { role: "me", text: "🎤 new question" },
      { role: "rep", text: "Uses **Python**. [Evidence](https://example.com)." },
    ];
    render(<Page />);
    expect(screen.getByText("new question")).toBeInTheDocument();
    expect(screen.getByText("Python").tagName).toBe("STRONG");
    expect(screen.getByRole("link", { name: "Evidence" })).toHaveAttribute("href", "https://example.com");
    expect(screen.queryByText("Old reply")).toBeNull();
    expect(screen.queryByText("old question")).toBeNull();
  });

  it("turns a bare URL in a caption into a clickable link", () => {
    call.phase = "speaking";
    call.messages = [
      { role: "me", text: "🎤 what's the calendar?" },
      { role: "rep", text: "The calendar link is https://cal.com/example/30min" },
    ];
    render(<Page />);
    expect(screen.getByRole("link", { name: "https://cal.com/example/30min" })).toHaveAttribute(
      "href",
      "https://cal.com/example/30min",
    );
  });

  it("marks caption links so they look clickable and open without ending the call", () => {
    call.phase = "speaking";
    call.messages = [
      { role: "me", text: "🎤 links?" },
      { role: "rep", text: "See [the article](https://example.com/a) or https://cal.com/example/30min." },
    ];
    render(<Page />);
    for (const name of ["the article", "https://cal.com/example/30min"]) {
      const link = screen.getByRole("link", { name });
      expect(link).toHaveClass("caption-link");
      expect(link).toHaveAttribute("target", "_blank");
      expect(link.getAttribute("rel")).toContain("noopener");
    }
  });

  it("renders muted control and status without showing internal latency", () => {
    call.phase = "listening"; call.muted = true;
    render(<Page />);
    expect(screen.getByRole("button", { name: "Unmute microphone" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status")).toHaveTextContent("Microphone muted");
    expect(screen.queryByText(/ms/)).toBeNull();
  });
});
