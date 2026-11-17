import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
const motorsScenarios = JSON.parse(await readFile(
  new URL("../../contoso-demo/motors-scenarios.json", import.meta.url), "utf8",
)).scenarios;
const question = "We propose delivering 300 of 500 units Friday and the remaining 200 Monday. Does this satisfy the buyer's terms?";
const noMatchQuestion = "Approve the supplier's partial delivery proposal.";
const functionURL = "**/api/chat";

function result(variant = "copilot-harness") {
  return { answer: "Literal actual response <img src=x onerror=alert(1)>.",
    request_id: "contoso-request", output_limited: false,
    contoso: { status: "answered", provider_id: "returned-provider", studio_variant: variant,
      sources: [{ source_id: "RETURNED-SOURCE", provider_id: "returned-provider", version: "2026-demo",
        title: "<script>unsafe-title</script>", classification: "Synthetic demonstration data" }],
      citations: [{ source_id: "RETURNED-SOURCE", version: "2026-demo", section: "Evidence limits" }],
      execution_path: ["Verified gateway response", "Actual downstream trace", "Verified source attribution"] } };
}

test.beforeEach(async ({ page }) => {
  await page.route("**/*", route => {
    const host = new URL(route.request().url()).hostname;
    return host === "localhost" || host === "127.0.0.1" ? route.continue() : route.abort();
  });
});

async function loginContoso(page, flag = true) {
  await page.addInitScript(value => { window.contosoTestEnabled = value; }, flag);
  await page.goto("/");
  await expect(page.locator("#send")).toBeDisabled();
  await page.locator("#login").click();
  await expect(page.locator("#draft")).toBeEnabled();
}

test("Contoso Motors branding preserves native route binding labels", async ({ page }) => {
  await loginContoso(page);
  await expect(page).toHaveTitle("Contoso Motors | Agent collaboration");
  await expect(page.getByRole("heading", { name: "Contoso Motors purchasing assistant" })).toBeVisible();
  await expect(page.locator("select")).toHaveCount(0);
  await expect(page.locator(".connection-panel")).toContainText("Microsoft Foundry");
  await expect(page.locator(".connection-panel")).toContainText("Copilot Studio");
  await expect(page.locator("body"))
    .not.toContainText(/\blibrarian\b/i);
});

test("disabled or malformed public flag never enables a standalone chat fallback", async ({ page }) => {
  await page.addInitScript(() => { window.contosoTestEnabled = false; });
  await page.goto("/");
  await expect(page.locator("select")).toHaveCount(0);
  await expect(page.locator(".connection-panel")).toBeVisible();
  await expect(page.locator("#error")).toContainText("Agent network is not configured");
  await page.locator("#login").click();
  await expect(page.locator("#draft")).toBeDisabled();
  let calls = 0;
  await page.route(functionURL, route => { calls++; return route.abort(); });
  await page.evaluate(() => document.querySelector("#chat-form").requestSubmit());
  await expect(page.locator("#error")).toContainText("No request was sent");
  expect(calls).toBe(0);
  await page.addInitScript(() => { window.contosoTestEnabled = "true"; });
  await page.reload();
  await expect(page.locator("#error")).toContainText("Invalid configuration");
  await expect(page.locator("#login")).toBeDisabled();
});

for (const integration of ["a2a"]) {
  for (const variant of ["copilot-harness"]) {
    test(`actual ${integration} / ${variant} payload, English question and safe returned attribution`, async ({ page }) => {
      const requests = [];
      await page.route(functionURL, route => {
        requests.push(route.request().postDataJSON());
        expect(route.request().headers().authorization).toBe("Bearer synthetic-access-token");
        return route.fulfill({ json: result(variant) });
      });
      await loginContoso(page);
      await page.locator("#draft").fill(question);
      await page.evaluate(() => {
        for (let index = 0; index < 10; index++) document.querySelector("#chat-form").requestSubmit();
      });
      await expect(page.locator(".contoso-metadata")).toContainText("RETURNED-SOURCE");
      await expect(page.locator(".contoso-metadata")).toContainText("returned-provider");
      await expect(page.locator(".contoso-metadata")).toContainText("2026-demo");
      await expect(page.locator(".contoso-metadata")).toContainText("Evidence limits");
      await expect(page.locator(".contoso-metadata")).toContainText("Actual downstream trace");
      await expect(page.locator("#messages img, #messages script")).toHaveCount(0);
      await expect(page.locator(".content").last()).toHaveText(result(variant).answer);
      await expect(page.locator(".contoso-metadata")).not.toContainText("CONTOSO-MOTORS-SUPPLIER-001");
      expect(requests).toEqual([{ messages: [{ role: "user", content: question }],
        contoso_options: { integration, studio_variant: variant }, identity_mode: "shared-delegated" }]);
      expect(JSON.stringify(requests)).not.toContain("tenant");
      expect(JSON.stringify(requests)).not.toContain("client_secret");
      await page.locator("#reset").click();
      await expect(page.locator(".contoso-metadata")).toHaveCount(0);
      await expect(page.locator("#send")).toBeEnabled();
      await page.locator("#draft").fill("Second question after reset");
      await page.locator("#send").click();
      await expect(page.locator(".contoso-metadata")).toBeVisible();
      expect(requests[1].contoso_options).toEqual({ integration, studio_variant: variant });
    });
  }
}

