"use client";

import { useEffect, useRef } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CallFeedbackForm } from "./voiceCall/CallFeedbackForm";
import { MicIcon, MutedMicIcon, PhoneOffIcon, Spinner, WaveIcon } from "./icons";
import { useVoiceCall, type CallPhase } from "./useVoiceCall";

// Who the rep represents, from the owner file (see next.config.mjs).
const OWNER_NAME = process.env.OWNER_NAME ?? "";
const OWNER_SHORT_NAME = process.env.OWNER_SHORT_NAME ?? "";
const OWNER_INITIALS = process.env.OWNER_INITIALS ?? "";
const OWNER_TAGLINE = process.env.OWNER_TAGLINE ?? "";
const [FIRST_NAME, ...OTHER_NAMES] = OWNER_NAME.split(" ");

const STATUS: Record<CallPhase, string> = {
  idle: "Start the call when you're ready",
  listening: "Listening…",
  processing: "Thinking…",
  speaking: "Speaking… tap to interrupt",
  error: "Something went wrong — tap to resume",
};

// The global reset strips anchor styling and a plain click would leave the page (ending the call),
// so caption links are styled and always open in a new tab.
const CAPTION_COMPONENTS: Components = {
  a: ({ node: _node, children, ...props }) => (
    <a {...props} className="caption-link" target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

export default function Page() {
  const {
    phase: callState, muted, messages, elapsedSeconds, activate, toggleMute, hangUp,
    feedback, feedbackStatus, submitFeedback, skipFeedback,
  } = useVoiceCall();
  const transcriptRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const visualContent =
    callState === "processing" ? (
      <Spinner />
    ) : callState === "speaking" ? (
      <WaveIcon />
    ) : (
      <MicIcon />
    );
  const orbLabel =
    callState === "idle"
      ? "Start call"
      : callState === "speaking" || callState === "processing"
        ? "Interrupt and listen"
        : callState === "error"
          ? "Resume listening"
          : "Listening for speech";

  const minutes = Math.floor(elapsedSeconds / 60).toString().padStart(2, "0");
  const seconds = (elapsedSeconds % 60).toString().padStart(2, "0");
  const lastCallerIndex = messages.findLastIndex((message) => message.role === "me");
  const latestExchange = lastCallerIndex >= 0 ? messages.slice(lastCallerIndex) : messages.slice(-1);

  if (callState === "idle" && feedback) {
    return (
      <CallFeedbackForm key={feedback.submissionId} status={feedbackStatus}
        submit={submitFeedback} skip={skipFeedback} restart={activate} />
    );
  }

  if (callState === "idle") {
    return (
      <main className="precall-shell">
        <div className="precall-glow" aria-hidden="true" />
        <section className="precall-card" aria-labelledby="precall-title">
          <div className="precall-copy">
            <div className="precall-mark" aria-hidden="true">{OWNER_INITIALS}</div>
            <div>
              <p className="call-kicker">{OWNER_TAGLINE}</p>
              <h1 id="precall-title">
                <span>{FIRST_NAME}</span>
                {OTHER_NAMES.length > 0 && <>{" "}<span>{OTHER_NAMES.join(" ")}</span></>}
              </h1>
              <p className="precall-role">Talk with {OWNER_SHORT_NAME}&apos;s AI Rep</p>
            </div>
          </div>

          <div className="precall-action">
            <div className="monogram-visual" aria-hidden="true">
              <span className="monogram-orbit orbit-outer" />
              <span className="monogram-orbit orbit-inner" />
              <span className="monogram-core">{OWNER_INITIALS}</span>
              <span className="voice-tick tick-one" />
              <span className="voice-tick tick-two" />
              <span className="voice-tick tick-three" />
            </div>
            <button type="button" className="start-call" onClick={activate}>
              <MicIcon />
              Start call
            </button>
            <p className="permission-note">Your browser will ask for microphone access.</p>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="phone-call">
      <section className="call-console">
        <header className="call-header">
          <div className="avatar" aria-hidden="true">{OWNER_INITIALS}</div>
          <div className="call-identity">
            <h1>{OWNER_NAME}</h1>
            <p>AI Screening Rep</p>
          </div>
          <time className="call-time" aria-label={`Call duration ${minutes} minutes ${seconds} seconds`}>
            {minutes}:{seconds}
          </time>
        </header>

        <section className="call-body" aria-label="Active voice call">
          <div className="voice-column">
            <div className={`voice-visual ${callState}`}>
              <div className="voice-ring ring-one" aria-hidden="true" />
              <div className="voice-ring ring-two" aria-hidden="true" />
              <button
                type="button"
                className="voice-core"
                onClick={activate}
                aria-label={orbLabel}
                disabled={callState === "listening"}
              >
                {visualContent}
              </button>
            </div>

            <p className="call-status" role="status">
              {muted && callState === "listening" ? "Microphone muted" : STATUS[callState]}
            </p>
          </div>

          <section className="latest-exchange" aria-label="Latest exchange" ref={transcriptRef}>
            {latestExchange.length > 0 ? (
              latestExchange.map((message, index) =>
                message.role === "me" ? (
                  <div key={index} className="exchange-line caller-line">
                    <span>You</span>
                    <p>{message.text.replace(/^🎤\s*/, "")}</p>
                  </div>
                ) : (
                  <div key={index} className="exchange-line rep-line">
                    <span>AI Rep</span>
                    <div><ReactMarkdown remarkPlugins={[remarkGfm]} components={CAPTION_COMPONENTS}>
                      {message.text}
                    </ReactMarkdown></div>
                  </div>
                ),
              )
            ) : (
              <p className="exchange-empty">Ask about {OWNER_SHORT_NAME}&apos;s experience, skills, or availability.</p>
            )}
          </section>
        </section>

        <footer className="call-controls" aria-label="Call controls">
          <div className="control-group">
            <button
              type="button"
              className={`round-control mic-control ${muted ? "active" : ""}`}
              aria-label={muted ? "Unmute microphone" : "Mute microphone"}
              aria-pressed={muted}
              onClick={toggleMute}
            >
              <MutedMicIcon muted={muted} />
            </button>
            <span>{muted ? "Unmute" : "Mute"}</span>
          </div>
          <div className="control-group">
            <button type="button" className="round-control hangup-control" onClick={hangUp} aria-label="Hang up">
              <PhoneOffIcon />
            </button>
            <span>End</span>
          </div>
        </footer>
      </section>
    </main>
  );
}
