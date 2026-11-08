import { Auth, signInErrorMessage } from "./auth.js";
import { Conversation, makePayload } from "./conversation.js";
import { renderMarkdown } from "./markdown.js";
import { makeContosoPayload, validContoso, readResponse, CONTOSO_TIMEOUT_MS, LEGACY_TIMEOUT_MS,
  nativeContosoOptions, NATIVE_CONTOSO_OPTIONS } from "./contoso.js";

const ui = Object.fromEntries(["error", "login", "logout", "reset", "send", "draft",
  "messages", "processing", "auth-status", "chat-form", "contoso-controls", "network-mode",
  "contoso-integration", "studio-variant", "empty-state", "start-hint"].map(id => [id, document.getElementById(id)]));
let auth;
let config;
let pending = false;
let signingOut = false;
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
  ui.login.disabled = !auth || !!auth.account || pending || signingOut;
  ui.logout.disabled = !auth?.account || signingOut;
  ui.reset.disabled = !auth?.account;
  ui.send.disabled = !auth?.account || pending;
  ui.draft.disabled = !auth?.account || pending;
  ui.processing.textContent = pending ? "Processing..." : "";
  ui["auth-status"].textContent = auth?.account ? "Signed in." : "Sign in to chat.";
  const contosoEnabled = config?.CONTOSO_ENABLED === true;
  ui["contoso-controls"].hidden = !contosoEnabled;
  ui["network-mode"].disabled = !contosoEnabled || !auth?.account || pending || signingOut;
  const contosoSelected = contosoEnabled && ui["network-mode"].value === "contoso";
  ui["contoso-integration"].disabled = !contosoSelected || !auth?.account || pending || signingOut;
  ui["studio-variant"].disabled = ui["contoso-integration"].disabled;
  ui["empty-state"].hidden = conversation.confirmedPairs.length > 0;
  ui["start-hint"].textContent = auth?.account
    ? "Choose a suggested question or write your own. Review the draft, then choose Send."
    : "Sign in with Microsoft to start. No question is sent until you choose Send.";
  ui.messages.setAttribute("aria-busy", String(pending));
  for (const button of document.querySelectorAll("[data-prompt]")) {
    button.disabled = !auth?.account || pending || signingOut;
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

export function validResponse(value, options = null) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === (options ? 4 : 3)
    && Object.hasOwn(value, "answer") && Object.hasOwn(value, "request_id")
    && Object.hasOwn(value, "output_limited")
    && typeof value.answer === "string" && value.answer.trim().length > 0
    && !/[\uD800-\uDFFF]/u.test(value.answer)
    && typeof value.request_id === "string" && value.request_id.length > 0
    && typeof value.output_limited === "boolean"
    && (!options || validContoso(value.contoso, options));
}

try {
  ({ default: config } = await import("./config.js"));
  auth = new Auth(config);
  await auth.initialize();
  render();
} catch {
  auth = null;
  ui.error.textContent = "Invalid configuration. Complete frontend/config.js before using the app.";
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
  if (!auth?.account || pending) return;
  conversation.draft = ui.draft.value;
  const draft = conversation.draft;
  let payload;
  const options = config.CONTOSO_ENABLED === true && ui["network-mode"].value === "contoso"
    ? { integration: ui["contoso-integration"].value, studio_variant: ui["studio-variant"].value } : null;
  try {
    if (options && !nativeContosoOptions(options)) throw new Error("Deferred agent route.");
    payload = options ? makeContosoPayload(conversation.confirmedPairs, draft, options)
      : makePayload(conversation.confirmedPairs, draft);
  } catch {
    ui.error.textContent = options
      ? "Enter a valid question of up to 2,000 characters within the byte limit."
      : "Enter a valid message of up to 4,000 characters within the byte limit.";
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
  const timeoutMs = options ? CONTOSO_TIMEOUT_MS : LEGACY_TIMEOUT_MS;
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
    const response = await fetch(config.FUNCTION_URL, {
      method: "POST", credentials: "omit",
      signal: operation.controller.signal,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      throw new RequestFailure(options && response.status === 502 ? "The agent network connection is unavailable."
        : statusMessages[response.status] || "Unexpected service failure.");
    }
    phase = "response";
    if (!response.headers.get("content-type")?.toLowerCase().includes("application/json")) {
      throw new RequestFailure("Invalid response from the service.");
    }
    const result = options ? await readResponse(response) : await response.json();
    if (!validResponse(result, options)) throw new RequestFailure("Invalid response from the service.");
    if (current() && conversation.confirm(generation, draft, result)) {
      append("user", draft);
      append("assistant", result.answer, result.output_limited, result.contoso, result.request_id);
      ui.draft.value = "";
    }
  } catch (error) {
    if (current()) {
      let message;
      if (timedOut) {
        message = `The request timed out after ${timeoutMs / 1000} seconds. Execution may continue in AWS; the outcome is uncertain.`;
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

function resetConversation(resetControls = true) {
  conversation.reset();
  ui.messages.replaceChildren();
  ui.draft.value = "";
  ui.error.textContent = "";
  if (resetControls) {
    ui["network-mode"].value = "legacy";
    ui["contoso-integration"].value = NATIVE_CONTOSO_OPTIONS.integration;
    ui["studio-variant"].value = NATIVE_CONTOSO_OPTIONS.studio_variant;
  }
  render();
}

ui.reset.addEventListener("click", () => resetConversation());
for (const button of document.querySelectorAll("[data-prompt]")) {
  button.addEventListener("click", () => {
    if (!auth?.account || pending || signingOut) return;
    ui.draft.value = button.dataset.prompt;
    ui.draft.focus();
  });
}
for (const id of ["network-mode", "contoso-integration", "studio-variant"]) {
  ui[id].addEventListener("change", () => resetConversation(false));
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
