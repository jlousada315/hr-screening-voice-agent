import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import nextEnv from "@next/env";

// Match the API: local configuration lives in the repository root.
// Next has already loaded web/.env*; reload from root, preserving process env.
const repoRoot = fileURLToPath(new URL("../", import.meta.url));
nextEnv.loadEnvConfig(repoRoot, process.env.NODE_ENV !== "production", console, true);

// The same owner file the API reads: $OWNER_FILE, then config/owner.md, then the example.
function ownerFile() {
  const configured = process.env.OWNER_FILE?.trim();
  if (configured) return isAbsolute(configured) ? configured : join(repoRoot, configured);
  const owner = join(repoRoot, "config/owner.md");
  return existsSync(owner) ? owner : join(repoRoot, "config/example.md");
}

// Mirrors agent/src/hireme_agent/owner.py: the `# Name` heading and the `Key: value` lines below it.
const POSSESSIVE = { he: "his", she: "her", they: "their" };
const head = readFileSync(ownerFile(), "utf8").replace(/<!--[\s\S]*?-->/g, "").split(/^## /m)[0];
const name = head.match(/^# (.+)$/m)?.[1].trim() ?? "";
const field = (key) => head.match(new RegExp(`^${key}:(.*)$`, "im"))?.[1].trim() || "";
const pronouns = field("pronouns").toLowerCase().split("/").map((word) => word.trim());
const owner = {
  name,
  short_name: field("short name"),
  possessive: pronouns[2] || POSSESSIVE[pronouns[0]] || "their",
  initials: field("initials"),
  tagline: field("tagline"),
};
const initials = owner.initials || owner.name.split(/\s+/).slice(0, 2).map((word) => word[0]).join("").toUpperCase();

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // These are the only server-named settings intentionally exposed to the browser.
  env: {
    POSTHOG_API_KEY: process.env.POSTHOG_API_KEY?.trim() || "",
    POSTHOG_API_URL: process.env.POSTHOG_API_URL?.trim() || "https://eu.i.posthog.com",
    OWNER_NAME: owner.name,
    OWNER_SHORT_NAME: owner.short_name || owner.name.split(/\s+/)[0],
    OWNER_POSSESSIVE: owner.possessive,
    OWNER_INITIALS: initials,
    OWNER_TAGLINE: owner.tagline || "AI screening call",
  },
};

export default nextConfig;
