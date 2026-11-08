import { test, expect } from "@playwright/test";

function result(answer, output_limited = false) {
  return { answer, request_id: "test", output_limited,
    contoso: { status: "no-matching-capability", provider_id: null, studio_variant: null,
      sources: [], citations: [], execution_path: ["Synthetic test route without a knowledge provider"] } };
}

test.beforeEach(async ({ page }) => {
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.hostname !== "localhost" && url.hostname !== "127.0.0.1") {
      await route.abort();
    } else {
      await route.continue();
    }
  });
});

async function login(page) {
  await page.goto("/");
  await expect(page.locator("#send")).toBeDisabled();
  await page.locator("#login").click();
  await expect(page.locator("#draft")).toBeEnabled();
}

test("known sign-in failures show only allowlisted codes and keep the user signed out", async ({ page }) => {
  await page.addInitScript(() => { window.loginErrorCode = "interaction_in_progress"; });
  await page.goto("/");
  await page.locator("#login").click();
  await expect(page.locator("#error")).toContainText("failed (interaction_in_progress)");
  await expect(page.locator("#error")).not.toContainText("Synthetic private diagnostic");
  await expect(page.locator("#auth-status")).toHaveText("Sign in to chat.");
  await expect(page.locator("#send")).toBeDisabled();
});

test("application labels and session states are in English", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page).toHaveTitle("Supplier–Buyer Agent Collaboration");
  await expect(page.getByRole("heading", { name: "Supplier–Buyer Agent Collaboration" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Session" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Messages" })).toBeAttached();
  await expect(page.locator("#reset")).toBeHidden();
  await expect(page.locator("#logout")).toBeHidden();
  await expect(page.getByLabel("Your message")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  await expect(page.locator("#auth-status")).toHaveText("Sign in to chat.");
  await page.getByRole("button", { name: "Sign in with Microsoft" }).click();
  await expect(page.locator("#auth-status")).toHaveText("Signed in.");
  await expect(page.getByRole("button", { name: "New conversation" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeEnabled();
});

test("root OAuth return in popup leaves hash intact and does not initialize MSAL", async ({ page }) => {
  await page.goto("/");
  const opened = page.waitForEvent("popup");
  await page.evaluate(() => window.open("/#code=synthetic-code&state=synthetic-state"));
  const popup = await opened;
  await popup.waitForLoadState();
  expect(await popup.evaluate(() => window.location.hash)).toBe("#code=synthetic-code&state=synthetic-state");
  expect(await popup.evaluate(() => window.fakeAuthOptions)).toBeUndefined();
  await popup.close();
});

test("authorized text-only response and associated limit notice without continuation", async ({ page }) => {
  let calls = 0;
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => {
    calls++;
    expect(route.request().headers().authorization).toBe("Bearer synthetic-access-token");
    return route.fulfill({ json: result("<img src=x onerror=alert(1)>", true) });
  });
  await login(page);
  await page.locator("#draft").fill("Olá");
  await page.locator("#send").click();
  await expect(page.locator(".limit")).toHaveText("The response reached the generation limit and may be incomplete.");
  await expect(page.locator("#messages strong")).toHaveText(["You", "Assistant"]);
  await expect(page.locator("#messages img")).toHaveCount(0);
  await expect(page.locator(".content").last()).toHaveText("<img src=x onerror=alert(1)>");
  expect(calls).toBe(1);
  expect(await page.evaluate(() => window.fakeAuthOptions.cache.cacheLocation)).toBe("memoryStorage");
  expect(await page.evaluate(() => window.fakeAuthOptions.cache.temporaryCacheLocation)).toBe("memoryStorage");
});

for (const value of [
  { answer: "x", request_id: "id" },
  { answer: "x", request_id: "id", output_limited: null },
  { answer: "x", request_id: "id", output_limited: "true" },
  { answer: " ", request_id: "id", output_limited: false },
]) {
  test(`malformed success ${JSON.stringify(value)}`, async ({ page }) => {
    let calls = 0;
    await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => {
      calls++;
      return route.fulfill({ json: value });
    });
    await login(page);
    await page.locator("#draft").fill("preserve-draft");
    await page.locator("#send").click();
    await expect(page.locator("#error")).not.toBeEmpty();
    await expect(page.locator("#draft")).toHaveValue("preserve-draft");
    await expect(page.locator("#messages article")).toHaveCount(0);
    expect(calls).toBe(1);
  });
}

test("context remains in memory, notices stay on their message and reset/reload erase it", async ({ page }) => {
  const requests = [];
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({ json: result("memory-answer", requests.length === 1) });
  });
  await login(page);
  for (const draft of ["memory-sentinel", "followup"]) {
    await page.locator("#draft").fill(draft);
    await page.locator("#send").click();
    await expect(page.locator("#processing")).toBeEmpty();
  }
  expect(requests[1].messages.map(message => message.content)).toEqual(["memory-sentinel", "memory-answer", "followup"]);
  expect(requests.every(request => request.contoso_options.integration === "a2a")).toBe(true);
  expect(JSON.stringify(requests[1])).not.toContain("output_limited");
  await expect(page.locator(".limit")).toHaveCount(1);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain("memory-sentinel");
  await page.locator("#reset").click();
  await expect(page.locator("#messages article")).toHaveCount(0);
  await expect(page.locator(".limit")).toHaveCount(0);
  await page.reload();
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#messages article")).toHaveCount(0);
});

