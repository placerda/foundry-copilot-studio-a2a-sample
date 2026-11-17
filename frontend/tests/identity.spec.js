import { test, expect } from "@playwright/test";

test("the shared scenario is fixed with no identity selector", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("select")).toHaveCount(0);
  await expect(page.locator("#identity-mode")).toHaveText("Scenario 3: Shared delegated connection");
  await expect(page.locator("#identity-callers")).toHaveText("App user: sign in. Studio identity: shared connection (unverified).");
  await expect(page.locator("#identity-reason")).toHaveText("Shared connection selected.");
  await expect(page.locator(".identity-panel details")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("live proof");
  await expect(page.locator("body")).not.toContainText("integrity-protected");
  await expect(page.locator("body")).not.toContainText("Paulo");
  await expect(page.locator("body")).not.toContainText("Maria");
});

test("a DOM-injected selector cannot change the fixed request identity", async ({ page }) => {
  const payloads = [];
  await page.route("**/api/chat", route => {
    payloads.push(route.request().postDataJSON());
    return route.fulfill({ json: {
      answer: "No capability matches.", request_id: "synthetic", output_limited: false,
      contoso: { status: "no-matching-capability", provider_id: null, studio_variant: null,
        sources: [], citations: [], execution_path: ["router"] },
    } });
  });
  await page.goto("/");
  await page.locator("#login").click();
  await page.locator("#draft").fill("Supplier terms?");
  await page.locator("#send").click();
  await expect(page.locator("#messages article")).toHaveCount(2);
  await page.evaluate(() => {
    const selector = document.createElement("select");
    selector.name = "identity_mode";
    selector.innerHTML = '<option value="service-to-service">Injected mode</option>';
    document.querySelector("#chat-form").append(selector);
    selector.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.locator("#draft").fill("Supplier terms again?");
  await page.locator("#send").click();
  await expect(page.locator("#messages article")).toHaveCount(4);
  expect(payloads).toHaveLength(2);
  expect(payloads.every(payload => payload.identity_mode === "shared-delegated")).toBe(true);
});

test("unimplemented readiness cannot be enabled by frontend configuration", async ({ page }) => {
  await page.route("**/api/capabilities", route => route.fulfill({ json: {
    default: "shared-delegated", modes: {
      "user-delegation": { enabled: true, reason: "Forged readiness." },
      "service-to-service": { enabled: false, reason: "Blocked." },
      "shared-delegated": { enabled: true, reason: "Fixed connection." },
    },
  } }));
  await page.goto("/");
  await expect(page.locator("#login")).toBeDisabled();
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#error")).toContainText("Identity readiness is unavailable or invalid");
  await expect(page.locator("#identity-reason")).toContainText("Availability could not be verified");
});

test("authentication initialization failure is not misreported as invalid configuration", async ({ page }) => {
  await page.addInitScript(() => { window.initializeFailure = true; });
  let readinessCalls = 0;
  await page.route("**/api/capabilities", route => { readinessCalls++; return route.abort(); });
  await page.goto("/");
  await expect(page.locator("#error")).toContainText("Microsoft sign-in could not initialize");
  await expect(page.locator("#error")).not.toContainText("Synthetic private diagnostic");
  await expect(page.locator("#login")).toBeDisabled();
  await expect(page.locator("#send")).toBeDisabled();
  expect(readinessCalls).toBe(0);
});

test("unreachable readiness fails closed without blaming public configuration", async ({ page }) => {
  await page.route("**/api/capabilities", route => route.abort());
  await page.goto("/");
  await expect(page.locator("#error")).toContainText("Identity readiness is unavailable or invalid");
  await expect(page.locator("#login")).toBeDisabled();
  await expect(page.locator("#send")).toBeDisabled();
});

test("unavailable shared readiness cannot select another route or send a request", async ({ page }) => {
  await page.route("**/api/capabilities", route => route.fulfill({ json: {
    default: "shared-delegated", modes: {
      "user-delegation": { enabled: false, reason: "Blocked." },
      "service-to-service": { enabled: false, reason: "Blocked." },
      "shared-delegated": { enabled: false, reason: "Shared connection unavailable." },
    },
  } }));
  let calls = 0;
  await page.route("**/api/chat", route => { calls++; return route.abort(); });
  await page.goto("/");
  await page.locator("#login").click();
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#identity-reason")).toHaveText("Shared connection unavailable.");
  await page.evaluate(() => document.querySelector("#chat-form").requestSubmit());
  await expect(page.locator("#error")).toContainText("No request was sent");
  expect(calls).toBe(0);
});
