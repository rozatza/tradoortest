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
    const results = screen.getByRole("region", { name: "Backtest results" });
    const job = screen.getByLabelText("Job id");
    expect(results.contains(job)).toBe(true);
    expect(results.contains(send)).toBe(false);
    expect(editor.contains(job)).toBe(false);
    expect(editor.compareDocumentPosition(results) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("places the results under the panes and does not overlay them", () => {
    const css = readFileSync(path.resolve(import.meta.dirname, "../app/globals.css"), "utf8");
    expect(css).not.toContain("position: fixed");
    expect(css).not.toContain("position:fixed");
    expect(css).not.toContain("position: absolute");
    const results = css.slice(css.indexOf(".results {"), css.indexOf(".results-form"));
    expect(results).toContain("grid-column: 1 / -1");
    expect(results).toContain("grid-row: 2");
    expect(results).toContain("max-height: 18rem");
    expect(results).toContain("overflow: auto");
    const narrow = css.slice(css.indexOf("@media (max-width: 60rem)"), css.indexOf("@media (max-width: 40rem)"));
    const narrowResults = narrow.slice(narrow.lastIndexOf(".results {"));
    expect(narrowResults).toContain("max-height: none");
    expect(narrowResults).toContain("overflow: visible");
    expect(narrowResults).not.toContain("overflow: auto");
    expect(narrow).toContain("flex-direction: column");
  });

  it("wraps the action row so download buttons stay inside a narrow page", () => {
    const css = readFileSync(path.resolve(import.meta.dirname, "../app/globals.css"), "utf8");
    const start = css.indexOf(".actions {");
    const end = css.indexOf("button {", start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const block = css.slice(start, end);
    expect(block).toContain("display: flex");
    expect(block).toContain("flex-wrap: wrap");
    const labels = css.slice(css.indexOf(".metrics dt {"), css.indexOf(".metrics dd {"));
    expect(labels).toContain("overflow-wrap: anywhere");
  });
});
