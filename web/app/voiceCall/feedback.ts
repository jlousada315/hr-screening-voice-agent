import type { PostHog } from "posthog-js";

export type CallIdentity = { distinctId?: string; sessionId?: string };
export type FeedbackSnapshot = CallIdentity & { conversationId: string; traceIds: string[] };
export type Rating = "Yes, helpful" | "No, not helpful";

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
    conversation_id: snapshot.conversationId,
    $ai_trace_id: snapshot.traceIds.at(-1),
    ai_trace_ids: snapshot.traceIds,
    $survey_submission_id: submissionId,
  };
}

/** A call-scoped ledger: retries only enqueue events the SDK hasn't accepted. */
export class CallFeedback {
  readonly submissionId = crypto.randomUUID();
  private accepted = new Set<string>();
  private answers: { rating: Rating; comment: string } | null = null;
  private busy = false;
  readonly config = feedbackConfig();

  constructor(readonly snapshot: FeedbackSnapshot) {}

  private capture(client: PostHog, key: string, event: string, props: Record<string, unknown>) {
    if (this.accepted.has(key)) return;
    if (!analyticsAvailable(client)) throw new Error("Analytics unavailable");
    const result = client.capture(event, {
      ...feedbackProperties(this.snapshot, this.submissionId),
      $survey_id: this.config!.surveyId,
      ...props,
    });
    if (!result) throw new Error("Capture was not accepted");
    this.accepted.add(key);
  }

  shown(client: PostHog) {
    this.capture(client, "shown", "survey shown", {});
  }

  submit(client: PostHog, rating: Rating, comment: string): boolean {
    if (this.busy || !this.config || comment.length > 2000) return false;
    this.busy = true;
    this.answers ??= { rating, comment: comment.trim() };
    try {
      this.shown(client);
      this.capture(client, "sent", "survey sent", {
        $survey_completed: true,
        [`$survey_response_${this.config.ratingId}`]: this.answers.rating,
        [`$survey_response_${this.config.commentId}`]: this.answers.comment,
      });
      for (const traceId of this.snapshot.traceIds) {
        this.capture(client, traceId, "voice_call_feedback_linked", {
          $ai_trace_id: traceId, rating: this.answers.rating,
        });
      }
      return true;
    } finally { this.busy = false; }
  }
}
