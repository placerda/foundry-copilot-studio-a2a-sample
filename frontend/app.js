import { Auth, signInErrorMessage } from "./auth.js";
import { Conversation } from "./conversation.js";
import { renderMarkdown } from "./markdown.js";
import { validateCapabilities, selectedMode, validIdentity, SHARED_MODE } from "./identity.js";
import { makeContosoPayload, validContoso, readResponse, CONTOSO_TIMEOUT_MS,
  NATIVE_CONTOSO_OPTIONS } from "./contoso.js";

const ui = Object.fromEntries(["error", "login", "logout", "reset", "send", "draft",
  "messages", "processing", "auth-status", "chat-form",
  "empty-state", "start-hint", "identity-reason",
  "identity-callers"].map(id => [id, document.getElementById(id)]));
let auth;
let config;
let pending = false;
let signingOut = false;
let identityReadiness = null;
const conversation = new Conversation();
const statusMessages = {
  400: "Invalid message or message exceeds the limits.",
  401: "Invalid or expired session. Sign in again before resending.",
  403: "This user does not have access to the chat.",
  405: "The service does not support this method.",
  429: "The service is busy. Wait before resending.",
  500: "Internal service error.",
  502: "The service is temporarily unavailable.",
  503: "The agent network connection is unavailable.",
};

class RequestFailure extends Error {}

function render() {
  const contosoEnabled = config?.CONTOSO_ENABLED === true;
  const identityReady = identityReadiness?.modes[SHARED_MODE]?.enabled === true;
  ui.login.hidden = !!auth?.account;
  ui.logout.hidden = !auth?.account;
  ui.reset.hidden = !auth?.account;
  ui.login.disabled = !auth || !!auth.account || pending || signingOut;
  ui.logout.disabled = !auth?.account || signingOut;
  ui.reset.disabled = !auth?.account;
  ui.send.disabled = !auth?.account || !contosoEnabled || !identityReady || pending || signingOut;
  ui.draft.disabled = ui.send.disabled;
  ui.processing.textContent = pending ? "Processing..." : "";
  ui["auth-status"].textContent = auth?.account
    ? (contosoEnabled ? "Signed in." : "Agent network is not configured.")
    : "Sign in to chat.";
  ui["empty-state"].hidden = conversation.confirmedPairs.length > 0;
  ui["start-hint"].textContent = !contosoEnabled
    ? "Configure the A2A connection before starting a conversation."
    : auth?.account
    ? "Choose a suggested question or write your own. Review the draft, then choose Send."
    : "Sign in with Microsoft to start. No question is sent until you choose Send.";
  ui.messages.setAttribute("aria-busy", String(pending));
  for (const button of document.querySelectorAll("[data-prompt]")) {
    button.disabled = ui.send.disabled;
  }
}

