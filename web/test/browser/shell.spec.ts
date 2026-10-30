import { expect, test, type Page, type Route } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const first = {
  id: "0199b167-0000-7000-8000-000000000011",
  name: "Alpha",
  retention_days: 30,
  created_at: "2026-01-01T12:00:00Z",
};
const second = { ...first, id: "0199b167-0000-7000-8000-000000000012", name: "Beta" };

async function projects(page: Page, items = [first, second]) {
  await page.route("**/v1/projects?*", (route) =>
    route.fulfill({ json: { items, next_cursor: null } }),
  );
}

async function signIn(page: Page, key = "test-read-key") {
  await page.getByLabel("API key", { exact: true }).fill(key);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

test("keyboard-only sign in, project switching, routing, skip link and sign out", async ({ page }) => {
  await projects(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to Anvil" })).toBeFocused();
  await expect(page.getByLabel("API key", { exact: true })).toHaveAttribute("type", "password");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("API key", { exact: true })).toBeFocused();
  await page.keyboard.type("test-read-key");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeFocused();
  const request = page.waitForRequest("**/v1/projects?*");
  await page.keyboard.press("Enter");
  expect((await request).headers().authorization).toBe("Bearer test-read-key");
  await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeFocused();
  expect(new URL((await request).url()).searchParams.get("limit")).toBe("100");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Alpha" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Alpha", exact: true })).toBeFocused();
  await expect(page).toHaveTitle("Alpha | Anvil");
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByLabel("Project", { exact: true })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Beta", exact: true })).toBeFocused();
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Alpha", exact: true })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
  const url = page.url();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  expect(page.url()).toBe(url);
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Sign in to Anvil" })).toBeFocused();
});

test("session is memory-only and old project scope cannot leak to a new key", async ({ page }) => {
  let requests = 0;
  await page.route("**/v1/projects?*", (route) => {
    requests++;
    const items = route.request().headers().authorization === "Bearer alpha-key" ? [first] : [second];
    return route.fulfill({ json: { items, next_cursor: null } });
  });
  await page.goto(`/#/projects/${first.id}`);
  expect(requests).toBe(0);
  await signIn(page, "alpha-key");
  await expect(page.getByRole("heading", { name: "Alpha" })).toBeVisible();
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  expect(await page.context().cookies()).toEqual([]);
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "beta-key");
  await expect(page.getByRole("link", { name: "Beta", exact: true })).toBeVisible();
  await page.goto(`/#/projects/${first.id}`);
  await expect(page.getByRole("heading", { name: "Project unavailable" })).toBeVisible();
  await expect(page.getByText("Alpha", { exact: true })).toHaveCount(0);
  const beforeReload = requests;
  await page.reload();
  await expect(page.getByRole("heading", { name: "Sign in to Anvil" })).toBeVisible();
  expect(requests).toBe(beforeReload);
  await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
});

test("empty, malformed and inaccessible project data never claims complete evidence", async ({ page }) => {
  await projects(page, []);
  await page.goto("/");
  await signIn(page);
  await expect(page.getByText("No projects are visible to this key.", { exact: false })).toBeVisible();
  await page.goto("/#/unknown");
  await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
  await page.goto(`/#/projects/${first.id}`);
  await expect(page.getByRole("heading", { name: "Project unavailable" })).toBeVisible();
});

for (const status of [401, 403, 404, 429, 503]) {
  test(`HTTP ${status} is explicit, including non-JSON upstream errors`, async ({ page }) => {
    await page.route("**/v1/projects?*", (route) => route.fulfill({
      status,
      contentType: "text/html",
      headers: { "X-Request-ID": "request-123", "Retry-After": "30" },
      body: "<h1>untrusted upstream detail</h1>",
    }));
    await page.goto("/");
    await signIn(page);
    if (status === 401) {
      await expect(page.getByRole("heading", { name: "Sign in to Anvil" })).toBeVisible();
      await expect(page.getByRole("alert")).toContainText("invalid or expired");
      await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
    } else {
      await expect(page.getByRole("alert")).toContainText("Projects could not be loaded");
      await expect(page.getByRole("alert")).toContainText("request-123");
      await expect(page.getByRole("alert")).toContainText("Retry-After: 30");
      await projects(page);
      await page.getByRole("button", { name: "Retry project request" }).click();
      await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
    }
    await expect(page.getByText("untrusted upstream detail")).toHaveCount(0);
  });
}

