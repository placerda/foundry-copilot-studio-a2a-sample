import { defineConfig } from "@playwright/test";

const port = Number(process.env.CONTOSO_TEST_PORT || 8000);

export default defineConfig({
  testDir: "./frontend/tests",
  testMatch: "**/*.spec.js",
  fullyParallel: false,
  workers: 1,
  use: { baseURL: `http://localhost:${port}`, headless: true },
  webServer: {
    command: "node frontend/tests/server.mjs",
    url: `http://localhost:${port}`,
    reuseExistingServer: false,
  },
});