function append(role, content, limited = false, contoso = null, requestId = null) {
  const article = document.createElement("article");
  article.dataset.role = role;
  const heading = document.createElement("strong");
  heading.textContent = role === "user" ? "You" : "Assistant";
  const paragraph = role === "assistant" ? renderMarkdown(content) : document.createElement("p");
  if (role === "user") {
    paragraph.className = "content user-content";
    paragraph.textContent = content;
  }
  article.append(heading, paragraph);
  if (limited) {
    const notice = document.createElement("p");
    notice.className = "limit";
    notice.textContent = "The response reached the generation limit and may be incomplete.";
    article.append(notice);
  }
  if (contoso) {
    const metadata = document.createElement("section");
    metadata.className = "contoso-metadata";
    metadata.setAttribute("aria-label", "Agent response attribution");
    const add = (tag, value) => {
      const element = document.createElement(tag);
      element.textContent = value;
      metadata.append(element);
      return element;
    };
    const status = add("p", `Status: ${contoso.status}`);
    status.className = "response-status";
    add("h4", "Sources");
    if (!contoso.sources.length) add("p", "No sources returned.");
    for (const source of contoso.sources) {
      add("p", `${source.source_id} · ${source.title} · Version: ${source.version} · `
        + `Provider: ${source.provider_id} · Classification: ${source.classification}`);
    }
    if (contoso.citations.length) add("h4", "Citations");
    for (const citation of contoso.citations) {
      add("p", `${citation.source_id} · Version: ${citation.version} · Section: ${citation.section}`);
    }
    const technical = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Technical details and execution trace";
    technical.append(summary);
    const detail = (tag, value) => {
      const node = document.createElement(tag);
      node.textContent = value;
      technical.append(node);
      return node;
    };
    if (requestId) detail("p", `Request ID: ${requestId}`);
    if (contoso.provider_id !== null) detail("p", `Provider: ${contoso.provider_id}`);
    if (contoso.studio_variant !== null) detail("p", `Copilot Studio configuration: ${contoso.studio_variant}`);
    detail("h4", "Execution trace");
    const list = detail("ol", "");
    for (const step of contoso.execution_path) {
      const item = document.createElement("li");
      item.textContent = step;
      list.append(item);
    }
    metadata.append(technical);
    article.append(metadata);
  } else if (requestId) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Technical details";
    const request = document.createElement("p");
    request.textContent = `Request ID: ${requestId}`;
    details.append(summary, request);
    article.append(details);
  }
  ui.messages.append(article);
}

export function validResponse(value, options = null, mode = "shared-delegated") {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === (options ? 4 : 3) + (Object.hasOwn(value, "identity") ? 1 : 0)
    && Object.hasOwn(value, "answer") && Object.hasOwn(value, "request_id")
    && Object.hasOwn(value, "output_limited")
    && typeof value.answer === "string" && value.answer.trim().length > 0
    && !/[\uD800-\uDFFF]/u.test(value.answer)
    && typeof value.request_id === "string" && value.request_id.length > 0
    && typeof value.output_limited === "boolean"
    && (!Object.hasOwn(value, "identity") || validIdentity(value.identity, mode))
    && (!options || validContoso(value.contoso, options));
}

let startupStage = "configuration";
try {
  ({ default: config } = await import("./config.js"));
  auth = new Auth(config);
  startupStage = "authentication";
  await auth.initialize();
  startupStage = "readiness";
  const readinessResponse = await fetch("/api/capabilities", {
    credentials: "omit", cache: "no-store", signal: AbortSignal.timeout(5000),
  });
  if (!readinessResponse.ok) throw new Error("Identity readiness unavailable.");
  identityReadiness = validateCapabilities(await readResponse(readinessResponse));
  ui["identity-reason"].textContent = identityReadiness.modes[SHARED_MODE].reason;
  if (config.CONTOSO_ENABLED !== true) {
    ui.error.textContent = "Agent network is not configured. Enable CONTOSO_ENABLED only after configuring the A2A connection.";
  }
  render();
} catch {
  auth = null;
  ui["identity-reason"].textContent = "Availability could not be verified.";
  ui.error.textContent = startupStage === "configuration"
    ? "Invalid configuration. Complete frontend/config.js before using the app."
    : startupStage === "authentication"
    ? "Microsoft sign-in could not initialize. Reload this page in a supported browser."
    : "Identity readiness is unavailable or invalid. Check the local Python server and reload.";
  render();
}

ui.login.addEventListener("click", async () => {
  if (!auth || pending) return;
  pending = true;
  ui.error.textContent = "";
  render();
  try { await auth.login(); }
  catch (error) { ui.error.textContent = signInErrorMessage(error); }
  finally { pending = false; render(); }
});