for (const body of [
  {},
  { items: [null], next_cursor: null },
  { items: [first], next_cursor: "" },
  { items: [{ ...first, id: "../other-project" }], next_cursor: null },
  { items: [first, first], next_cursor: null },
  { items: [{ ...first, retention_days: 0 }], next_cursor: null },
]) {
  test(`rejects invalid project response ${JSON.stringify(body)}`, async ({ page }) => {
    await page.route("**/v1/projects?*", (route) => route.fulfill({ json: body }));
    await page.goto("/");
    await signIn(page);
    await expect(page.getByRole("alert")).toContainText("invalid project page");
    await expect(page.getByRole("link", { name: "Alpha", exact: true })).toHaveCount(0);
  });
}

test("pagination uses opaque cursors, merges projects and rejects cursor cycles", async ({ page }) => {
  const cursor = "opaque+/=&?cursor";
  await page.route("**/v1/projects?*", (route) => {
    const query = new URL(route.request().url()).searchParams;
    return route.fulfill({ json: query.has("cursor")
      ? { items: [second], next_cursor: null }
      : { items: [first], next_cursor: cursor },
    });
  });
  await page.goto("/");
  await signIn(page);
  await expect(page.getByRole("link", { name: "Alpha", exact: true })).toBeVisible();
  const request = page.waitForRequest((request) => new URL(request.url()).searchParams.get("cursor") === cursor);
  await page.getByRole("button", { name: "Load more projects" }).click();
  await request;
  await expect(page.getByRole("link", { name: "Beta", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Alpha", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more projects" })).toHaveCount(0);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.route("**/v1/projects?*", (route) => route.fulfill({ json: {
    items: [first], next_cursor: cursor,
  } }));
  await signIn(page);
  await page.getByRole("button", { name: "Load more projects" }).click();
  await expect(page.getByRole("alert")).toContainText("repeated a pagination cursor");
});

test("loading, network failure, cancellation and stale responses are handled", async ({ page }) => {
  let pending: Route | undefined;
  await page.route("**/v1/projects?*", (route) => { pending = route; });
  await page.goto("/");
  await signIn(page);
  await expect(page.getByRole("status")).toHaveText("Loading projects...");
  await expect.poll(() => Boolean(pending)).toBe(true);
  const oldRequest = pending!;
  await page.getByRole("button", { name: "Sign out" }).click();
  await projects(page, [second]);
  await signIn(page, "second-key");
  await expect(page.getByRole("link", { name: "Beta", exact: true })).toBeVisible();
  await oldRequest.fulfill({ json: { items: [first], next_cursor: null } }).catch(() => {
    // Chromium may have already discarded the aborted request.
  });
  await expect(page.getByRole("link", { name: "Alpha", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.route("**/v1/projects?*", (route) => route.abort("connectionrefused"));
  await signIn(page);
  await expect(page.getByRole("alert")).toContainText("Projects could not be loaded");
});

test("safe text rendering, no execution requests and automated accessibility", async ({ page }) => {
  const apiRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/v1")) {
      apiRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  const name = '<img src=x onerror="window.unsafe=true">';
  await projects(page, [{ ...first, name }]);
  await page.goto("/");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await signIn(page);
  await page.getByRole("link", { name, exact: true }).click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await expect(page.locator("img")).toHaveCount(0);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(apiRequests.length).toBeGreaterThan(0);
  expect(apiRequests.every((request) => request === "GET /v1/projects")).toBe(true);
});

test("empty keys do not submit and keys are never sent through redirects", async ({ page }) => {
  let redirected = false;
  await page.route("**/v1/projects?*", (route) =>
    route.fulfill({ status: 302, headers: { Location: "/credential-sink" } }),
  );
  await page.route("**/credential-sink", (route) => {
    redirected = true;
    return route.fulfill({ json: {} });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Anvil" })).toBeVisible();
  await page.getByLabel("API key", { exact: true }).fill("   ");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Anvil" })).toBeVisible();
  await signIn(page);
  await expect(page.getByRole("alert")).toContainText("Projects could not be loaded");
  expect(redirected).toBe(false);
});
