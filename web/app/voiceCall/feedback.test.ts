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

it("retries SDK rejection without duplicating accepted impressions or links", () => {
  const capture = vi.fn<(...args: unknown[]) => unknown>().mockReturnValue({ uuid: "accepted" });
  const client = { capture, has_opted_out_capturing: () => false } as unknown as PostHog;
  const feedback = new CallFeedback({ conversationId: "call", traceIds: ["one", "two"] });
  feedback.shown(client);
  capture.mockReturnValueOnce(undefined);
  expect(() => feedback.submit(client, "Yes, helpful", "")).toThrow("not accepted");
  capture.mockImplementation((event, props) => {
    if (event === "voice_call_feedback_linked" && (props as Record<string, unknown>).$ai_trace_id === "two") {
      throw new Error("link failed");
    }
    return { uuid: "accepted" };
  });
  expect(() => feedback.submit(client, "Yes, helpful", "")).toThrow("link failed");
  capture.mockReturnValue({ uuid: "accepted" });
  expect(feedback.submit(client, "Yes, helpful", "")).toBe(true);
  expect(capture.mock.calls.filter(([event]) => event === "survey shown")).toHaveLength(1);
  expect(capture.mock.calls.filter(([event, props]) => event === "voice_call_feedback_linked" &&
    (props as Record<string, unknown>).$ai_trace_id === "one")).toHaveLength(1);
});

it("checks opt-out again before capturing a response", () => {
  const capture = vi.fn(() => ({ uuid: "accepted" }));
  const optedOut = vi.fn(() => false);
  const client = { capture, has_opted_out_capturing: optedOut } as unknown as PostHog;
  const feedback = new CallFeedback({ conversationId: "call", traceIds: ["one"] });
  feedback.shown(client);
  optedOut.mockReturnValue(true);
  expect(() => feedback.submit(client, "Yes, helpful", "")).toThrow("unavailable");
  expect(capture).toHaveBeenCalledOnce();
});
