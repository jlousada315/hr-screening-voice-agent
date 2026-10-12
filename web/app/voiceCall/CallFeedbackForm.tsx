"use client";

import { useEffect, useRef, useState } from "react";
import type { Rating } from "./feedback";

export function CallFeedbackForm({ status, submit, skip, restart }: {
  status: "ready" | "submitted" | "error";
  submit: (rating: Rating, comment: string) => void;
  skip: () => void;
  restart: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const [rating, setRating] = useState<Rating | null>(null);
  const [comment, setComment] = useState("");
  const [attempted, setAttempted] = useState(false);
  useEffect(() => { heading.current?.focus(); }, [status === "submitted"]);

  return (
    <main className="precall-shell">
      <section className="feedback-card" aria-labelledby="feedback-title">
        <h1 id="feedback-title" tabIndex={-1} ref={heading}>
          {status === "submitted" ? "Thank you for your feedback" : "Was this call helpful?"}
        </h1>
        {status === "submitted" ? (
          <>
            <p role="status">Thanks for helping improve the AI Rep.</p>
            <button className="start-call" onClick={restart}>Start another call</button>
          </>
        ) : (
          <form onSubmit={(event) => {
            event.preventDefault();
            if (rating) { setAttempted(true); submit(rating, comment); }
          }}>
            <fieldset disabled={attempted}>
              <legend>Choose a rating (required)</legend>
              <div className="feedback-ratings">
                {(["Yes, helpful", "No, not helpful"] as const).map((choice, index) => (
                  <label key={choice}>
                    <input type="radio" name="helpful" value={choice} required
                      checked={rating === choice} onChange={() => setRating(choice)} />
                    <span aria-hidden="true">{index === 0 ? "👍" : "👎"}</span> {choice}
                  </label>
                ))}
              </div>
              <label htmlFor="feedback-comment">What could we improve? (optional)</label>
              <textarea id="feedback-comment" maxLength={2000} value={comment}
                onChange={(event) => setComment(event.target.value)} aria-describedby="comment-limit" />
              <p id="comment-limit">{comment.length}/2,000 characters</p>
            </fieldset>
            <p role="status" aria-live="polite">
              {status === "error" ? "We couldn’t send all your feedback. Try again, skip, or start another call." : ""}
            </p>
            <div className="feedback-actions">
              <button className="start-call" type="submit" disabled={!rating}>Submit</button>
              <button type="button" onClick={skip}>Skip</button>
              <button type="button" onClick={restart}>Start another call</button>
            </div>
          </form>
        )}
      </section>
    </main>
  );
}
