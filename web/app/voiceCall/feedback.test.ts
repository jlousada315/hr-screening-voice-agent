import type { PostHog } from "posthog-js";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { analyticsAvailable, CallFeedback, feedbackConfig } from "./feedback";

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_POSTHOG_FEEDBACK_SURVEY_ID", "survey");
  vi.stubEnv("NEXT_PUBLIC_POSTHOG_FEEDBACK_RATING_QUESTION_ID", "rating");
  vi.stubEnv("NEXT_PUBLIC_POSTHOG_FEEDBACK_COMMENT_QUESTION_ID", "comment");
});
afterEach(() => vi.unstubAllEnvs());

it.each([
  "NEXT_PUBLIC_POSTHOG_FEEDBACK_SURVEY_ID",
  "NEXT_PUBLIC_POSTHOG_FEEDBACK_RATING_QUESTION_ID",
  "NEXT_PUBLIC_POSTHOG_FEEDBACK_COMMENT_QUESTION_ID",
])("requires nonblank %s", (key) => {
  vi.stubEnv(key, " ");
  expect(feedbackConfig()).toBeNull();
});

it("rejects duplicate question IDs and missing SDK", () => {
  vi.stubEnv("NEXT_PUBLIC_POSTHOG_FEEDBACK_COMMENT_QUESTION_ID", "rating");
  expect(feedbackConfig()).toBeNull();
  expect(analyticsAvailable(undefined)).toBe(false);
});

it("sends the call's final event once, retrying a rejection with the first answers", () => {
  const capture = vi.fn<(...args: unknown[]) => unknown>().mockReturnValueOnce(undefined)
    .mockReturnValue({ uuid: "accepted" });
  const client = { capture, has_opted_out_capturing: () => false } as unknown as PostHog;
  const feedback = new CallFeedback({ conversationId: "conversation", callId: "call" });
  expect(() => feedback.submit(client, 4, " Good ")).toThrow("not accepted");
  expect(feedback.submit(client, 1, "changed")).toBe(true);
  expect(feedback.submit(client, 2, "again")).toBe(true);
  expect(capture).toHaveBeenCalledTimes(2);
  expect(capture.mock.calls.every(([event]) => event === "survey sent")).toBe(true);
  expect(capture.mock.calls[1][1]).toMatchObject({
    $survey_id: "survey", $survey_completed: true, $survey_response_rating: 4,
    $survey_response_comment: "Good", $ai_trace_id: "conversation", $ai_session_id: "conversation",
    conversation_id: "conversation", call_id: "call",
    $survey_submission_id: feedback.submissionId,
  });
});

it("checks opt-out before capturing a response", () => {
  const capture = vi.fn(() => ({ uuid: "accepted" }));
  const client = { capture, has_opted_out_capturing: () => true } as unknown as PostHog;
  const feedback = new CallFeedback({ conversationId: "conversation", callId: "call" });
  expect(() => feedback.submit(client, 5, "")).toThrow("unavailable");
  expect(capture).not.toHaveBeenCalled();
});
