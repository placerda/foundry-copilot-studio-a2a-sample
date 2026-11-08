import { defineConfig } from "@playwright/test";
import base from "../../playwright.config.js";
import { fileURLToPath } from "node:url";

export default defineConfig({
  ...base,
  testDir: ".",
  use: { ...base.use, baseURL: "http://localhost:8001" },
  webServer: { ...base.webServer, url: "http://localhost:8001",
    cwd: fileURLToPath(new URL("../../", import.meta.url)),
    env: { CONTOSO_TEST_PORT: "8001" } },
});
