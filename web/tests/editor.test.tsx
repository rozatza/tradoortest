import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Chat } from "@/components/chat";
import { StrategyEditor } from "@/components/strategy-editor";
import { formatSse } from "@/lib/sse";
import type { ProposalView } from "@/lib/types";
import { agentEvent, PROPOSAL_ID, SESSION_ID, STRATEGY_ID } from "@/tests/fixtures";

const HEAD_HASH = "b".repeat(64);
const NEXT_HASH = "c".repeat(64);
const LATEST = 'strategy ema_cross:\n  name: "EMA cross"\n  warmup_bars: 30';
const PREVIOUS = 'strategy ema_cross:\n  name: "EMA cross"\n  warmup_bars: 26';
const EDITED = `${LATEST}\n  note: "changed"`;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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

function optionNames(): string[] {
  const select = screen.getByLabelText("Version") as HTMLSelectElement;
  return Array.from(select.options).map((option) => option.text);
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

describe("strategy editor", () => {
  it("blocks an invalid save, shows the position, and does not write", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const fetchMock = install(
      () =>
        jsonResponse(
          {
            error: {
              code: "syntax",
              message: "not a strategy",
              path: "$",
              diagnostics: [
                { code: "syntax", message: "not a strategy", path: "$", line: 4, column: 2 },
              ],
            },
          },
          422,
        ),
      () => jsonResponse({}),
    );
    renderEditor();
    const box = await loadedBox();
    expect(document.body.textContent).not.toContain("secret-bar");
    fireEvent.change(box, { target: { value: EDITED } });
    expect(puts(fetchMock)).toHaveLength(0);
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/validate"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Line 4, column 2: not a strategy");
    expect(document.querySelector('[data-line="4"]')?.className).toContain("line-error");
    expect(puts(fetchMock)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter((call) => String(call[0]).includes("/validate"))).toHaveLength(2);
    });
    expect(puts(fetchMock)).toHaveLength(0);
    expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(EDITED);
    expect(optionNames()).toContain("v2");
    expect(setItem).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/tools/"))).toBe(false);
  });

  it("validates and then saves a new version with the head hash", async () => {
    const fetchMock = install(
      () => jsonResponse({ valid: true, strategy_id: STRATEGY_ID, name: "EMA cross", ir_hash: NEXT_HASH }),
      (body) => {
        expect(body.expected_version).toBe(2);
        expect(body.expected_ir_hash).toBe(HEAD_HASH);
        expect(body.dsl).toBe(EDITED);
        return jsonResponse(detail(3, EDITED, NEXT_HASH));
      },
    );
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    expect(await screen.findByText("Saved version 3.")).toBeTruthy();
    expect(puts(fetchMock)).toHaveLength(1);
    expect(optionNames()).toContain("v3");
    expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(EDITED);
  });

  it("shows a stale save as a conflict and does not retry it", async () => {
    const fetchMock = install(
      () => jsonResponse({ valid: true }),
      () =>
        jsonResponse(
          {
            error: {
              code: "version_conflict",
              message: "strategy version conflict",
              path: "$.expected_version",
              current_version: 4,
              current_ir_hash: NEXT_HASH,
            },
          },
          409,
        ),
    );
    renderEditor();
    const box = await loadedBox();
    fireEvent.change(box, { target: { value: EDITED } });
    fireEvent.click(screen.getByRole("button", { name: "Save version" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("version_conflict: strategy version conflict");
    expect(alert.textContent).toContain("Current version is 4");
    expect(alert.textContent).toContain("was not saved");
    expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(EDITED);
    await waitFor(() => {
      expect(puts(fetchMock)).toHaveLength(1);
    });
    expect(optionNames()).toContain("v2");
    expect(optionNames()).not.toContain("v4");
  });

  it("loads a prior version and does not replace unsaved edits until asked", async () => {
    const fetchMock = install(
      () => jsonResponse({ valid: true }),
      () => jsonResponse({}),
    );
    renderEditor();
    const box = await loadedBox();
    expect(optionNames()).toContain("v1");
    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "1" } });
    await waitFor(() => {
      expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/versions/1"))).toBe(true);
    });
    await waitFor(() => {
      expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(PREVIOUS);
    });
    expect(puts(fetchMock)).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Strategy DSL"), { target: { value: `${PREVIOUS}\n# edit` } });
    const before = fetchMock.mock.calls.length;
    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "2" } });
    expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toContain("# edit");
    expect(screen.getByText(/Load version 2 and discard unsaved edits/)).toBeTruthy();
    expect(fetchMock.mock.calls.length).toBe(before);
    fireEvent.click(screen.getByRole("button", { name: "Load version" }));
    await waitFor(() => {
      expect((screen.getByLabelText("Strategy DSL") as HTMLTextAreaElement).value).toBe(LATEST);
    });
    expect(box).toBeTruthy();
  });

  it("accepts a proposal only from the button", async () => {
    install(
      () => jsonResponse({ valid: true }),
      () => jsonResponse({}),
    );
    const proposal: ProposalView = {
      seq: 2,
      proposalId: PROPOSAL_ID,
      strategyId: STRATEGY_ID,
      fromVersion: 2,
      toVersion: 3,
      status: "pending",
      diff: "--- a\n+++ b\n",
      notice: null,
    };
    const { onAccept, onReject } = renderEditor([proposal]);
    const accept = await screen.findByRole("button", { name: "Accept" });
    expect(onAccept).not.toHaveBeenCalled();
    expect(onReject).not.toHaveBeenCalled();
    fireEvent.click(accept);
    expect(onAccept).toHaveBeenCalledTimes(1);
    expect(onAccept).toHaveBeenCalledWith(PROPOSAL_ID);
    expect(onReject).not.toHaveBeenCalled();
  });
});

describe("opening a strategy from chat", () => {
  it("loads the file from the card and does not save it", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      if (url.includes("/messages/stream")) {
        return new Response(
          formatSse(
            agentEvent(1, "user_message", { role: "user", content: "draft" }),
          ) +
            formatSse(
              agentEvent(2, "tool_complete", {
                tool: "write_strategy",
                call_id: "c1",
                ok: true,
                output: {
                  ok: true,
                  data: { strategy_id: STRATEGY_ID, version: 2, name: "EMA cross", ir_hash: HEAD_HASH },
                },
              }),
            ) +
            formatSse(
              agentEvent(3, "turn_complete", {
                status: "completed",
                turn_id: "d".repeat(32),
                error: null,
              }),
            ),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        );
      }
      if (url.includes("/versions")) {
        return jsonResponse(versionsPage());
      }
      if (url.includes(STRATEGY_ID)) {
        return jsonResponse(detail(2, LATEST, HEAD_HASH));
      }
      throw new Error(`unexpected ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Chat />);
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    const box = (await screen.findByLabelText("Strategy DSL")) as HTMLTextAreaElement;
    await waitFor(() => {
      expect(box.value).toBe(LATEST);
    });
    expect(puts(fetchMock)).toHaveLength(0);
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/tools/"))).toBe(false);
  });
});
