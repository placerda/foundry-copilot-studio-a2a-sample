import {
  PublicClientApplication, InteractionRequiredAuthError, BrowserAuthErrorCodes,
  BrowserConfigurationAuthErrorCodes, ClientAuthErrorCodes, ClientConfigurationErrorCodes,
} from "@azure/msal-browser";

const guid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const authErrorCodes = new Set([
  ...Object.values(BrowserAuthErrorCodes), ...Object.values(BrowserConfigurationAuthErrorCodes),
  ...Object.values(ClientAuthErrorCodes), ...Object.values(ClientConfigurationErrorCodes),
]);

export function signInErrorMessage(error) {
  const code = error?.errorCode;
  switch (code) {
    case "popup_window_error":
    case "empty_window_error":
      return "Could not open Microsoft sign-in. Allow popups or open http://localhost:8000/ in your browser.";
    case "monitor_popup_timeout":
      return "Microsoft sign-in timed out. Complete the popup and try signing in again.";
    case "user_cancelled":
      return "Microsoft sign-in was cancelled. Sign in again to continue.";
    default:
      if (authErrorCodes.has(code)) {
        return `Microsoft sign-in failed (${code}). Open http://localhost:8000/ in your browser and try again.`;
      }
      if (error instanceof TypeError) {
        return "Microsoft sign-in failed (TypeError). Open http://localhost:8000/ in your browser and try again.";
      }
      return "Could not sign in. Check the popup and try again.";
  }
}

export function validateConfig(config) {
  const apiId = typeof config?.API_SCOPE === "string"
    ? /^api:\/\/(.+)\/access_as_user$/i.exec(config.API_SCOPE)?.[1] : null;
  if (!config || !guid.test(config.TENANT_ID) || !guid.test(config.SPA_CLIENT_ID)
      || !guid.test(apiId)
      || ("CONTOSO_ENABLED" in config && typeof config.CONTOSO_ENABLED !== "boolean")
      || config.REDIRECT_URI !== "http://localhost:8000/") {
    throw new Error("Invalid public configuration.");
  }
  if (config.API_URL !== "/api/chat" || "FUNCTION_URL" in config) {
    throw new Error("Invalid local API route.");
  }
  return config;
}

export class Auth {
  constructor(config) {
    this.config = validateConfig(config);
    this.account = null;
    this.client = new PublicClientApplication({
      auth: {
        clientId: config.SPA_CLIENT_ID,
        authority: `https://login.microsoftonline.com/${config.TENANT_ID}`,
        redirectUri: config.REDIRECT_URI,
        postLogoutRedirectUri: config.REDIRECT_URI,
      },
      cache: { cacheLocation: "memoryStorage", temporaryCacheLocation: "memoryStorage" },
      system: { loggerOptions: { piiLoggingEnabled: false, loggerCallback: () => {} } },
    });
  }

  async initialize() { await this.client.initialize(); }

  async login() {
    const result = await this.client.loginPopup({
      scopes: [this.config.API_SCOPE], prompt: "select_account",
    });
    if (!result.account) throw new Error("Could not sign in.");
    this.account = result.account;
    this.client.setActiveAccount(result.account);
  }

  async token(account) {
    if (!account || account !== this.account) throw new Error("Sign in again.");
    const request = { account, scopes: [this.config.API_SCOPE] };
    let result;
    try {
      result = await this.client.acquireTokenSilent(request);
    } catch (error) {
      if (!(error instanceof InteractionRequiredAuthError)) throw error;
      if (account !== this.account) throw new Error("Session ended.");
      result = await this.client.acquireTokenPopup(request);
    }
    if (typeof result.accessToken !== "string" || !result.accessToken) {
      throw new Error("Access token unavailable.");
    }
    return result.accessToken;
  }

  async logout() {
    const account = this.account;
    this.account = null;
    this.client.setActiveAccount(null);
    await this.client.clearCache();
    await this.client.logoutPopup({ account, mainWindowRedirectUri: this.config.REDIRECT_URI });
  }
}
