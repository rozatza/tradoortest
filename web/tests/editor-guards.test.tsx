import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StrategyEditor } from "@/components/strategy-editor";
import type { ProposalView } from "@/lib/types";
import { STRATEGY_ID } from "@/tests/fixtures";

const HEAD_HASH = "b".repeat(64);
const NEXT_HASH = "c".repeat(64);
const LATEST = 'strategy ema_cross:\n  name: "EMA cross"\n  warmup_bars: 30';
const PREVIOUS = 'strategy ema_cross:\n  name: "EMA cross"\n  warmup_bars: 26';
const EDITED = `${LATEST}\n  note: "changed"`;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function detail(version: number, dsl: string, hash: string, latest = version) {
  return {
    strategy_id: STRATEGY_ID,
    name: "EMA cross",
    latest_version: latest,
    ir_hash: latest === version ? hash : HEAD_HASH,
    version: {
      version,
      dsl,
      ir_hash: hash,
      created_at: "2026-01-01T00:00:00Z",
      ir: { bars: [{ close: "secret-bar" }] },
    },
  };
}

function versionsPage() {
  return {
    strategy_id: STRATEGY_ID,
    versions: [
      { version: 1, ir_hash: "a".repeat(64), created_at: "2026-01-01T00:00:00Z" },
      { version: 2, ir_hash: HEAD_HASH, created_at: "2026-01-02T00:00:00Z" },
    ],
    limit: 100,
    offset: 0,
    total: 2,
  };
}

