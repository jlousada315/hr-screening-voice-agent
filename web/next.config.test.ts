import { mkdtempSync, mkdirSync, copyFileSync, writeFileSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const webDir = dirname(fileURLToPath(import.meta.url));
const EXAMPLE_OWNER = {
  OWNER_NAME: "Alex Rivera", OWNER_SHORT_NAME: "Alex", OWNER_POSSESSIVE: "their",
  OWNER_INITIALS: "AR", OWNER_TAGLINE: "AI screening call",
};

function withRepo(run: (root: string) => void) {
  const root = mkdtempSync(join(tmpdir(), "hireme-env-"));
  try {
    mkdirSync(join(root, "web"));
    copyFileSync(join(webDir, "next.config.mjs"), join(root, "web/next.config.mjs"));
    mkdirSync(join(root, "config"));
    copyFileSync(join(webDir, "../config/example.md"), join(root, "config/example.md"));
    symlinkSync(join(webDir, "node_modules"), join(root, "web/node_modules"), "dir");
    run(root);
  } finally { rmSync(root, { recursive: true, force: true }); }
}

function loadConfigEnv(root: string, overrides: Record<string, string>) {
  const env = { ...process.env };
  for (const name of Object.keys(env)) {
    if (name === "POSTHOG_API_KEY" || name === "POSTHOG_API_URL" || name === "DEPLOYMENT_ENVIRONMENT" ||
      name === "OWNER_FILE" || name === "__NEXT_PROCESSED_ENV") delete env[name];
  }
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", `
    import nextEnv from '@next/env';
    nextEnv.loadEnvConfig(process.cwd(), true);
    const { default: config } = await import('./next.config.mjs');
    console.log(JSON.stringify(config.env));
  `], { cwd: join(root, "web"), env: { ...env, NODE_ENV: "development", ...overrides }, encoding: "utf8" });
  expect(result.status, result.stderr).toBe(0);
  return JSON.parse(result.stdout);
}

it.each([
  ["shared settings", "POSTHOG_API_KEY=project\nPOSTHOG_API_URL=https://shared.test", {}, "project", "https://shared.test"],
  ["exported settings", "POSTHOG_API_KEY=file-project\nPOSTHOG_API_URL=https://file.test",
    { POSTHOG_API_KEY: "exported-project", POSTHOG_API_URL: "https://exported.test" },
    "exported-project", "https://exported.test"],
  ["missing keys", "", {}, "", "https://eu.i.posthog.com"],
  ["default host", "POSTHOG_API_KEY=project", {}, "project", "https://eu.i.posthog.com"],
] as const)("loads %s after Next has loaded the web directory", (_name, contents, overrides, key, host) => {
  withRepo((root) => {
    writeFileSync(join(root, ".env"), contents + "\nGROQ_API_KEY=server-only\n");
    expect(loadConfigEnv(root, overrides))
      .toEqual({
        POSTHOG_API_KEY: key, POSTHOG_API_URL: host, DEPLOYMENT_ENVIRONMENT: "development",
        ...EXAMPLE_OWNER,
      });
  });
});

it("prefers config/owner.md over the example and OWNER_FILE over both", () => {
  withRepo((root) => {
    writeFileSync(join(root, "config/owner.md"),
      "# Sam Lee Park\nPronouns: she/her\n\n## Links\n- [GitHub](https://github.com/sam)\n");
    expect(loadConfigEnv(root, {})).toMatchObject({
      OWNER_NAME: "Sam Lee Park", OWNER_SHORT_NAME: "Sam", OWNER_INITIALS: "SL", OWNER_POSSESSIVE: "her",
    });
    expect(loadConfigEnv(root, { OWNER_FILE: "config/example.md" })).toMatchObject(EXAMPLE_OWNER);
  });
});