for (const [scenarioId, suggestion] of [
  ["logistics-returnables", "Coordinate returnable packaging"],
  ["logistics-evidence-limits", "Check missing collection information"],
]) {
  test(`${scenarioId}: registered suggestion preserves non-Studio attribution`, async ({ page }) => {
    const value = result(null);
    const requests = [];
    await page.route(functionURL, route => {
      requests.push(route.request().postDataJSON());
      return route.fulfill({ json: value });
    });
    await loginContoso(page);
    await page.getByRole("button", { name: suggestion }).click();
    expect(requests).toHaveLength(0);
    const registeredQuestion = motorsScenarios.find(scenario => scenario.id === scenarioId).question;
    await expect(page.locator("#draft")).toHaveValue(registeredQuestion);
    await page.locator("#send").click();
    await expect(page.locator(".content").last()).toHaveText(value.answer);
    await expect(page.locator(".contoso-metadata")).toContainText("returned-provider");
    await expect(page.locator(".contoso-metadata")).toContainText("RETURNED-SOURCE");
    await expect(page.locator(".contoso-metadata")).toContainText("2026-demo");
    await expect(page.locator(".contoso-metadata")).not.toContainText("Copilot Studio configuration:");
    expect(requests).toHaveLength(1);
    expect(requests[0].messages).toEqual([{ role: "user", content: registeredQuestion }]);
    expect(requests[0].contoso_options).toEqual({ integration: "a2a", studio_variant: "copilot-harness" });
    expect(requests[0].identity_mode).toBe("shared-delegated");
  });
}

test("unknown capability displays true literal answer and no provider or invented sources", async ({ page }) => {
  const value = result();
  value.answer = "No approved agent matches this request. No knowledge provider was called.";
  value.contoso = { status: "no-matching-capability", provider_id: null, studio_variant: null,
    sources: [], citations: [], execution_path: ["Verified gateway", "No matching capability"] };
  let calls = 0;
  await page.route(functionURL, route => {
    calls++;
    return route.fulfill({ json: value });
  });
  await loginContoso(page);
  await page.locator("#draft").fill(noMatchQuestion);
  await page.locator("#send").click();
  await expect(page.locator(".content").last()).toHaveText(value.answer);
  await expect(page.locator(".contoso-metadata")).toContainText("No sources returned.");
  await expect(page.locator(".contoso-metadata")).not.toContainText("Provider:");
  expect(calls).toBe(1);
});

for (const [label, changes] of [
  ["missing metadata", { contoso: undefined }],
  ["unexpected response metadata", { contoso: undefined, unexpected_metadata: result().contoso }],
  ["wrong variant", { contoso: { ...result().contoso, studio_variant: "standard" } }],
  ["missing citation", { contoso: { ...result().contoso, citations: [] } }],
  ["wrong attribution", { contoso: { ...result().contoso,
    citations: [{ source_id: "invented", version: "1", section: "x" }] } }],
  ["missing output limit", { output_limited: undefined }],
]) {
  test(`Contoso rejects ${label} without fallback and preserves draft`, async ({ page }) => {
    let calls = 0;
    await page.route(functionURL, route => {
      calls++;
      return route.fulfill({ json: { ...result(), ...changes } });
    });
    await loginContoso(page);
    await page.locator("#draft").fill("preserve");
    await page.locator("#send").click();
    await expect(page.locator("#error")).toContainText("Invalid response");
    await expect(page.locator("#draft")).toHaveValue("preserve");
    await expect(page.locator("#messages article")).toHaveCount(0);
    expect(calls).toBe(1);
  });
}

