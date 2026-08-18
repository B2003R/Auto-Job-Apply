chrome.runtime.onInstalled.addListener(() => {
  console.info("[jobright-stub] service worker installed");
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "ping") {
    sendResponse({ ok: true, source: "jobright-stub-background" });
    return true;
  }
  return false;
});
