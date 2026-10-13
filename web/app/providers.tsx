"use client";

import { useEffect, useState } from "react";
import posthog from "posthog-js";
import { PostHogProvider } from "posthog-js/react";

const POSTHOG_KEY = process.env.POSTHOG_API_KEY;
const POSTHOG_HOST = process.env.POSTHOG_API_URL;

export function Providers({ children }: { children: React.ReactNode }) {
  // Only initialize once, on the client, and only when a key is configured.
  // Without a key (e.g. local dev) we render children untouched, so SSR and
  // keyless dev never crash.
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!POSTHOG_KEY) return;
    try {
      posthog.init(POSTHOG_KEY, {
        api_host: POSTHOG_HOST,
        capture_pageview: true,
        capture_pageleave: true,
        capture_performance: { web_vitals: true },
      });
      setReady(true);
    } catch { /* The call remains usable when analytics initialization fails. */ }
  }, []);

  if (!POSTHOG_KEY || !ready) return <>{children}</>;

  return <PostHogProvider client={posthog}>{children}</PostHogProvider>;
}
