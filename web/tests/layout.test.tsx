import { readFileSync } from "node:fs";
import path from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Chat } from "@/components/chat";

describe("narrow layout", () => {
  it("stacks the panes at the 60rem breakpoint and does not clip the workspace", () => {
    const css = readFileSync(path.resolve(import.meta.dirname, "../app/globals.css"), "utf8");
    const start = css.indexOf("@media (max-width: 60rem)");
    const end = css.indexOf("@media (max-width: 40rem)");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const block = css.slice(start, end);
    expect(block).toContain("flex-direction: column");
    expect(block).toContain("overflow: visible");
    expect(block).toContain("min-height: auto");
    expect(block).toContain("max-height: 12rem");
    expect(block).not.toContain("overflow: auto");
  });

  it("keeps the composer inside the chat pane, ahead of the editor", () => {
    render(<Chat />);
    const chat = screen.getByRole("region", { name: "Author chat" });
    const editor = screen.getByRole("region", { name: "Strategy file" });
    const send = screen.getByRole("button", { name: "Send" });
    expect(chat.contains(send)).toBe(true);
    expect(editor.contains(send)).toBe(false);
    expect(chat.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });
});
