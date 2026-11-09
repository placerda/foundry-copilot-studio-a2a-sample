import test from "node:test";
import assert from "node:assert/strict";
import { Auth, signInErrorMessage, validateConfig } from "../auth.js";

const valid = {
  TENANT_ID: "11111111-1111-1111-1111-111111111111",
  SPA_CLIENT_ID: "22222222-2222-2222-2222-222222222222",
  API_SCOPE: "api://33333333-3333-3333-3333-333333333333/access_as_user",
  API_URL: "/api/chat",
  REDIRECT_URI: "http://localhost:8000/",
};

test("public configuration requires exact tenant, API scope, local route and redirect", () => {
  assert.equal(validateConfig(valid), valid);
  for (const changes of [
    { TENANT_ID: "" }, { SPA_CLIENT_ID: "not-a-guid" },
    { API_SCOPE: "api://------------------------------------/access_as_user" },
    { API_SCOPE: "https://graph.microsoft.com/User.Read" },
    { API_URL: "http://localhost:8000/api/chat" },
    { API_URL: "https://example.com/api/chat" },
    { API_URL: "//example.com/api/chat" },
    { API_URL: "/api/chat?x=y" },
    { API_URL: "/api/chat#fragment" },
    { API_URL: "/api/other" },
    { FUNCTION_URL: "https://obsolete.example.com/" },
    { REDIRECT_URI: "http://127.0.0.1:8000/" },
    { CONTOSO_ENABLED: "true" }, { CONTOSO_ENABLED: 1 }, { CONTOSO_ENABLED: null },
  ]) {
    assert.throws(() => validateConfig({ ...valid, ...changes }), JSON.stringify(changes));
  }
  assert.equal(validateConfig({ ...valid, CONTOSO_ENABLED: true }).CONTOSO_ENABLED, true);
  assert.equal(validateConfig({ ...valid, CONTOSO_ENABLED: false }).CONTOSO_ENABLED, false);
});

test("sign-in explicitly selects an account without changing scopes or token storage", async () => {
  const account = { homeAccountId: "selected-account" };
  const calls = [];
  const auth = {
    config: valid,
    account: null,
    client: {
      async loginPopup(request) {
        calls.push(request);
        return { account };
      },
      setActiveAccount(value) { calls.push(value); },
    },
  };
  await Auth.prototype.login.call(auth);
  assert.deepEqual(calls, [
    { scopes: [valid.API_SCOPE], prompt: "select_account" }, account,
  ]);
  assert.equal(auth.account, account);
});

test("sign-in failures distinguish popup problems without exposing raw authentication errors", () => {
  assert.match(signInErrorMessage({ errorCode: "popup_window_error" }), /Allow popups/);
  assert.match(signInErrorMessage({ errorCode: "empty_window_error" }), /Allow popups/);
  assert.match(signInErrorMessage({ errorCode: "monitor_popup_timeout" }), /timed out/);
  assert.match(signInErrorMessage({ errorCode: "user_cancelled" }), /cancelled/);
  assert.match(signInErrorMessage({
    errorCode: "interaction_in_progress", message: "private diagnostic",
  }), /failed \(interaction_in_progress\)/);
  assert.match(signInErrorMessage({
    errorCode: "endpoints_resolution_error", message: "private diagnostic",
  }), /failed \(endpoints_resolution_error\)/);
  assert.match(signInErrorMessage(new TypeError("private diagnostic")), /failed \(TypeError\)/);
  for (const error of [null, undefined, {}, new Error("private diagnostic"),
    { errorCode: "unknown_secret", message: "private diagnostic" }]) {
    assert.equal(signInErrorMessage(error), "Could not sign in. Check the popup and try again.");
  }
});