function install(
  validate: (body: { dsl?: string }) => Response,
  save: (body: { dsl?: string; expected_version?: number; expected_ir_hash?: string }) => Response,
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? "GET";
    if (url.includes("/versions/2")) {
      return jsonResponse({
        strategy_id: STRATEGY_ID,
        version: 2,
        dsl: LATEST,
        ir_hash: HEAD_HASH,
        created_at: "2026-01-02T00:00:00Z",
      });
    }
    if (url.includes("/versions/")) {
      return jsonResponse({
        strategy_id: STRATEGY_ID,
        version: 1,
        dsl: PREVIOUS,
        ir_hash: "a".repeat(64),
        created_at: "2026-01-01T00:00:00Z",
        ir: { bars: [{ close: "secret-bar" }] },
      });
    }
    if (url.includes("/versions")) {
      return jsonResponse(versionsPage());
    }
    if (url.includes("/validate")) {
      return validate(JSON.parse(String(init?.body)) as { dsl?: string });
    }
    if (method === "PUT") {
      return save(
        JSON.parse(String(init?.body)) as {
          dsl?: string;
          expected_version?: number;
          expected_ir_hash?: string;
        },
      );
    }
    if (url.includes(STRATEGY_ID)) {
      return jsonResponse(detail(2, LATEST, HEAD_HASH));
    }
    throw new Error(`unexpected ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function puts(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter((call) => (call[1] as RequestInit | undefined)?.method === "PUT");
}

function renderEditor(proposals: ProposalView[] = []) {
  const onAccept = vi.fn();
  const onReject = vi.fn();
  render(
    <StrategyEditor
      strategyId={STRATEGY_ID}
      refreshToken={0}
      proposals={proposals}
      busy={false}
      onAccept={onAccept}
      onReject={onReject}
      onSelectStrategy={vi.fn()}
    />,
  );
  return { onAccept, onReject };
}

async function loadedBox(): Promise<HTMLTextAreaElement> {
  const box = (await screen.findByLabelText("Strategy DSL")) as HTMLTextAreaElement;
  await waitFor(() => {
    expect(box.value).toBe(LATEST);
  });
  return box;
}

const OK = () => jsonResponse({ valid: true, strategy_id: STRATEGY_ID, name: "EMA cross", ir_hash: NEXT_HASH });

function htmlResponse(status: number): Response {
  return new Response("<html>nope</html>", {
    status,
    headers: { "content-type": "text/html" },
  });
}

async function expectEnabled(name: string) {
  await waitFor(() => {
    expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(false);
  });
}

describe("editor save guards", () => {
  it("never saves on typing, blur, keys, timers, or unload", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = install(OK, () => jsonResponse(detail(3, EDITED, NEXT_HASH)));
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.blur(box);
    fireEvent.keyDown(box, { key: "s", ctrlKey: true });
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    window.dispatchEvent(new Event("beforeunload"));
    window.dispatchEvent(new Event("pagehide"));
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(120_000);
    expect(puts(fetchMock)).toHaveLength(0);
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/validate"))).toBe(false);
  });

  it("saves the head pair after viewing an older version", async () => {
    const seen: Array<{ expected_version?: number; expected_ir_hash?: string }> = [];
    install(OK, (body) => {
      seen.push(body);
      return jsonResponse(detail(3, EDITED, NEXT_HASH));
    });
    renderEditor();
    await loadedBox();
    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "1" } });
    await waitFor(() => expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(PREVIOUS));
    fireEvent.change(screen.getByLabelText("Strategy DSL"), { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    await screen.findByText("Saved version 3.");
    expect(seen).toHaveLength(1);
    expect(seen[0]?.expected_version).toBe(2);
    expect(seen[0]?.expected_ir_hash).toBe(HEAD_HASH);
  });

  it("uses the head returned by the previous save", async () => {
    const seen: Array<{ expected_version?: number; expected_ir_hash?: string }> = [];
    let n = 0;
    install(OK, (body) => {
      seen.push(body);
      n += 1;
      return n === 1
        ? jsonResponse(detail(3, EDITED, NEXT_HASH))
        : jsonResponse(detail(4, `${EDITED}2`, "d".repeat(64)));
    });
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    await screen.findByText("Saved version 3.");
    fireEvent.change(screen.getByLabelText("Strategy DSL"), { target: { value: `${EDITED}2` } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    await screen.findByText("Saved version 4.");
    expect(seen[1]?.expected_version).toBe(3);
    expect(seen[1]?.expected_ir_hash).toBe(NEXT_HASH);
  });

  it("shows a server diagnostic on save and does not apply it", async () => {
    install(OK, () =>
      jsonResponse(
        {
          error: {
            code: "syntax",
            message: "bad",
            path: "$",
            diagnostics: [{ code: "syntax", message: "server says bad", path: "$", line: 3, column: 1 }],
          },
        },
        422,
      ),
    );
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("server says bad");
    expect(screen.queryByText(/Saved version/)).toBeNull();
    await expectEnabled("Save version");
  });

  it("issues one PUT when Save is clicked twice", async () => {
    const fetchMock = install(OK, () => jsonResponse(detail(3, EDITED, NEXT_HASH)));
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    const btn = screen.getByRole("button", { name: "Save version" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await screen.findByText("Saved version 3.");
    expect(puts(fetchMock)).toHaveLength(1);
  });

  it("does not PUT when Validate succeeds", async () => {
    const fetchMock = install(OK, () => jsonResponse(detail(3, EDITED, NEXT_HASH)));
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    await screen.findByText("Valid. Nothing was saved.");
    expect(puts(fetchMock)).toHaveLength(0);
  });

  it("asks before replacing unsaved edits with another strategy", async () => {
    const fetchMock = install(OK, () => jsonResponse(detail(3, EDITED, NEXT_HASH)));
    const onSelect = vi.fn();
    const { rerender } = render(
      <StrategyEditor
        strategyId={STRATEGY_ID}
        refreshToken={0}
        proposals={[]}
        busy={false}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        onSelectStrategy={onSelect}
      />,
    );
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    const before = fetchMock.mock.calls.length;
    rerender(
      <StrategyEditor
        strategyId={"f".repeat(32)}
        refreshToken={0}
        proposals={[]}
        busy={false}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        onSelectStrategy={onSelect}
      />,
    );
    await screen.findByText(/Unsaved edits are still in the editor/);
    expect(fetchMock.mock.calls.length).toBe(before);
    expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(EDITED);
  });

  it("does not overwrite a dirty draft when the file is refreshed", async () => {
    const fetchMock = install(OK, () => jsonResponse(detail(3, EDITED, NEXT_HASH)));
    const { rerender } = render(
      <StrategyEditor
        strategyId={STRATEGY_ID}
        refreshToken={0}
        proposals={[]}
        busy={false}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        onSelectStrategy={vi.fn()}
      />,
    );
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    const before = fetchMock.mock.calls.length;
    rerender(
      <StrategyEditor
        strategyId={STRATEGY_ID}
        refreshToken={1}
        proposals={[]}
        busy={false}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        onSelectStrategy={vi.fn()}
      />,
    );
    await screen.findByText(/This editor was not overwritten/);
    expect(fetchMock.mock.calls.length).toBe(before);
    expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(EDITED);
  });
});

describe("editor failures", () => {
  it("shows a 500 from save and re-enables Save", async () => {
    install(OK, () => new Response("", { status: 500 }));
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error: request failed (500)");
    expect(screen.queryByText(/Saved version/)).toBeNull();
    await expectEnabled("Save version");
  });

  it("shows a 500 from validate and re-enables Validate", async () => {
    install(() => new Response("", { status: 500 }), () => jsonResponse({}));
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error: request failed (500)");
    expect(screen.queryByText("Valid. Nothing was saved.")).toBeNull();
    await expectEnabled("Validate");
  });

  it("shows a non-JSON save response and re-enables Save", async () => {
    install(OK, () => htmlResponse(502));
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error: request failed (502)");
    await expectEnabled("Save version");
  });

  it("shows a non-JSON validate response and re-enables Validate", async () => {
    install(() => htmlResponse(502), () => jsonResponse({}));
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error: request failed (502)");
    await expectEnabled("Validate");
  });

  it("shows a rejected save fetch and re-enables Save", async () => {
    install(OK, () => {
      throw new TypeError("Failed to fetch");
    });
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error: Failed to fetch");
    await expectEnabled("Save version");
  });

  it("shows a rejected validate fetch and re-enables Validate", async () => {
    install(
      () => {
        throw new TypeError("Failed to fetch");
      },
      () => jsonResponse({}),
    );
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error: Failed to fetch");
    await expectEnabled("Validate");
  });

  it("re-enables Show saved strategies when the list request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    render(
      <StrategyEditor
        strategyId={null}
        refreshToken={0}
        proposals={[]}
        busy={false}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        onSelectStrategy={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Show saved strategies" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error: Failed to fetch");
    await expectEnabled("Show saved strategies");
  });
});