for (const status of [401, 403, 409, 429, 502, 503, 504]) {
  test(`Contoso HTTP ${status} stays safe, explicit and manual-only`, async ({ page }) => {
    let calls = 0;
    await page.route(functionURL, route => {
      calls++;
      return route.fulfill({ status, contentType: "text/plain", body: "RAW-CREDENTIAL-SENTINEL" });
    });
    await loginContoso(page);
    await page.locator("#draft").fill("manual");
    await page.locator("#send").click();
    await expect(page.locator("#error")).not.toBeEmpty();
    await expect(page.locator("#error")).not.toContainText("RAW-CREDENTIAL-SENTINEL");
    if (status === 502 || status === 503) await expect(page.locator("#error")).toContainText("connection is unavailable");
    await expect(page.locator("#draft")).toHaveValue("manual");
    await expect(page.locator("#messages article")).toHaveCount(0);
    expect(calls).toBe(1);
  });
}

test("Contoso waits beyond legacy budget, times out at 100 seconds and never retries", async ({ page }) => {
  await page.clock.install();
  let calls = 0;
  let started;
  const requested = new Promise(resolve => { started = resolve; });
  await page.route(functionURL, () => { calls++; started(); });
  await loginContoso(page);
  await page.locator("#draft").fill("uncertain");
  await page.locator("#send").click();
  await requested;
  await page.clock.fastForward(35001);
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#draft")).toBeDisabled();
  await page.clock.fastForward(65000);
  await expect(page.locator("#error")).toContainText("100 seconds");
  await expect(page.locator("#error")).toContainText("may continue");
  await expect(page.locator("#draft")).toHaveValue("uncertain");
  expect(calls).toBe(1);
});

test("Contoso logout invalidates token await and prevents late network request", async ({ page }) => {
  let calls = 0;
  await page.route(functionURL, route => { calls++; return route.fulfill({ json: result() }); });
  await loginContoso(page);
  await page.evaluate(() => { window.tokenDelay = 500; });
  await page.locator("#draft").fill("old");
  await page.locator("#send").click();
  await page.locator("#logout").click();
  await expect(page.locator("#processing")).toBeEmpty();
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#messages article")).toHaveCount(0);
  await expect(page.locator("select")).toHaveCount(0);
  expect(calls).toBe(0);
});

test("Contoso reset disposes late response and limit notice and preserves the fixed route", async ({ page }) => {
  let started;
  const requested = new Promise(resolve => { started = resolve; });
  await page.route(functionURL, async route => {
    started();
    await new Promise(resolve => setTimeout(resolve, 400));
    await route.fulfill({ json: { ...result(), output_limited: true } }).catch(() => {});
  });
  await loginContoso(page);
  await page.locator("#draft").fill("old");
  await page.locator("#send").click();
  await requested;
  await page.locator("#reset").click();
  await expect(page.locator("#processing")).toBeEmpty();
  await expect(page.locator("#messages article, .limit")).toHaveCount(0);
  await page.route(functionURL, route => route.fulfill({ json: { ...result(), output_limited: true } }));
  await page.locator("#draft").fill("new");
  await page.locator("#send").click();
  await expect(page.locator(".limit")).toHaveCount(1);
  await expect(page.locator("select")).toHaveCount(0);
  await page.locator("#reset").click();
  await expect(page.locator("#messages article, .limit")).toHaveCount(0);
});

test("injected obsolete controls cannot change the fixed native A2A route", async ({ page }) => {
  const requests = [];
  await page.route(functionURL, route => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({ json: result() });
  });
  await loginContoso(page);
  await page.evaluate(() => {
    for (const [id, value] of [["network-mode", "legacy"], ["contoso-integration", "sdk"], ["studio-variant", "standard"]]) {
      const input = document.createElement("input");
      input.id = id;
      input.value = value;
      document.body.append(input);
    }
  });
  await page.locator("#draft").fill(question);
  await page.locator("#send").click();
  await expect(page.locator(".contoso-metadata")).toBeVisible();
  expect(requests).toEqual([{ messages: [{ role: "user", content: question }],
    contoso_options: { integration: "a2a", studio_variant: "copilot-harness" },
    identity_mode: "shared-delegated" }]);
});
