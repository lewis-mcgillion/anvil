import { defineConfig } from "vitest/config";

// Without a config file here, Vitest searches upward and loads web/vite.config.ts,
// which belongs to the dashboard and imports `vite` — a dependency this package
// does not install. Pinning the config keeps the schema contract tests
// independent of the dashboard's build setup.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
  },
});
