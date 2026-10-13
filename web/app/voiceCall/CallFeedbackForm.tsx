"use client";

import { useEffect, useRef, useState } from "react";
import { StarIcon } from "../icons";
import type { Rating } from "./feedback";

const RATINGS: readonly Rating[] = [1, 2, 3, 4, 5];
// The ends match the PostHog survey's scale labels (Poor → Excellent).
const RATING_WORDS = ["Poor", "Fair", "Okay", "Good", "Excellent"] as const;

export function CallFeedbackForm({ status, submit, skip, restart }: {
  status: "ready" | "submitted" | "error";
  submit: (rating: Rating, comment: string) => void;
  skip: () => void;
  restart: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const [rating, setRating] = useState<Rating | null>(null);
  const [hovered, setHovered] = useState<Rating | null>(null);
  const shown = hovered ?? rating;
  const [comment, setComment] = useState("");
  const [attempted, setAttempted] = useState(false);
  useEffect(() => { heading.current?.focus(); }, [status === "submitted"]);

  return (
    <main className="precall-shell">
      <section className="feedback-card" aria-labelledby="feedback-title">
        <h1 id="feedback-title" tabIndex={-1} ref={heading}>
          {status === "submitted" ? "Thank you for your feedback" : "How was your call?"}
        </h1>
        {status === "submitted" ? (
          <>
            <p role="status" className="feedback-subtitle">Thanks for helping improve the AI Rep.</p>
            <div className="feedback-actions">
              <button className="start-call" type="button" onClick={restart}>Start another call</button>
              <button type="button" className="ghost-button" onClick={skip}>Back to home</button>
            </div>
          </>
        ) : (
          <form onSubmit={(event) => {
            event.preventDefault();
            if (rating) { setAttempted(true); submit(rating, comment); }
          }}>
            <p className="feedback-subtitle">Your rating helps improve the AI Rep.</p>
            <fieldset disabled={attempted}>
              <legend className="visually-hidden">Rate the call from 1 to 5 stars (required)</legend>
              <div className="feedback-rating-row">
                <div className="feedback-ratings" onMouseLeave={() => setHovered(null)}>
                  {RATINGS.map((stars) => (
                    <label key={stars} aria-label={`${stars} star${stars === 1 ? "" : "s"}`}
                      className={shown !== null && stars <= shown ? "filled" : undefined}
                      onMouseEnter={() => setHovered(stars)}>
                      <input type="radio" name="rating" value={stars} required className="visually-hidden"
                        checked={rating === stars} onChange={() => setRating(stars)} />
                      <StarIcon />
                    </label>
                  ))}
                </div>
                <span className="feedback-rating-word" aria-hidden="true">
                  {shown === null ? "" : RATING_WORDS[shown - 1]}
                </span>
              </div>
              <label htmlFor="feedback-comment" className="feedback-label">Tell us more (optional)</label>
              <textarea id="feedback-comment" maxLength={2000} value={comment}
                placeholder="What went well, or what could be better?"
                onChange={(event) => setComment(event.target.value)} aria-describedby="comment-limit" />
              <p id="comment-limit" className="feedback-counter">{comment.length}/2,000 characters</p>
            </fieldset>
            <p role="status" aria-live="polite" className="feedback-error">
              {status === "error" ? "We couldn’t send all your feedback. Try again, skip, or start another call." : ""}
            </p>
            <div className="feedback-actions">
              <button className="start-call" type="submit" disabled={!rating}>Submit</button>
              <button type="button" className="ghost-button" onClick={skip}>Skip</button>
              <button type="button" className="ghost-button" onClick={restart}>Start another call</button>
            </div>
          </form>
        )}
      </section>
    </main>
  );
}
