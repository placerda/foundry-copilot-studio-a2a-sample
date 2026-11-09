import { test, expect } from "@playwright/test";

test("three identity scenarios expose backend readiness without enabling unproven adapters", async ({ page }) => {
  await page.goto("/");
  const selector = page.locator("#identity-mode");
  await expect(selector).toBeEnabled();
  await expect(selector).toHaveValue("shared-delegated");
  await expect(selector.locator("option")).toHaveCount(3);
  for (const mode of ["user-delegation", "service-to-service"]) {
    await expect(selector.locator(`option[value="${mode}"]`)).toBeDisabled();
  }
  await expect(page.locator("#identity-callers")).toHaveText("App user: sign in. Studio identity: shared connection (unverified).");
  await expect(page.locator("#identity-reason")).toHaveText("Shared connection selected.");
  await expect(page.locator(".identity-panel details")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("live proof");
  await expect(page.locator("body")).not.toContainText("integrity-protected");
  await expect(page.locator("body")).not.toContainText("Paulo");
  await expect(page.locator("body")).not.toContainText("Maria");
});

test("forced unavailable mode resets conversation and never silently falls back", async ({ page }) => {
  let calls = 0;
  await page.route("**/api/chat", route => {
    calls++;
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
  await page.locator("#identity-mode").evaluate(element => {
    element.value = "service-to-service";
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.locator("#messages article")).toHaveCount(0);
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#error")).toContainText("Select Shared delegated connection");
  await expect(page.locator("#identity-reason")).toHaveText("Not configured.");
  expect(calls).toBe(1);
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
  await expect(page.locator("#error")).toContainText("Invalid configuration");
});