ui["chat-form"].addEventListener("submit", async event => {
  event.preventDefault();
  if (!auth?.account || pending || signingOut) return;
  if (config.CONTOSO_ENABLED !== true) {
    ui.error.textContent = "Only the configured A2A collaboration route is available. No request was sent.";
    return;
  }
  conversation.draft = ui.draft.value;
  const draft = conversation.draft;
  let payload;
  let mode;
  const options = NATIVE_CONTOSO_OPTIONS;
  try {
    mode = selectedMode(SHARED_MODE, identityReadiness);
    payload = makeContosoPayload(conversation.confirmedPairs, draft, options, mode);
  } catch {
    ui.error.textContent = identityReadiness?.modes[SHARED_MODE]?.enabled === true
      ? "Enter a valid question of up to 2,000 characters within the byte limit."
      : "The shared delegated connection is unavailable. No request was sent.";
    return;
  }
  pending = true;
  const generation = conversation.generation;
  const account = auth.account;
  const operation = { controller: new AbortController() };
  conversation.pending = operation;
  const current = () => generation === conversation.generation && auth.account === account;
  let phase = "token";
  let timedOut = false;
  let timer;
  const timeoutMs = CONTOSO_TIMEOUT_MS;
  ui.error.textContent = "";
  render();
  try {
    const token = await auth.token(account);
    if (!current()) return;
    phase = "network";
    timer = setTimeout(() => {
      timedOut = true;
      operation.controller.abort();
    }, timeoutMs);
    const response = await fetch(config.API_URL, {
      method: "POST", credentials: "omit",
      signal: operation.controller.signal,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      throw new RequestFailure(response.status === 502 ? "The agent network connection is unavailable."
        : statusMessages[response.status] || "Unexpected service failure.");
    }
    phase = "response";
    if (!response.headers.get("content-type")?.toLowerCase().includes("application/json")) {
      throw new RequestFailure("Invalid response from the service.");
    }
    const result = await readResponse(response);
    if (!validResponse(result, options, mode)) throw new RequestFailure("Invalid response from the service.");
    if (current() && conversation.confirm(generation, draft, result)) {
      if (result.identity) {
        ui["identity-callers"].textContent = `App user: ${result.identity.application_caller.object_id} `
          + `(tenant ${result.identity.application_caller.tenant_id}). Studio identity: shared connection (unverified).`;
      }
      append("user", draft);
      append("assistant", result.answer, result.output_limited, result.contoso, result.request_id);
      ui.draft.value = "";
    }
  } catch (error) {
    if (current()) {
      let message;
      if (timedOut) {
        message = `The request timed out after ${timeoutMs / 1000} seconds. Downstream execution may continue; the outcome is uncertain.`;
      } else if (error instanceof RequestFailure) {
        message = error.message;
      } else if (phase === "token") {
        message = "Authentication failed. Check the popup and sign in again if necessary.";
      } else if (phase === "response") {
        message = "Invalid response from the service.";
      } else {
        message = "Network failure. The outcome may be uncertain.";
      }
      ui.error.textContent = `${message} Your message was preserved for manual resending.`;
    }
  } finally {
    clearTimeout(timer);
    conversation.pending = null;
    pending = false;
    render();
    if (current() && conversation.confirmedPairs.length) {
      const body = ui.messages.closest(".conversation-body");
      if (body) body.scrollTop = ui.messages.lastElementChild.offsetTop - body.offsetTop - 20;
    }
  }
});

function resetConversation() {
  conversation.reset();
  ui.messages.replaceChildren();
  ui.draft.value = "";
  ui.error.textContent = "";
  ui["identity-callers"].textContent = "App user: not verified. Studio identity: shared connection (unverified).";
  render();
}

ui.reset.addEventListener("click", () => resetConversation());
for (const button of document.querySelectorAll("[data-prompt]")) {
  button.addEventListener("click", () => {
    if (ui.send.disabled) return;
    ui.draft.value = button.dataset.prompt;
    ui.draft.focus();
  });
}
ui.logout.addEventListener("click", async () => {
  if (!auth?.account || signingOut) return;
  signingOut = true;
  resetConversation();
  const logout = auth.logout();
  render();
  try { await logout; }
  catch {
    ui.error.textContent = "You signed out locally. Your Microsoft SSO session may still be active.";
  } finally { signingOut = false; render(); }
});