test("reset during token await retains lock and prevents late POST", async ({ page }) => {
  let calls = 0;
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => {
    calls++;
    return route.fulfill({ json: result("late", true) });
  });
  await login(page);
  await page.evaluate(() => { window.tokenDelay = 600; });
  await page.locator("#draft").fill("old");
  await page.locator("#send").click();
  await page.locator("#reset").click();
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#processing")).toBeEmpty();
  expect(calls).toBe(0);
  await expect(page.locator("#draft")).toHaveValue("");
  await expect(page.locator("#messages article")).toHaveCount(0);
});

test("late limited HTTP response cannot restore reset conversation", async ({ page }) => {
  let started;
  const called = new Promise(resolve => { started = resolve; });
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", async route => {
    started();
    await new Promise(resolve => setTimeout(resolve, 400));
    await route.fulfill({ json: result("late", true) }).catch(() => {});
  });
  await login(page);
  await page.locator("#draft").fill("old");
  await page.locator("#send").click();
  await called;
  await page.locator("#reset").click();
  await expect(page.locator("#processing")).toBeEmpty();
  await expect(page.locator("#messages article")).toHaveCount(0);
  await expect(page.locator("#error")).toBeEmpty();
});

test("logout clears local state even if provider popup fails", async ({ page }) => {
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route =>
    route.fulfill({ json: result("reply", true) }));
  await login(page);
  await page.locator("#draft").fill("secret-in-memory");
  await page.locator("#send").click();
  await expect(page.locator(".limit")).toHaveCount(1);
  await page.evaluate(() => { window.logoutFailure = true; });
  await page.locator("#logout").click();
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#messages article")).toHaveCount(0);
  await expect(page.locator("#draft")).toHaveValue("");
  await expect(page.locator("#error")).toContainText("SSO");
});

for (const [status, hint] of [[400, "Invalid message"], [401, "Invalid or expired session"],
  [403, "does not have access"], [405, "does not support this method"],
  [429, "busy"], [500, "Internal service error"], [502, "connection is unavailable"]]) {
  test(`friendly ${status} platform error keeps draft and no retry`, async ({ page }) => {
    let calls = 0;
    await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => {
      calls++;
      return route.fulfill({ status, contentType: "text/plain", body: "SECRET-RAW-UPSTREAM" });
    });
    await login(page);
    await page.locator("#draft").fill("manual retry");
    await page.locator("#send").click();
    await expect(page.locator("#error")).toContainText(hint);
    await expect(page.locator("#error")).not.toContainText("SECRET-RAW-UPSTREAM");
    await expect(page.locator("#draft")).toHaveValue("manual retry");
    await expect(page.locator("#messages article")).toHaveCount(0);
    expect(calls).toBe(1);
  });
}

test("ten submits during silent renewal produce one POST and interaction only when required", async ({ page }) => {
  let calls = 0;
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => {
    calls++;
    return route.fulfill({ json: result("ok") });
  });
  await login(page);
  await page.evaluate(() => { window.tokenDelay = 400; window.interactionRequired = true; });
  await page.locator("#draft").fill("one request");
  await page.evaluate(() => {
    for (let i = 0; i < 10; i++) document.querySelector("#chat-form").requestSubmit();
  });
  await expect(page.locator("#send")).toBeDisabled();
  await expect(page.locator("#processing")).toBeEmpty();
  expect(calls).toBe(1);
  expect(await page.evaluate(() => window.popupCalls)).toBe(1);
});

test("unrelated token failure never falls back to popup or sends POST", async ({ page }) => {
  let calls = 0;
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => { calls++; return route.abort(); });
  await login(page);
  await page.evaluate(() => { window.tokenFailure = true; });
  await page.locator("#draft").fill("keep");
  await page.locator("#send").click();
  await expect(page.locator("#error")).toContainText("Authentication failed");
  expect(calls).toBe(0);
  expect(await page.evaluate(() => window.popupCalls || 0)).toBe(0);
});

test("100 second collaboration timeout has uncertain outcome and manual retry only", async ({ page }) => {
  await page.clock.install();
  let calls = 0;
  let requestStarted;
  const started = new Promise(resolve => { requestStarted = resolve; });
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", () => {
    calls++;
    requestStarted();
  });
  await login(page);
  await page.locator("#draft").fill("uncertain");
  await page.locator("#send").click();
  await started;
  await page.clock.fastForward(100001);
  await expect(page.locator("#error")).toContainText("100 seconds");
  await expect(page.locator("#error")).toContainText("may continue");
  await expect(page.locator("#send")).toBeEnabled();
  await expect(page.locator("#draft")).toHaveValue("uncertain");
  expect(calls).toBe(1);
});

test("non-JSON success and network failure stay explicit", async ({ page }) => {
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route =>
    route.fulfill({ status: 200, contentType: "text/plain", body: "SECRET-BODY" }));
  await login(page);
  await page.locator("#draft").fill("keep");
  await page.locator("#send").click();
  await expect(page.locator("#error")).toContainText("Invalid response");
  await page.route("https://synthetic.lambda-url.us-east-2.on.aws/**", route => route.abort());
  await page.locator("#send").click();
  await expect(page.locator("#error")).toContainText("Network failure");
  await expect(page.locator("#messages article")).toHaveCount(0);
});
