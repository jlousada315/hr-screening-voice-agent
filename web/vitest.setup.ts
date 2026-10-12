import { vi } from "vitest";
import "@testing-library/jest-dom/vitest";

// jsdom does not implement scrollIntoView.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// The call hook uses usePostHog(); tests normally run without a key/provider.
// Return undefined so all `posthog?.…` calls are no-ops — mirroring keyless dev.
vi.mock("posthog-js/react", () => ({
  usePostHog: () => undefined,
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));
