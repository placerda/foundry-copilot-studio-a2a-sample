export class InteractionRequiredAuthError extends Error {}

export const BrowserAuthErrorCodes = { interactionInProgress: "interaction_in_progress" };
export const BrowserConfigurationAuthErrorCodes = {};
export const ClientAuthErrorCodes = { endpointsResolutionError: "endpoints_resolution_error" };
export const ClientConfigurationErrorCodes = {};

export class PublicClientApplication {
  constructor(options) {
    window.fakeAuthOptions = options;
    this.account = null;
  }
  async initialize() {
    if (window.initializeFailure) throw new Error("Synthetic private diagnostic");
  }
  setActiveAccount(account) { this.account = account; }
  getActiveAccount() { return this.account; }
  async clearCache() { this.account = null; }
  async loginPopup() {
    if (window.loginErrorCode) {
      throw Object.assign(new Error("Synthetic private diagnostic"), { errorCode: window.loginErrorCode });
    }
    if (window.loginFailure) throw new Error("Synthetic login failure");
    return { account: { homeAccountId: "test-owner" } };
  }
  async acquireTokenSilent() {
    if (window.tokenDelay) await new Promise(resolve => setTimeout(resolve, window.tokenDelay));
    if (window.interactionRequired) throw new InteractionRequiredAuthError();
    if (window.tokenFailure) throw new Error("Synthetic token failure");
    return { accessToken: "synthetic-access-token" };
  }
  async acquireTokenPopup() {
    window.popupCalls = (window.popupCalls || 0) + 1;
    return { accessToken: "synthetic-access-token" };
  }
  async logoutPopup() {
    if (window.logoutFailure) throw new Error("Synthetic logout failure");
  }
}
