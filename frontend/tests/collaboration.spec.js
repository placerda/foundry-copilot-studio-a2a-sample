import { test, expect } from "@playwright/test";
import { mkdir } from "node:fs/promises";

const functionURL = "https://synthetic.lambda-url.us-east-2.on.aws/**";
const evidence = "test-results/supplier-buyer-ui";
const answer = {
  answer: "## Proposal assessment\n\n**Synthetic test response**, not live approval.\n\n- Check the buyer's terms.\n- Confirm the evidence limits.\n\n| Delivery | Units |\n| --- | --- |\n| Friday | 300 |\n| Monday | 200 |\n\n[Reference](https://example.com/terms) [Unsafe](javascript:alert)\n\n<script>window.unsafe=true</script>\n\n```html\n<img src=x onerror=alert(1)>\n```",
  request_id: "synthetic-ui-evidence",
  output_limited: false,
  contoso: {
    status: "answered", provider_id: "synthetic-buyer", studio_variant: "copilot-harness",
    sources: [{ source_id: "SYNTHETIC-TERMS", provider_id: "synthetic-buyer", version: "test-v1",
      title: "Synthetic buyer terms", classification: "Synthetic demonstration data" }],
    citations: [{ source_id: "SYNTHETIC-TERMS", version: "test-v1", section: "Partial delivery conditions" }],
    execution_path: ["Synthetic test gateway", "Synthetic buyer response"],
  },
};

test.beforeEach(async ({ page }) => {
  await page.route("**/*", route => {
    const host = new URL(route.request().url()).hostname;
    return host === "localhost" || host === "127.0.0.1" ? route.continue() : route.abort();
  });
});

for (const [device, size] of [["desktop", { width: 1440, height: 1000 }], ["mobile", { width: 390, height: 844 }]]) {
  test(`${device}: context, signed-out state, draft suggestions and safe cited Markdown`, async ({ page }) => {
    await mkdir(evidence, { recursive: true });
    await page.setViewportSize(size);
    await page.addInitScript(() => { window.contosoTestEnabled = true; });
    await page.goto("/");
    await expect(page.locator("#delivery-context")).not.toHaveAttribute("open", "");
    await expect(page.locator("#contoso-controls")).not.toHaveAttribute("open", "");
    await expect(page.locator("#reset")).toBeHidden();
    await expect(page.locator("#logout")).toBeHidden();
    await expect(page.getByRole("heading", { name: "Original buyer request" })).toBeHidden();
    await page.screenshot({ path: `${evidence}/${device}-signed-out.png`, fullPage: true });
    await page.locator("#delivery-context > summary").click();
    await expect(page.getByRole("heading", { name: "Original buyer request" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Supplier proposal", exact: true })).toBeVisible();
    await expect(page.getByText("Illustrative context only.", { exact: false })).toBeVisible();
    await expect(page.getByRole("button", { name: "Evaluate the delivery proposal" })).toBeDisabled();
    await page.locator("#delivery-context > summary").click();
    await page.locator("#login").click();
    await expect(page.locator("#login")).toBeHidden();
    await expect(page.locator("#reset")).toBeVisible();
    await expect(page.locator("#logout")).toBeVisible();
    await page.locator("#contoso-controls > summary").click();
    await page.getByLabel("Network mode").selectOption("contoso");
    let payload;
    await page.route(functionURL, route => {
      payload = route.request().postDataJSON();
      return route.fulfill({ json: answer });
    });
    await page.getByRole("button", { name: "Evaluate the delivery proposal" }).click();
    expect(payload).toBeUndefined();
    const draft = await page.locator("#draft").inputValue();
    expect(draft).toContain("500 units due Friday");
    await page.locator("#send").click();
    await expect(page.locator(".content h3")).toHaveText("Proposal assessment");
    await expect(page.locator(".content table")).toBeVisible();
    await expect(page.locator(".content a")).toHaveCount(1);
    await expect(page.locator(".content a")).toHaveAttribute("rel", "noopener noreferrer");
    await expect(page.locator("#messages script, #messages img")).toHaveCount(0);
    await expect(page.locator(".content").last()).toContainText("<script>window.unsafe=true</script>");
    await expect(page.locator(".contoso-metadata")).toContainText("Partial delivery conditions");
    const details = page.locator(".contoso-metadata details");
    await expect(details).not.toHaveAttribute("open", "");
    await details.locator("summary").click();
    await expect(details).toContainText("synthetic-ui-evidence");
    await expect(details).toContainText("Synthetic test gateway");
    await details.locator("summary").click();
    expect(payload).toEqual({ messages: [{ role: "user", content: draft }],
      contoso_options: { integration: "a2a", studio_variant: "copilot-harness" } });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `${evidence}/${device}-response.png`, fullPage: true });
    await page.locator("#reset").click();
    await expect(page.locator("#empty-state")).toBeVisible();
  });
}

test("loading and error states preserve the draft without inventing an answer", async ({ page }) => {
  await page.goto("/");
  await page.locator("#login").click();
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(functionURL, async route => {
    await held;
    await route.fulfill({ status: 503, body: "private-upstream" });
  });
  await page.locator("#draft").fill("Check proposal conditions");
  await page.locator("#send").click();
  await expect(page.locator("#processing")).toHaveText("Processing...");
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#messages")).toHaveAttribute("aria-busy", "true");
  await page.screenshot({ path: `${evidence}/desktop-loading.png`, fullPage: true });
  release();
  await expect(page.locator("#error")).toContainText("Your message was preserved");
  await expect(page.locator("#draft")).toHaveValue("Check proposal conditions");
  await expect(page.locator("#messages article")).toHaveCount(0);
  await page.screenshot({ path: `${evidence}/desktop-error.png`, fullPage: true });
});
