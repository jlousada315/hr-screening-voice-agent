import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    // What next.config.mjs derives from config/example.md.
    env: {
      OWNER_NAME: "Alex Rivera",
      OWNER_SHORT_NAME: "Alex",
      OWNER_POSSESSIVE: "their",
      OWNER_INITIALS: "AR",
      OWNER_TAGLINE: "AI screening call",
    },
  },
});
