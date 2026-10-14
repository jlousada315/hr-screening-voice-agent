import type { PostHog } from "posthog-js";

export type CallIdentity = { distinctId?: string; sessionId?: string };
// One call is one trace: the backend uses the conversation id as the trace id.
export type FeedbackSnapshot = CallIdentity & { conversationId: string; callId: string };
export type Rating = 1 | 2 | 3 | 4 | 5;

export function feedbackConfig() {
  const surveyId = process.env.NEXT_PUBLIC_POSTHOG_FEEDBACK_SURVEY_ID?.trim();
  const ratingId = process.env.NEXT_PUBLIC_POSTHOG_FEEDBACK_RATING_QUESTION_ID?.trim();
  const commentId = process.env.NEXT_PUBLIC_POSTHOG_FEEDBACK_COMMENT_QUESTION_ID?.trim();
  return surveyId && ratingId && commentId && ratingId !== commentId
    ? { surveyId, ratingId, commentId } : null;
}

export function analyticsAvailable(client: PostHog | undefined): client is PostHog {
  try { return !!client && !client.has_opted_out_capturing(); } catch { return false; }
}

export function browserIdentity(client: PostHog | undefined): CallIdentity {
  try {
    if (!analyticsAvailable(client)) return {};
    return { distinctId: client.get_distinct_id(), sessionId: client.get_session_id() };
  } catch { return {}; }
}

export function feedbackProperties(snapshot: FeedbackSnapshot, submissionId: string) {
  return {
    distinct_id: snapshot.distinctId,
    $session_id: snapshot.sessionId,
    $ai_session_id: snapshot.conversationId,
    $ai_trace_id: snapshot.conversationId,
    conversation_id: snapshot.conversationId,
    call_id: snapshot.callId,
    $survey_submission_id: submissionId,
  };
}

/** The call's final event: one `survey sent` with the 1–5 rating and comment, sent once. */
export class CallFeedback {
  readonly submissionId = crypto.randomUUID();
  private accepted = false;
  private answers: { rating: Rating; comment: string } | null = null;
  private busy = false;
  readonly config = feedbackConfig();

  constructor(readonly snapshot: FeedbackSnapshot) {}

  submit(client: PostHog, rating: Rating, comment: string): boolean {
    if (this.busy || !this.config || comment.length > 2000) return false;
    if (this.accepted) return true;
    this.busy = true;
    // A retry resends the first answers, so a failed send can't be edited into another.
    this.answers ??= { rating, comment: comment.trim() };
    try {
      if (!analyticsAvailable(client)) throw new Error("Analytics unavailable");
      const result = client.capture("survey sent", {
        ...feedbackProperties(this.snapshot, this.submissionId),
        $survey_id: this.config.surveyId,
        $survey_completed: true,
        [`$survey_response_${this.config.ratingId}`]: this.answers.rating,
        [`$survey_response_${this.config.commentId}`]: this.answers.comment,
      });
      if (!result) throw new Error("Capture was not accepted");
      this.accepted = true;
      return true;
    } finally { this.busy = false; }
  }
}
